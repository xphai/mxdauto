from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from maple_automation_core.domain import (
    WORKING_COORDINATE_SPACE,
    Detection,
    DetectionBox,
    FrameSize,
    ModelBinding,
    Observation,
    ObservationFault,
    ObservationFaultCode,
    ObservationResult,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
WORKING_SIZE = FrameSize(width=1296, height=700)


def _binding() -> ModelBinding:
    return ModelBinding(
        release_id="g1-obs-fixture-v1",
        model_id="best_forest_v3-candidate",
        model_sha256=SHA_A.upper(),
        classes=("mob",),
        classes_sha256=SHA_B.upper(),
        config_sha256=SHA_C.upper(),
        preprocess_version="g1-obs-preprocess-v1",
        preprocess_sha256=SHA_D.upper(),
        input_name="images",
        input_size=FrameSize(width=640, height=640),
        output_name="output0",
        output_shape=(1, 5, 8400),
        requested_providers=("FixtureExecutionProvider", "CPUExecutionProvider"),
        detection_confidence=0.25,
        iou_threshold=0.45,
        roi=(0.04, 0.0, 0.98, 0.84),
    )


def _detection(
    confidence: float = 0.75,
    *,
    left: float = 10,
    top: float = 20,
) -> Detection:
    return Detection(
        class_id=0,
        class_name="mob",
        confidence=confidence,
        box=DetectionBox(
            left=left,
            top=top,
            right=left + 30,
            bottom=top + 40,
            working_size=WORKING_SIZE,
        ),
    )


def _observation(*detections: Detection) -> Observation:
    return Observation(
        session_id="session-1",
        source_id="capture-card-primary",
        frame_id=17,
        captured_at_ns=1_000,
        observed_at_ns=1_100,
        pixel_digest=SHA_A.upper(),
        image_ref="cas://frame-17",
        calibration_sha256=SHA_B.upper(),
        working_size=WORKING_SIZE,
        model_binding=_binding(),
        execution_provider="FixtureExecutionProvider",
        detections=tuple(detections),
    )


def _fault() -> ObservationFault:
    return ObservationFault(
        session_id="session-1",
        source_id="capture-card-primary",
        frame_id=17,
        captured_at_ns=1_000,
        failed_at_ns=1_090,
        pixel_digest=SHA_A.upper(),
        image_ref="cas://frame-17",
        calibration_sha256=SHA_B.upper(),
        working_size=WORKING_SIZE,
        model_binding=_binding(),
        code=ObservationFaultCode.MODEL_HASH_MISMATCH,
        message="model hash does not match the frozen binding",
        details={"expected": SHA_A, "observed": SHA_B, "stages": ["load", "verify"]},
    )


def _json_roundtrip(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def test_detection_box_is_working_space_bounded_immutable_and_roundtrips() -> None:
    box = _detection().box

    assert box.coordinate_space == WORKING_COORDINATE_SPACE
    assert box.width == 30.0
    assert box.height == 40.0
    assert DetectionBox.from_dict(_json_roundtrip(box.to_dict())) == box  # type: ignore[arg-type]
    assert len(box.digest) == 64
    with pytest.raises(FrozenInstanceError):
        box.left = 0  # type: ignore[misc]

    missing_space = box.to_dict()
    missing_space.pop("coordinate_space")
    with pytest.raises(ValueError, match="coordinate_space"):
        DetectionBox.from_dict(missing_space)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"left": -1}, "left/top"),
        ({"right": 10}, "positive width"),
        ({"bottom": 701}, "working_size"),
        ({"left": float("nan")}, "finite"),
        ({"right": True}, "finite"),
    ],
)
def test_detection_box_rejects_invalid_geometry(
    updates: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "left": 10,
        "top": 20,
        "right": 40,
        "bottom": 60,
        "working_size": WORKING_SIZE,
    }
    values.update(updates)
    with pytest.raises(ValueError, match=message):
        DetectionBox(**values)  # type: ignore[arg-type]


def test_detection_validates_class_confidence_box_and_roundtrips() -> None:
    detection = _detection()
    assert Detection.from_dict(_json_roundtrip(detection.to_dict())) == detection  # type: ignore[arg-type]
    assert len(detection.digest) == 64

    with pytest.raises(ValueError, match="class_id"):
        replace(detection, class_id=-1)
    with pytest.raises(ValueError, match="confidence"):
        replace(detection, confidence=1.01)
    with pytest.raises(TypeError, match="DetectionBox"):
        replace(detection, box=object())  # type: ignore[arg-type]


def test_model_binding_is_exact_roundtrippable_and_hash_normalized() -> None:
    binding = _binding()
    assert binding.model_sha256 == SHA_A
    assert binding.classes_sha256 == SHA_B
    assert binding.config_sha256 == SHA_C
    assert binding.preprocess_sha256 == SHA_D
    assert binding.output_shape == (1, 5, 8400)
    assert ModelBinding.from_dict(_json_roundtrip(binding.to_dict())) == binding  # type: ignore[arg-type]
    assert len(binding.digest) == 64


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("classes", ("mob", "mob"), "unique"),
        ("output_shape", (2, 5, 8400), "batch"),
        ("output_shape", (1, 6, 8400), "feature"),
        ("requested_providers", ("CPUExecutionProvider", "CPUExecutionProvider"), "unique"),
        ("roi", (0.5, 0.0, 0.5, 1.0), "positive"),
        ("roi", (-0.1, 0.0, 1.0, 1.0), "normalized"),
        ("detection_confidence", float("inf"), "between 0 and 1"),
    ],
)
def test_model_binding_rejects_ambiguous_or_mismatched_contracts(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_binding(), **{field: value})


def test_model_binding_requires_immutable_tuple_boundaries() -> None:
    with pytest.raises(TypeError, match="classes"):
        replace(_binding(), classes=["mob"])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="output_shape"):
        replace(_binding(), output_shape=[1, 5, 8400])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="requested_providers"):
        replace(
            _binding(),
            requested_providers=["CPUExecutionProvider"],  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="roi"):
        replace(_binding(), roi=[0.0, 0.0, 1.0, 1.0])  # type: ignore[arg-type]


def test_observation_canonicalizes_detection_order_and_digest() -> None:
    lower_confidence = _detection(0.5, left=100)
    higher_confidence = _detection(0.9, left=200)
    first = _observation(lower_confidence, higher_confidence)
    second = _observation(higher_confidence, lower_confidence)

    assert first.detections == (higher_confidence, lower_confidence)
    assert first.to_dict() == second.to_dict()
    assert first.digest == second.digest
    assert first.detection_digest == second.detection_digest
    assert first.pixel_digest == SHA_A
    assert first.calibration_sha256 == SHA_B
    assert first.plan_suppressed is False
    assert Observation.from_dict(_json_roundtrip(first.to_dict())) == first  # type: ignore[arg-type]


def test_observation_enforces_bound_class_threshold_space_and_provider() -> None:
    valid = _observation(_detection())
    with pytest.raises(ValueError, match="class_id"):
        replace(valid, detections=(replace(_detection(), class_id=1),))
    with pytest.raises(ValueError, match="class_name"):
        replace(valid, detections=(replace(_detection(), class_name="player"),))
    with pytest.raises(ValueError, match="below"):
        replace(valid, detections=(_detection(0.24),))
    different_space = replace(
        _detection(),
        box=replace(_detection().box, working_size=FrameSize(width=640, height=640)),
    )
    with pytest.raises(ValueError, match="working_size"):
        replace(valid, detections=(different_space,))
    with pytest.raises(ValueError, match="requested_providers"):
        replace(valid, execution_provider="UnboundExecutionProvider")


def test_observation_rejects_invalid_frame_lineage_and_mutable_detection_list() -> None:
    valid = _observation()
    with pytest.raises(ValueError, match="observed_at_ns"):
        replace(valid, observed_at_ns=999)
    with pytest.raises(ValueError, match="pixel_digest"):
        replace(valid, pixel_digest="not-a-digest")
    with pytest.raises(TypeError, match="tuple"):
        replace(valid, detections=[])  # type: ignore[arg-type]


def test_fault_is_fail_closed_immutable_and_roundtrips() -> None:
    fault = _fault()
    assert fault.plan_suppressed is True
    assert fault.pixel_digest == SHA_A
    assert fault.calibration_sha256 == SHA_B
    assert fault.details["stages"] == ("load", "verify")
    with pytest.raises(TypeError):
        fault.details["new"] = True  # type: ignore[index]
    assert ObservationFault.from_dict(_json_roundtrip(fault.to_dict())) == fault  # type: ignore[arg-type]
    assert len(fault.digest) == 64


def test_fault_rejects_unbounded_code_invalid_details_and_bad_time() -> None:
    fault = _fault()
    with pytest.raises(TypeError, match="ObservationFaultCode"):
        replace(fault, code="model_hash_mismatch")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="JSON-serializable"):
        replace(fault, details={"bad": object()})
    with pytest.raises(ValueError, match="failed_at_ns"):
        replace(fault, failed_at_ns=999)
    payload = fault.to_dict()
    payload["code"] = "silent_fallback"
    with pytest.raises(ValueError):
        ObservationFault.from_dict(payload)


def test_result_success_and_fault_are_mutually_exclusive_and_deterministic() -> None:
    observation = _observation(_detection())
    success = ObservationResult(observation=observation)
    failure = ObservationResult(fault=_fault())

    assert success.succeeded is True
    assert success.plan_suppressed is False
    assert success.session_id == "session-1"
    assert success.frame_id == 17
    assert failure.succeeded is False
    assert failure.plan_suppressed is True
    assert ObservationResult.from_dict(_json_roundtrip(success.to_dict())) == success  # type: ignore[arg-type]
    assert ObservationResult.from_dict(_json_roundtrip(failure.to_dict())) == failure  # type: ignore[arg-type]
    assert len(success.digest) == 64

    with pytest.raises(ValueError, match="exactly one"):
        ObservationResult()
    with pytest.raises(ValueError, match="exactly one"):
        ObservationResult(observation=observation, fault=_fault())


def test_result_rejects_claims_that_contradict_the_branch() -> None:
    payload = ObservationResult(observation=_observation()).to_dict()
    payload["status"] = "fault"
    with pytest.raises(ValueError, match="status"):
        ObservationResult.from_dict(payload)

    payload = ObservationResult(fault=_fault()).to_dict()
    payload["plan_suppressed"] = False
    with pytest.raises(ValueError, match="plan_suppressed"):
        ObservationResult.from_dict(payload)
