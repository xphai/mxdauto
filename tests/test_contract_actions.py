from __future__ import annotations

import pytest

from maple_automation_core.domain.actions import (
    ActionHandle,
    ActionKind,
    ActionReference,
    ActionResult,
    ActionSpec,
    ActionTermination,
)


def _spec() -> ActionSpec:
    return ActionSpec(
        session_id="session-1",
        action_id="act-1",
        kind=ActionKind.MOVE,
        requested_at_ns=1_000,
        timeout_ns=5_000,
        origin_frame_id=10,
        origin_world_state_version=1,
        payload={"dx": 1, "dy": 0},
        evidence={"source": "planner"},
    )


def test_action_spec_handle_result_roundtrip() -> None:
    spec = _spec()
    handle = ActionHandle(
        handle_id="h1",
        session_id="session-1",
        spec=spec,
        issued_at_ns=1_000,
        expires_at_ns=4_900,
        generation=1,
        started_at_ns=1_100,
        evidence={"source": "runtime"},
    )
    result = ActionResult(
        handle_id=handle.handle_id,
        action_id=spec.action_id,
        session_id=handle.session_id,
        termination=ActionTermination.SUCCESS,
        started_at_ns=1_000,
        completed_at_ns=6_000,
        origin_frame_id=spec.origin_frame_id,
        origin_world_state_version=spec.origin_world_state_version,
        result_frame_id=12,
        result_world_state_version=2,
        generation=1,
        details={"ok": True},
        evidence={"frame": 12},
    )

    assert spec.to_dict()["kind"] == ActionKind.MOVE.value
    assert spec.deadline_ns == 6_000
    assert handle.is_expired(9_000)
    assert handle.expires_at_ns <= spec.deadline_ns
    assert ActionSpec.from_dict(spec.to_dict()) == spec
    assert ActionHandle.from_dict(handle.to_dict()) == handle
    assert ActionResult.from_dict(result.to_dict()) == result
    assert result.duration_ns == 5_000
    assert spec.origin == ActionReference("session-1", 10, 1)
    assert handle.action_id == spec.action_id
    assert handle.origin == spec.origin
    assert result.origin == spec.origin
    assert result.result == ActionReference("session-1", 12, 2)


def test_action_illegal_payload() -> None:
    with pytest.raises(ValueError):
        ActionSpec(
            session_id="session-1",
            action_id="a",
            kind=ActionKind.MOVE,
            requested_at_ns=1,
            timeout_ns=1,
            origin_frame_id=0,
            origin_world_state_version=1,
            payload={"a": {1, 2}},
        )


def test_action_expired_invalid_times() -> None:
    spec = _spec()
    with pytest.raises(ValueError):
        ActionHandle(
            handle_id="h1",
            session_id="session-1",
            spec=spec,
            issued_at_ns=1_000,
            expires_at_ns=6_500,
            generation=0,
        )


def test_action_invalid_lifecycle_order() -> None:
    spec = _spec()
    with pytest.raises(ValueError):
        ActionHandle(
            handle_id="h1",
            session_id="session-1",
            spec=spec,
            issued_at_ns=1_000,
            expires_at_ns=4_900,
            generation=0,
            started_at_ns=5_000,
        )


def test_action_handle_start_and_result_factory_bind_lifecycle() -> None:
    handle = ActionHandle(
        handle_id="h1",
        session_id="session-1",
        spec=_spec(),
        issued_at_ns=1_000,
        expires_at_ns=4_900,
        generation=4,
    )
    started = handle.start(1_100, evidence={"started": True})
    assert handle.started_at_ns is None
    assert started.is_started
    assert started.is_active_at(1_100)
    assert not started.is_active_at(4_900)
    result = ActionResult.from_handle(
        started,
        termination=ActionTermination.SUCCESS,
        completed_at_ns=2_000,
        result_frame_id=11,
        result_world_state_version=2,
    )
    result.validate_against(started)
    with pytest.raises(ValueError):
        started.start(1_200)


def test_action_payload_is_strict_and_deeply_immutable() -> None:
    payload = {"nested": {"items": [1, 2]}}
    spec = ActionSpec(
        session_id="s",
        action_id="a",
        kind=ActionKind.MOVE,
        requested_at_ns=0,
        timeout_ns=1,
        origin_frame_id=0,
        origin_world_state_version=0,
        payload=payload,
    )
    payload["nested"]["items"].append(3)
    assert spec.to_dict()["payload"] == {"nested": {"items": [1, 2]}}
    with pytest.raises(TypeError):
        spec.payload["new"] = 1  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        spec.payload["nested"]["items"].append(3)  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        ActionSpec(
            session_id="s",
            action_id="a",
            kind=ActionKind.MOVE,
            requested_at_ns=0,
            timeout_ns=1,
            origin_frame_id=0,
            origin_world_state_version=0,
            payload={"bad": float("nan")},
        )


def test_action_result_requires_newer_world_state() -> None:
    with pytest.raises(ValueError):
        ActionResult(
            handle_id="h1",
            action_id="a1",
            session_id="session-1",
            termination=ActionTermination.SUCCESS,
            started_at_ns=1_000,
            completed_at_ns=2_000,
            origin_frame_id=10,
            origin_world_state_version=2,
            result_frame_id=11,
            result_world_state_version=2,
            generation=1,
        )
