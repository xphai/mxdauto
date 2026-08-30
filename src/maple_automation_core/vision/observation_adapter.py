"""Deterministic, fail-closed adapter from ``FramePacket`` to ``Observation``.

The module deliberately contains no inference-runtime dependency.  A backend
is injected at the boundary, while frame pixels are read and verified through
the existing ``PixelStore`` contract.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Protocol, cast, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from maple_automation_core.capture.frame_source import canonical_calibration_sha256
from maple_automation_core.capture.pixel_store import (
    PixelSpec,
    PixelStore,
    PixelStoreError,
    pixel_digest,
    validate_pixels,
)
from maple_automation_core.domain.frame import FramePacket
from maple_automation_core.domain.observation import (
    Detection,
    DetectionBox,
    ModelBinding,
    Observation,
    ObservationFault,
    ObservationFaultCode,
    ObservationResult,
)

from .preprocess import PreprocessConfig, PreprocessError, PreprocessResult, preprocess_pixels

Tensor = NDArray[np.float32]


@runtime_checkable
class DetectorBackend(Protocol):
    """Small injectable backend contract used by the observation boundary."""

    @property
    def providers(self) -> Sequence[str]: ...

    def infer(
        self,
        tensor: Tensor,
        *,
        provider: str,
        input_name: str,
        output_name: str,
    ) -> DetectorOutput: ...


@dataclass(frozen=True, slots=True)
class DetectorOutput:
    """Backend result envelope; the tensor is validated by ``ObservationAdapter``."""

    output: NDArray[np.float32]
    provider: str
    input_name: str
    output_name: str
    input_shape: tuple[int, ...]
    output_shape: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Fault(Exception):
    code: ObservationFaultCode
    message: str
    details: Mapping[str, object]


def _sha256(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    try:
        bytes.fromhex(value)
    except ValueError:
        return None
    return value.lower()


def _details(value: object) -> object:
    if isinstance(value, np.generic):
        return _details(value.item())
    if isinstance(value, np.ndarray):
        return _details(value.tolist())
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else repr(value)
    if isinstance(value, tuple | list):
        return [_details(item) for item in value]
    if isinstance(value, Mapping):
        return {
            key
            if isinstance(key, str)
            else f"<{type(key).__module__}.{type(key).__qualname__}>": _details(item)
            for key, item in value.items()
        }
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


class ObservationAdapter:
    """Build one typed Observation from a fresh, hash-verified FramePacket.

    The optional hash arguments are external attestations.  Each supplied
    value is checked against the immutable model binding or frame geometry
    before the injected backend is called.
    """

    def __init__(
        self,
        binding: ModelBinding,
        pixel_store: PixelStore,
        backend: DetectorBackend,
        preprocess_config: PreprocessConfig | None = None,
        calibration_sha256: str | None = None,
        model_sha256: str | None = None,
        classes_sha256: str | None = None,
        config_sha256: str | None = None,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not isinstance(binding, ModelBinding):
            raise TypeError("binding must be a ModelBinding.")
        if not isinstance(pixel_store, PixelStore):
            raise TypeError("pixel_store must be a PixelStore.")
        if not callable(getattr(backend, "infer", None)):
            raise TypeError("backend must expose infer().")
        if preprocess_config is None:
            preprocess_config = PreprocessConfig()
        if not isinstance(preprocess_config, PreprocessConfig):
            raise TypeError("preprocess_config must be a PreprocessConfig.")
        if not callable(clock):
            raise TypeError("clock must be callable.")

        self.binding = binding
        self.pixel_store = pixel_store
        self.backend = backend
        self.preprocess_config = preprocess_config
        self.calibration_sha256 = calibration_sha256
        self.model_sha256 = model_sha256
        self.classes_sha256 = classes_sha256
        self.config_sha256 = config_sha256
        self.clock = clock

    def observe(self, frame: FramePacket) -> ObservationResult:
        """Return exactly one ``ObservationResult`` for *frame*."""

        if not isinstance(frame, FramePacket):
            raise TypeError("frame must be a FramePacket.")
        now = frame.captured_at_ns
        try:
            now = self._clock_time("start")
            self._precheck(frame, now)
            pixels, spec = self._read_pixels(frame)
            prepared = self._preprocess(frame, pixels, spec)
            provider = self._provider()
            response = self.backend.infer(
                prepared.tensor,
                provider=provider,
                input_name=self.binding.input_name,
                output_name=self.binding.output_name,
            )
            detections = self._decode(response, prepared, provider)
            completed_at_ns = self._clock_time("completion")
            if completed_at_ns < now:
                raise _Fault(
                    ObservationFaultCode.FRAME_LINEAGE_MISMATCH,
                    "observation clock moved backwards during inference",
                    {"started_at_ns": now, "completed_at_ns": completed_at_ns},
                )
            now = completed_at_ns
            self._ensure_fresh(frame, now)
            observation = Observation(
                session_id=frame.session_id,
                source_id=frame.source_id,
                frame_id=frame.frame_id,
                captured_at_ns=frame.captured_at_ns,
                observed_at_ns=now,
                pixel_digest=frame.content_hash,
                image_ref=frame.image_ref,
                calibration_sha256=self._calibration(frame),
                working_size=frame.source_geometry.working_size,
                model_binding=self.binding,
                execution_provider=provider,
                detections=tuple(detections),
            )
            return ObservationResult(observation=observation)
        except _Fault as fault:
            return self._fault_result(frame, fault.code, fault.message, fault.details, now)
        except PreprocessError as exc:
            return self._fault_result(
                frame, ObservationFaultCode.PREPROCESS_ERROR, str(exc), {}, now
            )
        except (PixelStoreError, OSError) as exc:
            return self._fault_result(frame, ObservationFaultCode.PIXEL_MISSING, str(exc), {}, now)
        except Exception as exc:
            return self._fault_result(
                frame,
                ObservationFaultCode.INFERENCE_ERROR,
                str(exc) or "detector backend failed",
                {},
                now,
            )

    def _now(self) -> int:
        return self._validate_now(self.clock())

    def _clock_time(self, phase: str) -> int:
        try:
            return self._now()
        except Exception as exc:
            raise _Fault(
                ObservationFaultCode.FRAME_LINEAGE_MISMATCH,
                "observation clock did not return a valid timestamp",
                {"phase": phase, "error": str(exc).strip() or type(exc).__name__},
            ) from exc

    @staticmethod
    def _validate_now(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("clock must return a non-negative integer timestamp.")
        return value

    def _precheck(self, frame: FramePacket, now: int) -> None:
        self._ensure_fresh(frame, now)

        if frame.source_geometry != self.preprocess_config.geometry:
            raise _Fault(
                ObservationFaultCode.FRAME_LINEAGE_MISMATCH,
                "frame source geometry does not match preprocessing geometry",
                {
                    "frame_geometry": frame.source_geometry.to_dict(),
                    "expected_geometry": self.preprocess_config.geometry.to_dict(),
                },
            )
        expected_image_ref = f"cas://sha256/{frame.content_hash.lower()}"
        if frame.image_ref != expected_image_ref:
            raise _Fault(
                ObservationFaultCode.FRAME_LINEAGE_MISMATCH,
                "frame image_ref does not match its content hash",
                {"expected": expected_image_ref, "actual": frame.image_ref},
            )

        calibration = self._calibration(frame)
        metadata = frame.image_metadata
        self._attest(
            self.calibration_sha256,
            metadata,
            calibration,
            ObservationFaultCode.CALIBRATION_MISMATCH,
            "calibration_sha256",
        )
        self._attest(
            self.model_sha256,
            metadata,
            self.binding.model_sha256,
            ObservationFaultCode.MODEL_HASH_MISMATCH,
            "model_sha256",
            missing_code=ObservationFaultCode.MODEL_MISSING,
        )
        self._attest(
            self.classes_sha256,
            metadata,
            self.binding.classes_sha256,
            ObservationFaultCode.CLASSES_HASH_MISMATCH,
            "classes_sha256",
            missing_code=ObservationFaultCode.CLASSES_HASH_MISMATCH,
        )
        self._attest(
            self.config_sha256,
            metadata,
            self.binding.config_sha256,
            ObservationFaultCode.MODEL_BINDING_MISMATCH,
            "config_sha256",
            missing_code=ObservationFaultCode.MODEL_BINDING_MISMATCH,
        )
        self._attest(
            None,
            metadata,
            self.binding.digest,
            ObservationFaultCode.MODEL_BINDING_MISMATCH,
            "binding_sha256",
        )

        if self.preprocess_config.version != self.binding.preprocess_version:
            raise _Fault(
                ObservationFaultCode.PREPROCESS_ERROR,
                "preprocess version does not match the model binding",
                {
                    "expected": self.binding.preprocess_version,
                    "actual": self.preprocess_config.version,
                },
            )
        if self.preprocess_config.digest != self.binding.preprocess_sha256:
            raise _Fault(
                ObservationFaultCode.PREPROCESS_ERROR,
                "preprocess digest does not match the model binding",
                {
                    "expected": self.binding.preprocess_sha256,
                    "actual": self.preprocess_config.digest,
                },
            )
        self._attest(
            None,
            metadata,
            self.binding.preprocess_sha256,
            ObservationFaultCode.PREPROCESS_ERROR,
            "preprocess_sha256",
        )
        if (
            metadata.get("preprocess_version") is not None
            and metadata["preprocess_version"] != self.binding.preprocess_version
        ):
            raise _Fault(
                ObservationFaultCode.PREPROCESS_ERROR,
                "preprocess version attestation does not match the model binding",
                {
                    "expected": self.binding.preprocess_version,
                    "actual": metadata["preprocess_version"],
                },
            )
        if self.preprocess_config.model_size != self.binding.input_size:
            raise _Fault(
                ObservationFaultCode.INPUT_SHAPE_MISMATCH,
                "preprocess model size does not match the model binding",
                {
                    "expected": self.binding.input_size.to_dict(),
                    "actual": self.preprocess_config.model_size.to_dict(),
                },
            )
        roi = (
            self.preprocess_config.roi.left,
            self.preprocess_config.roi.top,
            self.preprocess_config.roi.right,
            self.preprocess_config.roi.bottom,
        )
        if roi != self.binding.roi:
            raise _Fault(
                ObservationFaultCode.MODEL_BINDING_MISMATCH,
                "preprocess ROI does not match the model binding",
                {"expected": list(self.binding.roi), "actual": list(roi)},
            )

    @staticmethod
    def _ensure_fresh(frame: FramePacket, now: int) -> None:
        try:
            fresh = frame.is_fresh_at(now)
        except ValueError as exc:
            raise _Fault(
                ObservationFaultCode.FRAME_LINEAGE_MISMATCH,
                str(exc),
                {"captured_at_ns": frame.captured_at_ns, "now_ns": now},
            ) from exc
        if not fresh:
            raise _Fault(
                ObservationFaultCode.FRAME_STALE,
                "frame freshness lease has expired",
                {
                    "age_ns": frame.capture_health.age_ns_at(now),
                    "max_age_ns": frame.capture_health.max_age_ns,
                },
            )

    def _attest(
        self,
        supplied: object,
        metadata: Mapping[str, object],
        expected: str,
        code: ObservationFaultCode,
        name: str,
        *,
        missing_code: ObservationFaultCode | None = None,
    ) -> None:
        claims = [claim for claim in (supplied, metadata.get(name)) if claim is not None]
        if not claims:
            if missing_code is not None:
                raise _Fault(
                    missing_code,
                    f"{name} attestation is required",
                    {},
                )
            return
        for claim in claims:
            self._check_hash(claim, expected, code, name)

    @staticmethod
    def _check_hash(
        supplied: object,
        expected: str,
        code: ObservationFaultCode,
        name: str,
    ) -> None:
        if supplied is None:
            return
        actual = _sha256(supplied)
        if actual is None or actual != expected.lower():
            raise _Fault(
                code,
                f"{name} does not match the bound value",
                {"expected": expected.lower(), "actual": _details(supplied)},
            )

    @staticmethod
    def _calibration(frame: FramePacket) -> str:
        return canonical_calibration_sha256(frame.source_geometry, frame.transform_version)

    def _spec(self, frame: FramePacket) -> PixelSpec:
        source_size = self.preprocess_config.geometry.source_size
        expected = PixelSpec(width=source_size.width, height=source_size.height)
        raw = frame.image_metadata.get("pixel_spec")
        if raw is None:
            return expected
        try:
            actual = (
                raw
                if isinstance(raw, PixelSpec)
                else PixelSpec.from_dict(cast(Mapping[str, object], raw))
            )
        except (TypeError, ValueError) as exc:
            raise _Fault(
                ObservationFaultCode.PIXEL_HASH_MISMATCH,
                "frame pixel specification is invalid",
                {"error": str(exc)},
            ) from exc
        if actual != expected:
            raise _Fault(
                ObservationFaultCode.PIXEL_HASH_MISMATCH,
                "frame pixel specification does not match source geometry",
                {"expected": expected.to_dict(), "actual": actual.to_dict()},
            )
        return actual

    def _read_pixels(self, frame: FramePacket) -> tuple[bytes, PixelSpec]:
        spec = self._spec(frame)
        digest = frame.content_hash.lower()
        try:
            raw = self.pixel_store.read(digest, spec)
        except (PixelStoreError, OSError, KeyError) as exc:
            raise _Fault(
                ObservationFaultCode.PIXEL_MISSING,
                str(exc) or "pixel CAS object is missing",
                {"pixel_digest": digest},
            ) from exc
        try:
            pixels = validate_pixels(spec, raw)
            actual = pixel_digest(spec, pixels)
        except (TypeError, ValueError) as exc:
            raise _Fault(
                ObservationFaultCode.PIXEL_HASH_MISMATCH,
                str(exc) or "pixel CAS bytes are invalid",
                {"pixel_digest": digest},
            ) from exc
        if actual != digest:
            raise _Fault(
                ObservationFaultCode.PIXEL_HASH_MISMATCH,
                "pixel CAS bytes do not match FramePacket.content_hash",
                {"expected": digest, "actual": actual},
            )
        return pixels, spec

    def _preprocess(self, frame: FramePacket, pixels: bytes, spec: PixelSpec) -> PreprocessResult:
        try:
            result = preprocess_pixels(
                pixels,
                spec,
                config=self.preprocess_config,
                expected_pixel_digest=frame.content_hash.lower(),
            )
        except PreprocessError as exc:
            raise _Fault(
                ObservationFaultCode.PREPROCESS_ERROR,
                str(exc),
                {"preprocess_sha256": self.preprocess_config.digest},
            ) from exc
        if result.source_pixel_digest != frame.content_hash.lower():
            raise _Fault(
                ObservationFaultCode.PIXEL_HASH_MISMATCH,
                "preprocess source digest does not match the frame digest",
                {},
            )
        return result

    def _provider(self) -> str:
        try:
            available = tuple(self.backend.providers)
        except Exception as exc:
            raise _Fault(
                ObservationFaultCode.PROVIDER_UNAVAILABLE,
                str(exc) or "backend providers are unavailable",
                {},
            ) from exc
        if not available or any(not isinstance(item, str) or not item for item in available):
            raise _Fault(
                ObservationFaultCode.PROVIDER_UNAVAILABLE,
                "backend providers are invalid or empty",
                {"available_providers": _details(available)},
            )
        requested = self.binding.requested_providers[0]
        if requested in available:
            return requested
        raise _Fault(
            ObservationFaultCode.PROVIDER_UNAVAILABLE,
            "none of the requested providers is available",
            {
                "requested_provider": requested,
                "available_providers": list(available),
            },
        )

    def _decode(
        self,
        response: DetectorOutput,
        prepared: PreprocessResult,
        provider: str,
    ) -> list[Detection]:
        if not isinstance(response, DetectorOutput):
            raise _Fault(
                ObservationFaultCode.INFERENCE_ERROR,
                "backend must return DetectorOutput",
                {},
            )
        if response.provider != provider:
            raise _Fault(
                ObservationFaultCode.PROVIDER_UNAVAILABLE,
                "backend response provider does not match selection",
                {"expected": provider, "actual": response.provider},
            )
        if response.input_name != self.binding.input_name:
            raise _Fault(
                ObservationFaultCode.INPUT_SHAPE_MISMATCH,
                "backend response input name does not match binding",
                {"expected": self.binding.input_name, "actual": response.input_name},
            )
        if response.output_name != self.binding.output_name:
            raise _Fault(
                ObservationFaultCode.OUTPUT_SHAPE_MISMATCH,
                "backend response output name does not match binding",
                {"expected": self.binding.output_name, "actual": response.output_name},
            )
        expected_input_shape = (1, 3, self.binding.input_size.height, self.binding.input_size.width)
        if response.input_shape != expected_input_shape:
            raise _Fault(
                ObservationFaultCode.INPUT_SHAPE_MISMATCH,
                "backend response input shape does not match binding",
                {"expected": list(expected_input_shape), "actual": _details(response.input_shape)},
            )
        if response.output_shape != self.binding.output_shape:
            raise _Fault(
                ObservationFaultCode.OUTPUT_SHAPE_MISMATCH,
                "backend response output shape does not match binding",
                {
                    "expected": list(self.binding.output_shape),
                    "actual": _details(response.output_shape),
                },
            )
        output = response.output
        if not isinstance(output, np.ndarray) or output.dtype != np.dtype(np.float32):
            raise _Fault(
                ObservationFaultCode.OUTPUT_SHAPE_MISMATCH,
                "backend output must be a float32 ndarray",
                {"dtype": str(getattr(output, "dtype", type(output).__name__))},
            )
        if output.shape != self.binding.output_shape:
            raise _Fault(
                ObservationFaultCode.OUTPUT_SHAPE_MISMATCH,
                "backend output tensor shape does not match binding",
                {"expected": list(self.binding.output_shape), "actual": list(output.shape)},
            )
        if not np.isfinite(output).all():
            raise _Fault(
                ObservationFaultCode.INFERENCE_ERROR,
                "backend output tensor contains non-finite values",
                {},
            )
        return self._decode_yolo(output, prepared)

    def _decode_yolo(
        self, output: NDArray[np.float32], prepared: PreprocessResult
    ) -> list[Detection]:
        detections: list[Detection] = []
        class_count = len(self.binding.classes)
        for index in range(self.binding.output_shape[2]):
            cx, cy, width, height = (float(output[0, offset, index]) for offset in range(4))
            scores = output[0, 4 : 4 + class_count, index]
            if not np.isfinite(scores).all() or np.any(scores < 0.0) or np.any(scores > 1.0):
                raise _Fault(
                    ObservationFaultCode.INFERENCE_ERROR,
                    "backend confidence must be a finite probability",
                    {"output_index": index},
                )
            class_id = int(np.argmax(scores))
            confidence = float(scores[class_id])
            if confidence < self.binding.detection_confidence:
                continue
            if width <= 0.0 or height <= 0.0:
                raise _Fault(
                    ObservationFaultCode.INFERENCE_ERROR,
                    "backend box must have positive dimensions",
                    {"output_index": index},
                )
            box = (cx - width / 2.0, cy - height / 2.0, cx + width / 2.0, cy + height / 2.0)
            self._validate_box(box, index)
            try:
                working = prepared.transform.model_box_to_working(box, clip=True)
                detection_box = DetectionBox(
                    left=working[0],
                    top=working[1],
                    right=working[2],
                    bottom=working[3],
                    working_size=self.preprocess_config.geometry.working_size,
                )
                detections.append(
                    Detection(
                        class_id=class_id,
                        class_name=self.binding.classes[class_id],
                        confidence=confidence,
                        box=detection_box,
                    )
                )
            except (PreprocessError, TypeError, ValueError) as exc:
                raise _Fault(
                    ObservationFaultCode.INFERENCE_ERROR,
                    str(exc),
                    {"output_index": index, "box": list(box)},
                ) from exc
        return sorted(detections, key=lambda item: item.sort_key)

    def _validate_box(self, box: tuple[float, float, float, float], index: int) -> None:
        left, top, right, bottom = box
        width = self.binding.input_size.width
        height = self.binding.input_size.height
        if (
            not all(isfinite(value) for value in box)
            or left < 0.0
            or top < 0.0
            or right > width
            or bottom > height
            or right <= left
            or bottom <= top
        ):
            raise _Fault(
                ObservationFaultCode.INFERENCE_ERROR,
                "backend box is outside model input bounds",
                {"output_index": index, "box": list(box)},
            )

    def _fault_result(
        self,
        frame: FramePacket,
        code: ObservationFaultCode,
        message: str,
        details: Mapping[str, object],
        now: int,
    ) -> ObservationResult:
        fault = ObservationFault(
            session_id=frame.session_id,
            source_id=frame.source_id,
            frame_id=frame.frame_id,
            captured_at_ns=frame.captured_at_ns,
            failed_at_ns=max(now, frame.captured_at_ns),
            pixel_digest=frame.content_hash,
            image_ref=frame.image_ref,
            calibration_sha256=self._calibration(frame),
            working_size=frame.source_geometry.working_size,
            model_binding=self.binding,
            code=code,
            message=message.strip() if isinstance(message, str) and message.strip() else code.value,
            details=cast(Mapping[str, object], _details(details)),
        )
        return ObservationResult(fault=fault)


__all__ = [
    "DetectorBackend",
    "DetectorOutput",
    "ObservationAdapter",
]
