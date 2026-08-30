from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Any

from ._contract_utils import (
    ensure_json_value,
    ensure_mapping,
    ensure_non_empty_str,
    ensure_non_negative_int,
    ensure_positive_int,
    ensure_probability,
    ensure_sha256_hex,
    ensure_time_ns,
    freeze_json_value,
    hash_payload,
    to_json_dict,
)
from .frame import FrameSize

WORKING_COORDINATE_SPACE = "working"


def _normalise_sha256(value: str, field_name: str) -> str:
    ensure_sha256_hex(value, field_name)
    return value.lower()


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite number.")
    result = float(value)
    return 0.0 if result == 0.0 else result


@dataclass(frozen=True, slots=True)
class DetectionBox:
    """Half-open detector bounds in the declared working pixel space."""

    left: float
    top: float
    right: float
    bottom: float
    working_size: FrameSize

    def __post_init__(self) -> None:
        if not isinstance(self.working_size, FrameSize):
            raise TypeError("working_size must be FrameSize.")
        for field_name in ("left", "top", "right", "bottom"):
            object.__setattr__(
                self,
                field_name,
                _finite_number(getattr(self, field_name), field_name),
            )
        if self.left < 0 or self.top < 0:
            raise ValueError("DetectionBox left/top must be >= 0.")
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("DetectionBox must have positive width and height.")
        if self.right > self.working_size.width or self.bottom > self.working_size.height:
            raise ValueError("DetectionBox must fit inside working_size.")

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def coordinate_space(self) -> str:
        return WORKING_COORDINATE_SPACE

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "coordinate_space": WORKING_COORDINATE_SPACE,
            "working_size": self.working_size.to_dict(),
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DetectionBox:
        data = ensure_mapping(value, "DetectionBox payload")
        try:
            if data["coordinate_space"] != WORKING_COORDINATE_SPACE:
                raise ValueError("DetectionBox coordinate_space must be 'working'.")
            return cls(
                left=data["left"],
                top=data["top"],
                right=data["right"],
                bottom=data["bottom"],
                working_size=FrameSize.from_dict(data["working_size"]),
            )
        except KeyError as exc:
            raise ValueError(f"DetectionBox payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class Detection:
    """One class-labelled detection projected into working pixel space."""

    class_id: int
    class_name: str
    confidence: float
    box: DetectionBox

    def __post_init__(self) -> None:
        ensure_non_negative_int(self.class_id, "class_id")
        ensure_non_empty_str(self.class_name, "class_name")
        ensure_probability(self.confidence, "confidence")
        object.__setattr__(self, "confidence", float(self.confidence))
        if not isinstance(self.box, DetectionBox):
            raise TypeError("box must be DetectionBox.")

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    @property
    def sort_key(self) -> tuple[float | int | str, ...]:
        """Canonical order independent of backend tensor/anchor traversal."""

        return (
            -self.confidence,
            self.class_id,
            self.class_name,
            self.box.top,
            self.box.left,
            self.box.bottom,
            self.box.right,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "class_name": self.class_name,
            "confidence": self.confidence,
            "box": self.box.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Detection:
        data = ensure_mapping(value, "Detection payload")
        try:
            return cls(
                class_id=data["class_id"],
                class_name=data["class_name"],
                confidence=data["confidence"],
                box=DetectionBox.from_dict(data["box"]),
            )
        except KeyError as exc:
            raise ValueError(f"Detection payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """Frozen model, preprocessing, threshold and provider contract."""

    release_id: str
    model_id: str
    model_sha256: str
    classes: tuple[str, ...]
    classes_sha256: str
    config_sha256: str
    preprocess_version: str
    preprocess_sha256: str
    input_name: str
    input_size: FrameSize
    output_name: str
    output_shape: tuple[int, int, int]
    requested_providers: tuple[str, ...]
    detection_confidence: float
    iou_threshold: float
    roi: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.release_id, "release_id")
        ensure_non_empty_str(self.model_id, "model_id")
        object.__setattr__(
            self, "model_sha256", _normalise_sha256(self.model_sha256, "model_sha256")
        )
        object.__setattr__(
            self,
            "classes_sha256",
            _normalise_sha256(self.classes_sha256, "classes_sha256"),
        )
        object.__setattr__(
            self, "config_sha256", _normalise_sha256(self.config_sha256, "config_sha256")
        )
        object.__setattr__(
            self,
            "preprocess_sha256",
            _normalise_sha256(self.preprocess_sha256, "preprocess_sha256"),
        )
        ensure_non_empty_str(self.preprocess_version, "preprocess_version")
        ensure_non_empty_str(self.input_name, "input_name")
        ensure_non_empty_str(self.output_name, "output_name")
        if not isinstance(self.input_size, FrameSize):
            raise TypeError("input_size must be FrameSize.")

        if not isinstance(self.classes, tuple) or not self.classes:
            raise TypeError("classes must be a non-empty tuple.")
        for class_name in self.classes:
            ensure_non_empty_str(class_name, "classes item")
        if len(set(self.classes)) != len(self.classes):
            raise ValueError("classes must be unique and ordered by class_id.")

        if not isinstance(self.output_shape, tuple) or len(self.output_shape) != 3:
            raise TypeError("output_shape must be a three-item tuple.")
        for index, dimension in enumerate(self.output_shape):
            ensure_positive_int(dimension, f"output_shape[{index}]")
        if self.output_shape[0] != 1:
            raise ValueError("output_shape batch dimension must be 1.")
        if self.output_shape[1] != 4 + len(self.classes):
            raise ValueError("output_shape feature dimension must equal 4 + len(classes).")

        if not isinstance(self.requested_providers, tuple) or not self.requested_providers:
            raise TypeError("requested_providers must be a non-empty tuple.")
        for provider in self.requested_providers:
            ensure_non_empty_str(provider, "requested_providers item")
        if len(set(self.requested_providers)) != len(self.requested_providers):
            raise ValueError("requested_providers must be unique and ordered by preference.")

        ensure_probability(self.detection_confidence, "detection_confidence")
        ensure_probability(self.iou_threshold, "iou_threshold")
        object.__setattr__(self, "detection_confidence", float(self.detection_confidence))
        object.__setattr__(self, "iou_threshold", float(self.iou_threshold))

        if not isinstance(self.roi, tuple) or len(self.roi) != 4:
            raise TypeError("roi must be a four-item tuple.")
        roi = tuple(_finite_number(value, f"roi[{index}]") for index, value in enumerate(self.roi))
        if any(value < 0.0 or value > 1.0 for value in roi):
            raise ValueError("roi values must be normalized to [0, 1].")
        if roi[0] >= roi[2] or roi[1] >= roi[3]:
            raise ValueError("roi must have positive normalized width and height.")
        object.__setattr__(self, "roi", roi)

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "model_id": self.model_id,
            "model_sha256": self.model_sha256,
            "classes": list(self.classes),
            "classes_sha256": self.classes_sha256,
            "config_sha256": self.config_sha256,
            "preprocess_version": self.preprocess_version,
            "preprocess_sha256": self.preprocess_sha256,
            "input_name": self.input_name,
            "input_size": self.input_size.to_dict(),
            "output_name": self.output_name,
            "output_shape": list(self.output_shape),
            "requested_providers": list(self.requested_providers),
            "detection_confidence": self.detection_confidence,
            "iou_threshold": self.iou_threshold,
            "roi": list(self.roi),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelBinding:
        data = ensure_mapping(value, "ModelBinding payload")
        try:
            raw_classes = data["classes"]
            raw_output_shape = data["output_shape"]
            raw_providers = data["requested_providers"]
            raw_roi = data["roi"]
            for field_name, raw_value in (
                ("classes", raw_classes),
                ("output_shape", raw_output_shape),
                ("requested_providers", raw_providers),
                ("roi", raw_roi),
            ):
                if not isinstance(raw_value, list | tuple):
                    raise ValueError(f"{field_name} must be an array.")
            return cls(
                release_id=data["release_id"],
                model_id=data["model_id"],
                model_sha256=data["model_sha256"],
                classes=tuple(raw_classes),
                classes_sha256=data["classes_sha256"],
                config_sha256=data["config_sha256"],
                preprocess_version=data["preprocess_version"],
                preprocess_sha256=data["preprocess_sha256"],
                input_name=data["input_name"],
                input_size=FrameSize.from_dict(data["input_size"]),
                output_name=data["output_name"],
                output_shape=tuple(raw_output_shape),
                requested_providers=tuple(raw_providers),
                detection_confidence=data["detection_confidence"],
                iou_threshold=data["iou_threshold"],
                roi=tuple(raw_roi),
            )
        except KeyError as exc:
            raise ValueError(f"ModelBinding payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class Observation:
    """Successful, immutable detector output for one admitted frame."""

    session_id: str
    source_id: str
    frame_id: int
    captured_at_ns: int
    observed_at_ns: int
    pixel_digest: str
    image_ref: str
    calibration_sha256: str
    working_size: FrameSize
    model_binding: ModelBinding
    execution_provider: str
    detections: tuple[Detection, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_frame_lineage(
            session_id=self.session_id,
            source_id=self.source_id,
            frame_id=self.frame_id,
            captured_at_ns=self.captured_at_ns,
            terminal_at_ns=self.observed_at_ns,
            pixel_digest=self.pixel_digest,
            image_ref=self.image_ref,
            calibration_sha256=self.calibration_sha256,
            working_size=self.working_size,
            terminal_name="observed_at_ns",
        )
        object.__setattr__(self, "pixel_digest", self.pixel_digest.lower())
        object.__setattr__(self, "calibration_sha256", self.calibration_sha256.lower())
        if not isinstance(self.model_binding, ModelBinding):
            raise TypeError("model_binding must be ModelBinding.")
        ensure_non_empty_str(self.execution_provider, "execution_provider")
        if self.execution_provider not in self.model_binding.requested_providers:
            raise ValueError("execution_provider must occur in requested_providers.")
        if not isinstance(self.detections, tuple):
            raise TypeError("detections must be a tuple.")
        if any(not isinstance(detection, Detection) for detection in self.detections):
            raise TypeError("detections must contain only Detection values.")
        for detection in self.detections:
            if detection.box.working_size != self.working_size:
                raise ValueError("detection box working_size must match observation working_size.")
            if detection.class_id >= len(self.model_binding.classes):
                raise ValueError("detection class_id is outside the bound class list.")
            if self.model_binding.classes[detection.class_id] != detection.class_name:
                raise ValueError("detection class_name does not match bound class_id.")
            if detection.confidence < self.model_binding.detection_confidence:
                raise ValueError("detection confidence is below the bound threshold.")
        object.__setattr__(
            self, "detections", tuple(sorted(self.detections, key=lambda item: item.sort_key))
        )

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    @property
    def detection_digest(self) -> str:
        return hash_payload([detection.to_dict() for detection in self.detections])

    @property
    def plan_suppressed(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source_id": self.source_id,
            "frame_id": self.frame_id,
            "captured_at_ns": self.captured_at_ns,
            "observed_at_ns": self.observed_at_ns,
            "pixel_digest": self.pixel_digest,
            "image_ref": self.image_ref,
            "calibration_sha256": self.calibration_sha256,
            "working_size": self.working_size.to_dict(),
            "model_binding": self.model_binding.to_dict(),
            "execution_provider": self.execution_provider,
            "detections": [detection.to_dict() for detection in self.detections],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Observation:
        data = ensure_mapping(value, "Observation payload")
        try:
            raw_detections = data.get("detections", [])
            if not isinstance(raw_detections, list | tuple):
                raise ValueError("detections must be an array.")
            return cls(
                session_id=data["session_id"],
                source_id=data["source_id"],
                frame_id=data["frame_id"],
                captured_at_ns=data["captured_at_ns"],
                observed_at_ns=data["observed_at_ns"],
                pixel_digest=data["pixel_digest"],
                image_ref=data["image_ref"],
                calibration_sha256=data["calibration_sha256"],
                working_size=FrameSize.from_dict(data["working_size"]),
                model_binding=ModelBinding.from_dict(data["model_binding"]),
                execution_provider=data["execution_provider"],
                detections=tuple(Detection.from_dict(item) for item in raw_detections),
            )
        except KeyError as exc:
            raise ValueError(f"Observation payload missing key: {exc.args[0]}") from exc


class ObservationFaultCode(str, Enum):
    FRAME_STALE = "frame_stale"
    FRAME_LINEAGE_MISMATCH = "frame_lineage_mismatch"
    PIXEL_MISSING = "pixel_missing"
    PIXEL_HASH_MISMATCH = "pixel_hash_mismatch"
    CALIBRATION_MISMATCH = "calibration_mismatch"
    MODEL_MISSING = "model_missing"
    MODEL_HASH_MISMATCH = "model_hash_mismatch"
    CLASSES_HASH_MISMATCH = "classes_hash_mismatch"
    MODEL_BINDING_MISMATCH = "model_binding_mismatch"
    INPUT_SHAPE_MISMATCH = "input_shape_mismatch"
    OUTPUT_SHAPE_MISMATCH = "output_shape_mismatch"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PREPROCESS_ERROR = "preprocess_error"
    INFERENCE_ERROR = "inference_error"


@dataclass(frozen=True, slots=True)
class ObservationFault:
    """Fail-closed observation outcome retaining expected frame/model lineage."""

    session_id: str
    source_id: str
    frame_id: int
    captured_at_ns: int
    failed_at_ns: int
    pixel_digest: str
    image_ref: str
    calibration_sha256: str
    working_size: FrameSize
    model_binding: ModelBinding
    code: ObservationFaultCode
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_frame_lineage(
            session_id=self.session_id,
            source_id=self.source_id,
            frame_id=self.frame_id,
            captured_at_ns=self.captured_at_ns,
            terminal_at_ns=self.failed_at_ns,
            pixel_digest=self.pixel_digest,
            image_ref=self.image_ref,
            calibration_sha256=self.calibration_sha256,
            working_size=self.working_size,
            terminal_name="failed_at_ns",
        )
        object.__setattr__(self, "pixel_digest", self.pixel_digest.lower())
        object.__setattr__(self, "calibration_sha256", self.calibration_sha256.lower())
        if not isinstance(self.model_binding, ModelBinding):
            raise TypeError("model_binding must be ModelBinding.")
        if not isinstance(self.code, ObservationFaultCode):
            raise TypeError("code must be ObservationFaultCode.")
        ensure_non_empty_str(self.message, "message")
        details = ensure_mapping(self.details, "details")
        ensure_json_value(details, "details")
        object.__setattr__(self, "details", freeze_json_value(details))

    @property
    def plan_suppressed(self) -> bool:
        return True

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source_id": self.source_id,
            "frame_id": self.frame_id,
            "captured_at_ns": self.captured_at_ns,
            "failed_at_ns": self.failed_at_ns,
            "pixel_digest": self.pixel_digest,
            "image_ref": self.image_ref,
            "calibration_sha256": self.calibration_sha256,
            "working_size": self.working_size.to_dict(),
            "model_binding": self.model_binding.to_dict(),
            "code": self.code.value,
            "message": self.message,
            "details": to_json_dict(self.details),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ObservationFault:
        data = ensure_mapping(value, "ObservationFault payload")
        try:
            details = data.get("details", {})
            return cls(
                session_id=data["session_id"],
                source_id=data["source_id"],
                frame_id=data["frame_id"],
                captured_at_ns=data["captured_at_ns"],
                failed_at_ns=data["failed_at_ns"],
                pixel_digest=data["pixel_digest"],
                image_ref=data["image_ref"],
                calibration_sha256=data["calibration_sha256"],
                working_size=FrameSize.from_dict(data["working_size"]),
                model_binding=ModelBinding.from_dict(data["model_binding"]),
                code=ObservationFaultCode(data["code"]),
                message=data["message"],
                details=ensure_mapping(details, "details"),
            )
        except KeyError as exc:
            raise ValueError(f"ObservationFault payload missing key: {exc.args[0]}") from exc


def _validate_frame_lineage(
    *,
    session_id: str,
    source_id: str,
    frame_id: int,
    captured_at_ns: int,
    terminal_at_ns: int,
    pixel_digest: str,
    image_ref: str,
    calibration_sha256: str,
    working_size: FrameSize,
    terminal_name: str,
) -> None:
    ensure_non_empty_str(session_id, "session_id")
    ensure_non_empty_str(source_id, "source_id")
    ensure_non_negative_int(frame_id, "frame_id")
    ensure_time_ns(captured_at_ns, "captured_at_ns")
    ensure_time_ns(terminal_at_ns, terminal_name)
    if terminal_at_ns < captured_at_ns:
        raise ValueError(f"{terminal_name} must be >= captured_at_ns.")
    ensure_sha256_hex(pixel_digest, "pixel_digest")
    ensure_non_empty_str(image_ref, "image_ref")
    ensure_sha256_hex(calibration_sha256, "calibration_sha256")
    if not isinstance(working_size, FrameSize):
        raise TypeError("working_size must be FrameSize.")


@dataclass(frozen=True, slots=True)
class ObservationResult:
    """Exactly one successful Observation or one fail-closed ObservationFault."""

    observation: Observation | None = None
    fault: ObservationFault | None = None

    def __post_init__(self) -> None:
        if (self.observation is None) == (self.fault is None):
            raise ValueError("ObservationResult requires exactly one of observation or fault.")
        if self.observation is not None and not isinstance(self.observation, Observation):
            raise TypeError("observation must be Observation or None.")
        if self.fault is not None and not isinstance(self.fault, ObservationFault):
            raise TypeError("fault must be ObservationFault or None.")

    @property
    def succeeded(self) -> bool:
        return self.observation is not None

    @property
    def plan_suppressed(self) -> bool:
        return self.fault is not None

    @property
    def session_id(self) -> str:
        branch = self.observation if self.observation is not None else self.fault
        assert branch is not None
        return branch.session_id

    @property
    def frame_id(self) -> int:
        branch = self.observation if self.observation is not None else self.fault
        assert branch is not None
        return branch.frame_id

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "success" if self.succeeded else "fault",
            "plan_suppressed": self.plan_suppressed,
            "observation": None if self.observation is None else self.observation.to_dict(),
            "fault": None if self.fault is None else self.fault.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ObservationResult:
        data = ensure_mapping(value, "ObservationResult payload")
        raw_observation = data.get("observation")
        raw_fault = data.get("fault")
        if raw_observation is not None and not isinstance(raw_observation, Mapping):
            raise ValueError("observation must be an object or null.")
        if raw_fault is not None and not isinstance(raw_fault, Mapping):
            raise ValueError("fault must be an object or null.")
        result = cls(
            observation=None if raw_observation is None else Observation.from_dict(raw_observation),
            fault=None if raw_fault is None else ObservationFault.from_dict(raw_fault),
        )
        expected_status = "success" if result.succeeded else "fault"
        if "status" in data and data["status"] != expected_status:
            raise ValueError("ObservationResult status contradicts its result branch.")
        if "plan_suppressed" in data and data["plan_suppressed"] is not result.plan_suppressed:
            raise ValueError("ObservationResult plan_suppressed contradicts its result branch.")
        return result
