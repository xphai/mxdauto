from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from maple_automation_core.domain.coordinates import WorldCoordinate
from maple_automation_core.domain.frame import FrameSize
from maple_automation_core.domain.observation import (
    Detection,
    DetectionBox,
    ModelBinding,
    Observation,
    ObservationFault,
    ObservationFaultCode,
    ObservationResult,
)
from maple_automation_core.domain.player_world import PlayerState, Visibility
from maple_automation_core.localization.platform import PlatformGraph, PlatformSegment
from maple_automation_core.localization.player_localizer import (
    IdentityStatus,
    LocalizationFaultCode,
    LocalizationPolicy,
    LocalizationResult,
    LocalizationStatus,
    LocationState,
    PlayerAnchorSource,
    PlayerCandidate,
    PlayerLocation,
    WorkingPoint,
    resolve_player_location,
)
from maple_automation_core.localization.transform import LocalizationTransform

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SIZE = FrameSize(width=100, height=80)


def _binding() -> ModelBinding:
    return ModelBinding(
        release_id="candidate-core-v2-20260829-shadow",
        model_id="mob-only",
        model_sha256=SHA_A,
        classes=("mob",),
        classes_sha256=SHA_B,
        config_sha256=SHA_C,
        preprocess_version="v1",
        preprocess_sha256=SHA_D,
        input_name="images",
        input_size=FrameSize(width=640, height=640),
        output_name="output0",
        output_shape=(1, 5, 8400),
        requested_providers=("FixtureExecutionProvider",),
        detection_confidence=0.25,
        iou_threshold=0.45,
        roi=(0.0, 0.0, 1.0, 1.0),
    )


def _observation(
    *,
    frame_id: int = 7,
    observed_at_ns: int = 1_100,
    source_id: str = "capture-card-primary",
) -> Observation:
    return Observation(
        session_id="session-1",
        source_id=source_id,
        frame_id=frame_id,
        captured_at_ns=observed_at_ns - 100,
        observed_at_ns=observed_at_ns,
        pixel_digest=SHA_A,
        image_ref=f"cas://frame-{frame_id}",
        calibration_sha256=SHA_B,
        working_size=SIZE,
        model_binding=_binding(),
        execution_provider="FixtureExecutionProvider",
        detections=(
            Detection(
                class_id=0,
                class_name="mob",
                confidence=0.9,
                box=DetectionBox(
                    left=5,
                    top=5,
                    right=15,
                    bottom=15,
                    working_size=SIZE,
                ),
            ),
        ),
    )


def _fault() -> ObservationFault:
    observation = _observation()
    return ObservationFault(
        session_id=observation.session_id,
        source_id=observation.source_id,
        frame_id=observation.frame_id,
        captured_at_ns=observation.captured_at_ns,
        failed_at_ns=observation.observed_at_ns,
        pixel_digest=observation.pixel_digest,
        image_ref=observation.image_ref,
        calibration_sha256=observation.calibration_sha256,
        working_size=observation.working_size,
        model_binding=observation.model_binding,
        code=ObservationFaultCode.INFERENCE_ERROR,
        message="fixture inference fault",
    )


def _transform(*, map_id: str = "map-1", calibration: str = SHA_B) -> LocalizationTransform:
    return LocalizationTransform(
        map_id=map_id,
        map_fingerprint_sha256=SHA_E,
        profile_id="pilot-profile",
        transform_version="loc-affine-v1",
        calibration_sha256=calibration,
        working_size=SIZE,
        matrix=((2.0, 0.0, -10.0), (0.0, 2.0, -20.0)),
    )


def _graph(*, map_id: str = "map-1") -> PlatformGraph:
    return PlatformGraph(
        map_id=map_id,
        map_fingerprint_sha256=SHA_E,
        graph_version="platform-v1",
        platforms=(
            PlatformSegment(
                platform_id="ground",
                start=WorldCoordinate(-20, 20),
                end=WorldCoordinate(100, 20),
            ),
        ),
        ambiguity_margin=0.25,
        max_vertical_distance=3.0,
        max_horizontal_distance=5.0,
    )


def _policy() -> LocalizationPolicy:
    return LocalizationPolicy(
        subject_id="pilot-subject-01",
        minimum_confidence=0.8,
        maximum_freshness_ns=1_000,
    )


def _candidate(
    *,
    frame_id: int = 7,
    observed_at_ns: int = 1_100,
    generation: int = 0,
    confidence: float = 0.95,
    subject_id: str = "pilot-subject-01",
    x: float = 20.0,
    y: float = 20.0,
    calibration: str = SHA_B,
    pixel_digest: str = SHA_A,
    source_id: str = "capture-card-primary",
) -> PlayerCandidate:
    return PlayerCandidate(
        session_id="session-1",
        source_id=source_id,
        source_frame_id=frame_id,
        observed_at_ns=observed_at_ns,
        generation=generation,
        subject_id=subject_id,
        confidence=confidence,
        visibility=Visibility.VISIBLE,
        evidence_source=PlayerAnchorSource.REPLAY_FIXTURE,
        evidence_digest=SHA_C,
        pixel_digest=pixel_digest,
        calibration_sha256=calibration,
        working_size=SIZE,
        anchor_working=WorkingPoint(x=x, y=y, working_size=SIZE),
    )


def _resolve(
    *,
    observation: ObservationResult | None = None,
    candidates: tuple[PlayerCandidate, ...] | None = None,
    transform: LocalizationTransform | None = None,
    graph: PlatformGraph | None = None,
    policy: LocalizationPolicy | None = None,
    previous: LocationState | None = None,
    as_of_ns: int = 1_200,
) -> tuple[LocalizationResult, LocationState | None]:
    return resolve_player_location(
        observation=(
            ObservationResult(observation=_observation()) if observation is None else observation
        ),
        candidates=(_candidate(),) if candidates is None else candidates,
        transform=_transform() if transform is None else transform,
        platform_graph=_graph() if graph is None else graph,
        policy=_policy() if policy is None else policy,
        previous=previous,
        as_of_ns=as_of_ns,
    )


def _json_roundtrip(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def test_localizes_independent_anchor_and_roundtrips_every_contract() -> None:
    result, state = _resolve()

    assert result.succeeded
    assert result.location is not None
    location = result.location
    assert location.status is LocalizationStatus.LOCATED
    assert location.as_of_ns == 1_200
    assert location.freshness_ns == location.as_of_ns - location.observed_at_ns
    assert location.identity_status is IdentityStatus.CONFIRMED
    assert location.world_position == WorldCoordinate(30, 20)
    assert location.platform_id == "ground"
    assert location.plan_suppressed is False
    assert location.observation_digest == _observation().digest
    assert location.transform_digest == _transform().digest
    assert state is not None
    assert state.identity_switch_count == 0
    assert state.subject_id == "pilot-subject-01"
    assert state.last_as_of_ns == location.as_of_ns
    assert state.last_checked_as_of_ns == location.as_of_ns
    assert state.source_id == location.source_id
    assert state.last_identity_status is location.identity_status
    assert state.transform_digest == location.transform_digest
    assert state.transform_version == location.transform_version
    assert state.platform_graph_digest == _graph().digest
    assert state.platform_graph_version == _graph().graph_version

    player = location.to_player_state()
    assert isinstance(player, PlayerState)
    assert player.position is not None and player.position.to_dict() == {"x": 20, "y": 20}
    assert PlayerState.from_dict(player.to_dict()) == player
    assert WorkingPoint.from_dict(_json_roundtrip(location.anchor_working.to_dict())) == (
        location.anchor_working
    )
    assert PlayerCandidate.from_dict(_json_roundtrip(_candidate().to_dict())) == _candidate()
    assert PlayerLocation.from_dict(_json_roundtrip(location.to_dict())) == location
    assert LocalizationResult.from_dict(_json_roundtrip(result.to_dict())) == result
    assert LocationState.from_dict(_json_roundtrip(state.to_dict())) == state


@pytest.mark.parametrize("candidate_mode", ["missing", "low-confidence", "lost"])
def test_missing_low_confidence_and_lost_anchor_become_unknown(
    candidate_mode: str,
) -> None:
    if candidate_mode == "missing":
        candidates: tuple[PlayerCandidate, ...] = ()
    elif candidate_mode == "low-confidence":
        candidates = (replace(_candidate(), confidence=0.2),)
    else:
        candidates = (
            replace(
                _candidate(),
                visibility=Visibility.LOST,
                confidence=0.0,
                anchor_working=None,
            ),
        )

    result, state = _resolve(candidates=candidates)

    assert result.location is not None
    assert result.location.status is LocalizationStatus.UNKNOWN
    assert result.location.world_position is None
    assert result.location.platform_id is None
    assert result.plan_suppressed
    assert state is not None


def test_multiple_viable_anchors_are_ambiguous_independent_of_order() -> None:
    first = _candidate(x=20, y=20, confidence=0.95)
    second = _candidate(x=21, y=20, confidence=0.94)

    left, _ = _resolve(candidates=(first, second))
    right, _ = _resolve(candidates=(second, first))

    assert left.location is not None
    assert left.location.identity_status is IdentityStatus.AMBIGUOUS
    assert left.location.status is LocalizationStatus.UNKNOWN
    assert left.digest == right.digest


def test_upstream_fault_stale_and_lineage_drift_fail_closed() -> None:
    upstream, state = _resolve(observation=ObservationResult(fault=_fault()))
    assert upstream.fault is not None
    assert upstream.fault.code is LocalizationFaultCode.OBSERVATION_FAULT
    assert state is None

    stale, _ = _resolve(as_of_ns=2_101)
    assert stale.fault is not None
    assert stale.fault.code is LocalizationFaultCode.STALE

    drifted, _ = _resolve(candidates=(replace(_candidate(), source_frame_id=8),))
    assert drifted.fault is not None
    assert drifted.fault.code is LocalizationFaultCode.LINEAGE_MISMATCH

    wrong_pixels, _ = _resolve(candidates=(_candidate(pixel_digest=SHA_D),))
    assert wrong_pixels.fault is not None
    assert wrong_pixels.fault.code is LocalizationFaultCode.LINEAGE_MISMATCH


def test_transform_graph_and_calibration_mismatch_fail_closed() -> None:
    wrong_graph, _ = _resolve(graph=_graph(map_id="other-map"))
    assert wrong_graph.fault is not None
    assert wrong_graph.fault.code is LocalizationFaultCode.PLATFORM_GRAPH_MISMATCH

    wrong_fingerprint, _ = _resolve(graph=replace(_graph(), map_fingerprint_sha256=SHA_D))
    assert wrong_fingerprint.fault is not None
    assert wrong_fingerprint.fault.code is LocalizationFaultCode.PLATFORM_GRAPH_MISMATCH

    wrong_transform, _ = _resolve(transform=_transform(calibration=SHA_D))
    assert wrong_transform.fault is not None
    assert wrong_transform.fault.code is LocalizationFaultCode.TRANSFORM_MISMATCH

    wrong_candidate, _ = _resolve(candidates=(replace(_candidate(), calibration_sha256=SHA_D),))
    assert wrong_candidate.fault is not None
    assert wrong_candidate.fault.code is LocalizationFaultCode.LINEAGE_MISMATCH


def test_temporal_monotonicity_generation_and_identity_switch_are_explicit() -> None:
    first, state = _resolve()
    assert first.location is not None and state is not None
    second_observation = ObservationResult(
        observation=_observation(frame_id=8, observed_at_ns=1_300)
    )
    second_candidate = _candidate(frame_id=8, observed_at_ns=1_300, generation=1)
    second, state2 = _resolve(
        observation=second_observation,
        candidates=(second_candidate,),
        previous=state,
        as_of_ns=1_400,
    )
    assert second.location is not None and state2 is not None
    assert state2.generation == 1

    duplicate, unchanged = _resolve(previous=state2, as_of_ns=1_500)
    assert duplicate.fault is not None
    assert duplicate.fault.code is LocalizationFaultCode.OUT_OF_ORDER
    assert unchanged is not None
    assert unchanged.last_location == state2.last_location
    assert unchanged.last_checked_as_of_ns == 1_500

    generation, _ = _resolve(
        observation=ObservationResult(observation=_observation(frame_id=9, observed_at_ns=1_600)),
        candidates=(_candidate(frame_id=9, observed_at_ns=1_600, generation=7),),
        previous=state2,
        as_of_ns=1_700,
    )
    assert generation.fault is not None
    assert generation.fault.code is LocalizationFaultCode.GENERATION_DRIFT

    switched, switched_state = _resolve(
        observation=ObservationResult(observation=_observation(frame_id=9, observed_at_ns=1_600)),
        candidates=(
            _candidate(
                frame_id=9,
                observed_at_ns=1_600,
                generation=2,
                subject_id="unexpected-subject",
            ),
        ),
        previous=state2,
        as_of_ns=1_700,
    )
    assert switched.fault is not None
    assert switched.fault.code is LocalizationFaultCode.IDENTITY_SWITCH
    assert switched_state is not None
    assert switched_state.identity_switch_count == 1


def test_as_of_is_strictly_monotonic_within_a_session() -> None:
    first, state = _resolve()
    assert first.location is not None and state is not None

    result, unchanged = _resolve(
        observation=ObservationResult(observation=_observation(frame_id=8, observed_at_ns=1_150)),
        candidates=(_candidate(frame_id=8, observed_at_ns=1_150),),
        previous=state,
        as_of_ns=1_200,
    )
    assert result.fault is not None
    assert result.fault.code is LocalizationFaultCode.OUT_OF_ORDER
    assert unchanged == state


def test_failed_request_advances_clock_fence_and_blocks_rollback() -> None:
    first, state = _resolve()
    assert first.location is not None and state is not None
    next_observation = ObservationResult(observation=_observation(frame_id=8, observed_at_ns=1_300))
    next_candidate = _candidate(frame_id=8, observed_at_ns=1_300, generation=1)

    stale, fenced = _resolve(
        observation=next_observation,
        candidates=(next_candidate,),
        previous=state,
        as_of_ns=3_000,
    )
    assert stale.fault is not None
    assert stale.fault.code is LocalizationFaultCode.STALE
    assert fenced is not None
    assert fenced.last_checked_as_of_ns == 3_000

    rollback, unchanged = _resolve(
        observation=next_observation,
        candidates=(next_candidate,),
        previous=fenced,
        as_of_ns=1_400,
    )
    assert rollback.fault is not None
    assert rollback.fault.code is LocalizationFaultCode.OUT_OF_ORDER
    assert unchanged == fenced


def test_source_switch_requires_explicit_reset() -> None:
    first, state = _resolve()
    assert first.location is not None and state is not None
    source_id = "capture-card-secondary"

    switched, fenced = _resolve(
        observation=ObservationResult(
            observation=_observation(
                frame_id=8,
                observed_at_ns=1_300,
                source_id=source_id,
            )
        ),
        candidates=(
            _candidate(
                frame_id=8,
                observed_at_ns=1_300,
                generation=1,
                source_id=source_id,
            ),
        ),
        previous=state,
        as_of_ns=1_400,
    )
    assert switched.fault is not None
    assert switched.fault.code is LocalizationFaultCode.LINEAGE_MISMATCH
    assert fenced is not None
    assert fenced.source_id == "capture-card-primary"
    assert fenced.last_checked_as_of_ns == 1_400


def test_same_session_transform_or_graph_switch_requires_reset() -> None:
    first, state = _resolve()
    assert first.location is not None and state is not None

    changed_transform = replace(_transform(), transform_version="loc-affine-v2")
    transform_result, transform_state = _resolve(
        observation=ObservationResult(observation=_observation(frame_id=8, observed_at_ns=1_300)),
        candidates=(_candidate(frame_id=8, observed_at_ns=1_300),),
        transform=changed_transform,
        previous=state,
        as_of_ns=1_400,
    )
    assert transform_result.fault is not None
    assert transform_result.fault.code is LocalizationFaultCode.TRANSFORM_MISMATCH
    assert transform_state is not None
    assert transform_state.last_location == state.last_location
    assert transform_state.last_checked_as_of_ns == 1_400

    changed_graph = replace(_graph(), graph_version="platform-v2")
    graph_result, graph_state = _resolve(
        observation=ObservationResult(observation=_observation(frame_id=8, observed_at_ns=1_300)),
        candidates=(_candidate(frame_id=8, observed_at_ns=1_300),),
        graph=changed_graph,
        previous=state,
        as_of_ns=1_400,
    )
    assert graph_result.fault is not None
    assert graph_result.fault.code is LocalizationFaultCode.PLATFORM_GRAPH_MISMATCH
    assert graph_state is not None
    assert graph_state.last_location == state.last_location
    assert graph_state.last_checked_as_of_ns == 1_400

    reset_result, reset_state = _resolve(transform=changed_transform)
    assert reset_result.succeeded
    assert reset_state is not None
    assert reset_state.transform_version == "loc-affine-v2"


def test_previous_policy_subject_mismatch_is_identity_switch_fault() -> None:
    first, state = _resolve()
    assert first.location is not None and state is not None

    result, switched = _resolve(
        policy=replace(_policy(), subject_id="another-subject"),
        previous=state,
        observation=ObservationResult(observation=_observation(frame_id=8, observed_at_ns=1_300)),
        candidates=(_candidate(frame_id=8, observed_at_ns=1_300),),
        as_of_ns=1_400,
    )
    assert result.fault is not None
    assert result.fault.code is LocalizationFaultCode.IDENTITY_SWITCH
    assert switched is not None
    assert switched.identity_switch_count == state.identity_switch_count + 1


def test_location_and_state_bind_temporal_identity_and_provenance_fields() -> None:
    result, state = _resolve()
    assert result.location is not None and state is not None
    location = result.location

    with pytest.raises(ValueError, match="freshness_ns"):
        PlayerLocation.from_dict({**location.to_dict(), "freshness_ns": 999})
    with pytest.raises(ValueError, match="as_of_ns"):
        PlayerLocation.from_dict({**location.to_dict(), "as_of_ns": 1_199})
    with pytest.raises(ValueError, match="observed_at_ns"):
        replace(
            state,
            last_location=replace(
                location,
                observed_at_ns=1_101,
                as_of_ns=1_201,
            ),
        )
    with pytest.raises(ValueError, match="identity_status"):
        replace(state, last_identity_status=IdentityStatus.AMBIGUOUS)
    with pytest.raises(ValueError, match="transform_version"):
        replace(state, transform_version="forged-transform")
    with pytest.raises(ValueError, match="unconfirmed identity"):
        replace(
            location,
            status=LocalizationStatus.UNKNOWN,
            identity_status=IdentityStatus.UNKNOWN,
            platform_id=None,
            anchor_working=None,
            world_position=None,
        )
    with pytest.raises(ValueError, match="unknown identity"):
        replace(
            state,
            last_location=None,
            subject_id=None,
            last_identity_status=IdentityStatus.CONFIRMED,
        )
    with pytest.raises(ValueError, match="last_checked_as_of_ns"):
        replace(state, last_checked_as_of_ns=state.last_as_of_ns - 1)
    with pytest.raises(ValueError, match="source"):
        replace(state, source_id="capture-card-secondary")


def test_platform_unknown_degrades_and_replay_digest_is_stable() -> None:
    far = _candidate(y=30)
    result, _ = _resolve(candidates=(far,))
    assert result.location is not None
    assert result.succeeded is False
    assert result.to_dict()["status"] == "location"
    assert result.location.status is LocalizationStatus.DEGRADED
    assert result.location.world_position == WorldCoordinate(30, 40)
    assert result.location.platform_id is None
    assert result.plan_suppressed
    assert result.location.to_player_state() is None

    digests = {_resolve()[0].digest for _ in range(100)}
    assert len(digests) == 1


def test_working_point_uses_half_open_bounds_and_candidate_is_strict() -> None:
    assert WorkingPoint(0, 0, SIZE).to_dict()["x"] == 0.0
    with pytest.raises(ValueError, match="half-open"):
        WorkingPoint(SIZE.width, 0, SIZE)
    with pytest.raises(ValueError, match="half-open"):
        WorkingPoint(0, SIZE.height, SIZE)
    with pytest.raises(ValueError, match="lost"):
        replace(_candidate(), visibility=Visibility.LOST)
    with pytest.raises(TypeError, match="non-lost"):
        replace(_candidate(), anchor_working=None)
    with pytest.raises(ValueError, match="evidence_digest"):
        replace(_candidate(), evidence_digest="forged")
