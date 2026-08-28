from __future__ import annotations

import pytest

from maple_automation_core.domain.actions import (
    ActionHandle,
    ActionKind,
    ActionResult,
    ActionSpec,
    ActionTermination,
)


def _spec() -> ActionSpec:
    return ActionSpec(
        session_id="s",
        action_id="a",
        kind=ActionKind.MOVE,
        requested_at_ns=10,
        timeout_ns=100,
        origin_frame_id=2,
        origin_world_state_version=3,
        payload={"nested": {"x": [1]}},
        evidence={"source": "test"},
    )


def _handle() -> ActionHandle:
    return ActionHandle(
        handle_id="h",
        session_id="s",
        spec=_spec(),
        issued_at_ns=10,
        expires_at_ns=90,
        generation=1,
    )


def test_action_spec_edges_and_strict_hydration() -> None:
    spec = _spec()
    assert spec.is_stale_at(110)
    assert spec.is_expired_at(110)
    assert not spec.is_stale_at(109)
    assert ActionSpec.from_dict(spec.to_dict()) == spec
    for invalid in (
        {"kind": ActionKind.MOVE},
        {**spec.to_dict(), "payload": []},
        {**spec.to_dict(), "requested_at_ns": "10"},
    ):
        with pytest.raises((ValueError, TypeError)):
            ActionSpec.from_dict(invalid)
    with pytest.raises(TypeError):
        ActionSpec(
            session_id="s",
            action_id="a",
            kind="move",  # type: ignore[arg-type]
            requested_at_ns=1,
            timeout_ns=1,
            origin_frame_id=0,
            origin_world_state_version=0,
        )
    with pytest.raises(ValueError):
        spec.is_stale_at(-1)


def test_action_handle_edges_and_validation_failures() -> None:
    handle = _handle()
    assert handle.action_id == "a"
    assert handle.deadline_ns == 110
    assert not handle.is_started
    assert handle.is_expired(90)
    assert not handle.is_active_at(10)
    with pytest.raises(ValueError):
        handle.is_expired(-1)
    with pytest.raises(ValueError):
        handle.start(9)

    started = handle.start(20)
    assert started.is_started
    with pytest.raises(ValueError):
        started.start(30)

    valid_result = ActionResult.from_handle(
        started,
        termination=ActionTermination.CANCELLED,
        completed_at_ns=30,
        result_frame_id=2,
        result_world_state_version=4,
    )
    started.validate_result(valid_result)
    valid_result.validate_against(started)
    for invalid_handle in (
        object(),
        ActionHandle(
            handle_id="other",
            session_id="s",
            spec=_spec(),
            issued_at_ns=10,
            expires_at_ns=90,
            generation=1,
        ),
    ):
        with pytest.raises((TypeError, ValueError)):
            valid_result.validate_against(invalid_handle)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        started.validate_result(object())  # type: ignore[arg-type]

    for kwargs in (
        {"session_id": "other"},
        {"action_id": "other"},
        {"handle_id": "other"},
        {"generation": 2},
    ):
        result = ActionResult(
            handle_id=kwargs.get("handle_id", "h"),
            action_id=kwargs.get("action_id", "a"),
            session_id=kwargs.get("session_id", "s"),
            termination=ActionTermination.SUCCESS,
            started_at_ns=20,
            completed_at_ns=30,
            origin_frame_id=2,
            origin_world_state_version=3,
            result_frame_id=2,
            result_world_state_version=4,
            generation=kwargs.get("generation", 1),
        )
        with pytest.raises(ValueError):
            started.validate_result(result)


def test_action_result_edges_and_strict_hydration() -> None:
    result = ActionResult.from_handle(
        _handle(),
        termination=ActionTermination.TIMEOUT,
        completed_at_ns=40,
        result_frame_id=3,
        result_world_state_version=4,
        details={"nested": {"values": [1]}},
    )
    assert ActionResult.from_dict(result.to_dict()) == result
    for invalid in (
        {},
        {**result.to_dict(), "details": []},
        {**result.to_dict(), "started_at_ns": "20"},
    ):
        with pytest.raises((ValueError, TypeError)):
            ActionResult.from_dict(invalid)

    with pytest.raises(ValueError):
        ActionResult(
            handle_id="h",
            action_id="a",
            session_id="s",
            termination=ActionTermination.SUCCESS,
            started_at_ns=40,
            completed_at_ns=30,
            origin_frame_id=2,
            origin_world_state_version=3,
            result_frame_id=3,
            result_world_state_version=4,
            generation=1,
        )
    with pytest.raises(TypeError):
        ActionResult(
            handle_id="h",
            action_id="a",
            session_id="s",
            termination="success",  # type: ignore[arg-type]
            started_at_ns=20,
            completed_at_ns=30,
            origin_frame_id=2,
            origin_world_state_version=3,
            result_frame_id=3,
            result_world_state_version=4,
            generation=1,
        )
    with pytest.raises(TypeError):
        ActionResult.from_handle(
            object(),  # type: ignore[arg-type]
            termination=ActionTermination.SUCCESS,
            completed_at_ns=30,
            result_frame_id=3,
            result_world_state_version=4,
        )
