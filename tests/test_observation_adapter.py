from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from maple_automation_core.capture.frame_source import canonical_calibration_sha256
from maple_automation_core.capture.pixel_store import PixelSpec, PixelStore
from maple_automation_core.domain.frame import (
    CaptureHealth,
    FramePacket,
    FrameSize,
    SourceGeometry,
    SourceRect,
)
from maple_automation_core.domain.observation import ModelBinding, ObservationFaultCode
from maple_automation_core.vision.observation_adapter import (
    DetectorOutput,
    ObservationAdapter,
)
from maple_automation_core.vision.preprocess import NormalizedRoi, PreprocessConfig


def _config() -> PreprocessConfig:
    geometry = SourceGeometry(
        source_size=FrameSize(width=2, height=2),
        content_rect=SourceRect(x=0, y=0, width=2, height=2),
        working_size=FrameSize(width=4, height=4),
    )
    return PreprocessConfig(
        geometry=geometry,
        roi=NormalizedRoi(left=0.0, top=0.0, right=1.0, bottom=1.0),
        model_size=FrameSize(width=4, height=4),
    )


def _binding(config: PreprocessConfig) -> ModelBinding:
    return ModelBinding(
        release_id="g1-obs-test",
        model_id="fixture-model",
        model_sha256="a" * 64,
        classes=("mob", "elite"),
        classes_sha256="b" * 64,
        config_sha256="c" * 64,
        preprocess_version=config.version,
        preprocess_sha256=config.digest,
        input_name="images",
        input_size=config.model_size,
        output_name="output0",
        output_shape=(1, 6, 2),
        requested_providers=("FixtureExecutionProvider", "CPUExecutionProvider"),
        detection_confidence=0.25,
        iou_threshold=0.45,
        roi=(0.0, 0.0, 1.0, 1.0),
    )


def _frame(config: PreprocessConfig, store: PixelStore) -> FramePacket:
    spec = PixelSpec(width=2, height=2)
    digest = store.put(spec, bytes(range(spec.length)))
    geometry = config.geometry
    return FramePacket(
        source_id="source",
        session_id="session",
        frame_id=7,
        captured_at_ns=0,
        received_at_ns=1,
        transform_version="calibration-v1",
        clock_domain="monotonic",
        content_hash=digest,
        source_geometry=geometry,
        image_ref=f"cas://sha256/{digest}",
        capture_health=CaptureHealth(
            session_id="session",
            frame_id=7,
            source_id="source",
            content_hash=digest,
            clock_domain="monotonic",
            captured_at_ns=0,
            received_at_ns=1,
            transform_version="calibration-v1",
            max_age_ns=10,
        ),
        image_metadata={
            "pixel_spec": spec.to_dict(),
            "calibration_sha256": canonical_calibration_sha256(geometry, "calibration-v1"),
        },
    )


class _FakeBackend:
    providers = ("FixtureExecutionProvider",)

    def __init__(self, output: np.ndarray) -> None:
        self.output = output
        self.provider: str | None = "FixtureExecutionProvider"
        self.input_name: str | None = "images"
        self.output_name: str | None = "output0"
        self.input_shape: tuple[int, ...] | None = (1, 3, 4, 4)
        self.output_shape: tuple[int, ...] | None = (1, 6, 2)
        self.calls = 0
        self.last_tensor: np.ndarray | None = None
        self.last_kwargs: dict[str, object] = {}

    def infer(self, tensor: np.ndarray, **kwargs: object) -> DetectorOutput:
        self.calls += 1
        self.last_tensor = tensor
        self.last_kwargs = kwargs
        return DetectorOutput(
            output=self.output,
            provider=self.provider,  # type: ignore[arg-type]
            input_name=self.input_name,  # type: ignore[arg-type]
            output_name=self.output_name,  # type: ignore[arg-type]
            input_shape=self.input_shape,  # type: ignore[arg-type]
            output_shape=self.output_shape,  # type: ignore[arg-type]
        )


def _adapter(
    binding: ModelBinding,
    store: PixelStore,
    backend: _FakeBackend,
    config: PreprocessConfig,
    *,
    clock=lambda: 1,
) -> ObservationAdapter:
    return ObservationAdapter(
        binding,
        store,
        backend,
        preprocess_config=config,
        calibration_sha256=canonical_calibration_sha256(config.geometry, "calibration-v1"),
        model_sha256=binding.model_sha256,
        classes_sha256=binding.classes_sha256,
        config_sha256=binding.config_sha256,
        clock=clock,
    )


def test_adapter_verifies_cas_preprocess_and_projects_deterministically(tmp_path) -> None:
    config = _config()
    binding = _binding(config)
    store = PixelStore(tmp_path / "pixels")
    frame = _frame(config, store)
    output = np.zeros(binding.output_shape, dtype=np.float32)
    output[0, :, 0] = (2.0, 2.0, 2.0, 2.0, 0.60, 0.80)
    output[0, :, 1] = (2.0, 2.0, 2.0, 2.0, 0.90, 0.10)
    backend = _FakeBackend(output)

    result = _adapter(binding, store, backend, config).observe(frame)
    assert result.succeeded
    assert result.observation is not None
    assert backend.calls == 1
    assert backend.last_tensor is not None
    assert backend.last_tensor.shape == (1, 3, 4, 4)
    assert backend.last_kwargs["provider"] == "FixtureExecutionProvider"
    assert [item.class_id for item in result.observation.detections] == [0, 1]
    assert [item.confidence for item in result.observation.detections] == pytest.approx([0.9, 0.8])
    assert result.observation.detections[0].box.to_dict()["left"] == 1.0

    repeated = _adapter(binding, store, backend, config).observe(frame)
    third = _adapter(binding, store, backend, config).observe(frame)
    assert repeated.digest == result.digest == third.digest


def test_adapter_precheck_faults_do_not_call_backend(tmp_path) -> None:
    config = _config()
    binding = _binding(config)
    store = PixelStore(tmp_path / "pixels")
    frame = _frame(config, store)
    backend = _FakeBackend(np.zeros(binding.output_shape, dtype=np.float32))
    adapter = ObservationAdapter(
        binding,
        store,
        backend,
        preprocess_config=config,
        calibration_sha256=canonical_calibration_sha256(config.geometry, "calibration-v1"),
        clock=lambda: 1,
        model_sha256="f" * 64,
        classes_sha256=binding.classes_sha256,
        config_sha256=binding.config_sha256,
    )

    result = adapter.observe(frame)

    assert not result.succeeded
    assert result.fault is not None
    assert result.fault.code is ObservationFaultCode.MODEL_HASH_MISMATCH
    assert result.plan_suppressed
    assert backend.calls == 0


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("image_ref", ObservationFaultCode.FRAME_LINEAGE_MISMATCH),
        ("calibration", ObservationFaultCode.CALIBRATION_MISMATCH),
        ("model_missing", ObservationFaultCode.MODEL_MISSING),
        ("model_conflict", ObservationFaultCode.MODEL_HASH_MISMATCH),
        ("classes", ObservationFaultCode.CLASSES_HASH_MISMATCH),
        ("config", ObservationFaultCode.MODEL_BINDING_MISMATCH),
        ("preprocess", ObservationFaultCode.PREPROCESS_ERROR),
        ("pixel_spec", ObservationFaultCode.PIXEL_HASH_MISMATCH),
    ],
)
def test_adapter_lineage_and_binding_fault_matrix_stops_before_backend(
    tmp_path,
    case: str,
    expected_code: ObservationFaultCode,
) -> None:
    config = _config()
    binding = _binding(config)
    store = PixelStore(tmp_path / "pixels")
    frame = _frame(config, store)
    backend = _FakeBackend(np.zeros(binding.output_shape, dtype=np.float32))
    calibration = canonical_calibration_sha256(config.geometry, "calibration-v1")
    kwargs: dict[str, object] = {
        "preprocess_config": config,
        "calibration_sha256": calibration,
        "model_sha256": binding.model_sha256,
        "classes_sha256": binding.classes_sha256,
        "config_sha256": binding.config_sha256,
        "clock": lambda: 1,
    }
    if case == "image_ref":
        frame = replace(frame, image_ref="cas://sha256/" + "f" * 64)
    elif case == "calibration":
        kwargs["calibration_sha256"] = "f" * 64
    elif case == "model_missing":
        kwargs["model_sha256"] = None
    elif case == "model_conflict":
        frame = replace(
            frame,
            image_metadata={**dict(frame.image_metadata), "model_sha256": "f" * 64},
        )
    elif case == "classes":
        kwargs["classes_sha256"] = "f" * 64
    elif case == "config":
        kwargs["config_sha256"] = "f" * 64
    elif case == "preprocess":
        binding = replace(binding, preprocess_sha256="f" * 64)
    elif case == "pixel_spec":
        frame = replace(
            frame,
            image_metadata={
                **dict(frame.image_metadata),
                "pixel_spec": PixelSpec(width=1, height=1).to_dict(),
            },
        )

    result = ObservationAdapter(binding, store, backend, **kwargs).observe(frame)  # type: ignore[arg-type]

    assert result.fault is not None
    assert result.fault.code is expected_code
    assert result.plan_suppressed
    assert backend.calls == 0


def test_adapter_rejects_provider_and_output_drift(tmp_path) -> None:
    config = _config()
    binding = _binding(config)
    store = PixelStore(tmp_path / "pixels")
    frame = _frame(config, store)
    backend = _FakeBackend(np.zeros((1, 6, 1), dtype=np.float32))
    # The secondary CPU provider is bound but must not be selected silently.
    backend.providers = ("CPUExecutionProvider",)
    result = _adapter(binding, store, backend, config).observe(frame)
    assert result.fault is not None
    assert result.fault.code is ObservationFaultCode.PROVIDER_UNAVAILABLE
    assert backend.calls == 0

    backend.providers = ("FixtureExecutionProvider",)
    result = ObservationAdapter(
        binding,
        store,
        backend,
        preprocess_config=config,
        clock=lambda: 1,
        model_sha256=binding.model_sha256,
        classes_sha256=binding.classes_sha256,
        config_sha256=binding.config_sha256,
    ).observe(frame)
    assert result.fault is not None
    assert result.fault.code is ObservationFaultCode.OUTPUT_SHAPE_MISMATCH
    assert backend.calls == 1

    input_backend = _FakeBackend(np.zeros(binding.output_shape, dtype=np.float32))
    input_backend.input_shape = (1, 3, 2, 2)
    result = _adapter(binding, store, input_backend, config).observe(frame)
    assert result.fault is not None
    assert result.fault.code is ObservationFaultCode.INPUT_SHAPE_MISMATCH

    unattested_backend = _FakeBackend(np.zeros(binding.output_shape, dtype=np.float32))
    unattested_backend.provider = None
    result = _adapter(binding, store, unattested_backend, config).observe(frame)
    assert result.fault is not None
    assert result.fault.code is ObservationFaultCode.PROVIDER_UNAVAILABLE


def test_stale_and_missing_cas_are_suppressed_before_inference(tmp_path) -> None:
    config = _config()
    binding = _binding(config)
    store = PixelStore(tmp_path / "pixels")
    frame = _frame(config, store)
    backend = _FakeBackend(np.zeros(binding.output_shape, dtype=np.float32))

    stale = _adapter(binding, store, backend, config, clock=lambda: 11).observe(frame)
    assert stale.fault is not None
    assert stale.fault.code is ObservationFaultCode.FRAME_STALE
    assert backend.calls == 0

    missing_store = PixelStore(tmp_path / "missing")
    missing = _adapter(binding, missing_store, backend, config).observe(frame)
    assert missing.fault is not None
    assert missing.fault.code is ObservationFaultCode.PIXEL_MISSING
    assert backend.calls == 0


def test_frame_that_expires_during_inference_is_suppressed(tmp_path) -> None:
    config = _config()
    binding = _binding(config)
    store = PixelStore(tmp_path / "pixels")
    frame = _frame(config, store)
    backend = _FakeBackend(np.zeros(binding.output_shape, dtype=np.float32))
    ticks = iter((1, 11))

    result = _adapter(binding, store, backend, config, clock=lambda: next(ticks)).observe(frame)

    assert result.fault is not None
    assert result.fault.code is ObservationFaultCode.FRAME_STALE
    assert result.plan_suppressed
    assert backend.calls == 1


def test_empty_low_confidence_anchors_and_nonfinite_outputs_fail_closed(tmp_path) -> None:
    config = _config()
    binding = _binding(config)
    store = PixelStore(tmp_path / "pixels")
    frame = _frame(config, store)

    empty_backend = _FakeBackend(np.zeros(binding.output_shape, dtype=np.float32))
    empty = _adapter(binding, store, empty_backend, config).observe(frame)
    assert empty.succeeded
    assert empty.observation is not None
    assert empty.observation.detections == ()

    nonfinite_output = np.zeros(binding.output_shape, dtype=np.float32)
    nonfinite_output[0, 4, 0] = np.nan
    nonfinite_backend = _FakeBackend(nonfinite_output)
    nonfinite = _adapter(binding, store, nonfinite_backend, config).observe(frame)
    assert nonfinite.fault is not None
    assert nonfinite.fault.code is ObservationFaultCode.INFERENCE_ERROR
    assert nonfinite_backend.calls == 1


def test_backend_exception_is_a_suppressed_inference_fault(tmp_path) -> None:
    config = _config()
    binding = _binding(config)
    store = PixelStore(tmp_path / "pixels")
    frame = _frame(config, store)

    class FailingBackend(_FakeBackend):
        def infer(self, tensor: np.ndarray, **kwargs: object) -> DetectorOutput:
            self.calls += 1
            raise RuntimeError(" ")

    backend = FailingBackend(np.zeros(binding.output_shape, dtype=np.float32))
    result = _adapter(binding, store, backend, config).observe(frame)

    assert result.fault is not None
    assert result.fault.code is ObservationFaultCode.INFERENCE_ERROR
    assert result.fault.message == ObservationFaultCode.INFERENCE_ERROR.value
    assert result.plan_suppressed
    assert backend.calls == 1


def test_hostile_provider_metadata_still_returns_a_json_safe_fault(tmp_path) -> None:
    config = _config()
    binding = _binding(config)
    store = PixelStore(tmp_path / "pixels")
    frame = _frame(config, store)
    backend = _FakeBackend(np.zeros(binding.output_shape, dtype=np.float32))
    backend.providers = (object(),)  # type: ignore[assignment]

    result = _adapter(binding, store, backend, config).observe(frame)

    assert result.fault is not None
    assert result.fault.code is ObservationFaultCode.PROVIDER_UNAVAILABLE
    assert result.plan_suppressed
