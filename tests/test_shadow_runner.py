from __future__ import annotations

import json
from pathlib import Path

import pytest

from maple_automation_core.domain.actions import ActionKind
from maple_automation_core.replay import (
    DryRunInputSink,
    GoldenFixture,
    LegacyObservedAction,
    ShadowError,
    ShadowReport,
    ShadowRunner,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "pilot_minimal_v1.json"


def _fixture() -> GoldenFixture:
    return GoldenFixture.from_path(FIXTURE_PATH)


def test_dry_run_sink_has_explicit_lifecycle_and_zero_real_calls() -> None:
    sink = DryRunInputSink()
    assert sink.connected is False
    with pytest.raises(ShadowError, match="not connected"):
        sink.key_down("right")
    with pytest.raises(ShadowError, match="not connected"):
        sink.record_planned_action(_fixture().frames[0].action.spec)  # type: ignore[union-attr]

    sink.connect()
    with pytest.raises(ShadowError, match="already connected"):
        sink.connect()
    sink.key_down("right")
    sink.key_up("right")
    sink.release_all()
    sink.close()
    with pytest.raises(ShadowError, match="not connected"):
        sink.close()
    sink.assert_no_real_input()
    assert sink.real_input_call_count == 0
    assert sink.input_call_count == sink.dry_run_call_count == 5
    assert [item.sequence for item in sink.receipts] == list(range(5))
    assert all(item.simulated for item in sink.receipts)

    sink.reset()
    assert sink.receipts == ()
    assert sink.planned_action_count == 0
    sink.record_real_boundary_attempt("receiver", "send")
    assert sink.real_input_call_count == 1
    assert sink.audit()["receiver_call_count"] == 1
    with pytest.raises(ShadowError, match="real input"):
        sink.assert_no_real_input()
    sink.reset()
    with pytest.raises(ShadowError, match="Unknown"):
        sink.record_real_boundary_attempt("other", "send")
    sink.connect()
    with pytest.raises(ShadowError, match="connected"):
        sink.reset()
    sink.close()


def test_shadow_runner_report_separates_plans_from_legacy_and_proves_zero_calls(
    tmp_path: Path,
) -> None:
    sink = DryRunInputSink()
    runner = ShadowRunner(FIXTURE_PATH, input_sink=sink)
    report = runner.run()

    assert report.status == "PASS"
    report.assert_no_real_input()
    assert report.fixture_digest == runner.replay_runner.fixture_digest
    assert report.bundle_digest == runner.replay_runner.fixture.bundle_digest
    assert report.core_v2_real_input_call_count == 0
    assert report.input_audit["real_input_call_count"] == 0
    assert report.input_audit["core_v2_real_input_call_count"] == 0
    assert report.input_audit["double_write_event_count"] == 0
    assert report.input_audit["boundary_attempts"] == ()
    assert report.input_audit["planned_action_count"] == 2
    assert report.input_audit["legacy_observed_action_count"] == 2
    assert all(item["provenance"] == "core_v2_planned" for item in report.planned_actions)
    assert all(
        item["execution_status"] == "planned_not_executed" for item in report.planned_actions
    )
    assert all(item["provenance"] == "legacy_observed" for item in report.legacy_observed_actions)
    assert all(
        item["execution_status"] == "observed_not_core_executed"
        for item in report.legacy_observed_actions
    )
    assert [item["taxonomy"] for item in report.diffs] == ["MATCH", "MATCH"]
    assert sink.connected is False

    second = runner.run()
    assert second.output_digest == report.output_digest
    assert second.to_json() == report.to_json()
    report_path = report.write_json(tmp_path / "evidence" / "shadow.json")
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["report_id"] == report.report_id
    assert written["report_digest"] == report.to_dict()["report_digest"]


def test_legacy_observed_action_roundtrip_and_validation() -> None:
    data = _fixture().legacy_observed_actions[0]
    action = LegacyObservedAction.from_dict(data)
    assert action.to_dict()["provenance"] == "legacy_observed"
    assert LegacyObservedAction.from_dict(action.to_dict()) == action
    with pytest.raises(ShadowError, match="missing key"):
        LegacyObservedAction.from_dict({"action_id": "x"})
    with pytest.raises(ValueError, match="frame_id"):
        LegacyObservedAction(
            action_id="x",
            session_id="s",
            frame_id=-1,
            world_state_version=1,
            kind=ActionKind.MOVE,
            observed_at_ns=1,
        )
    with pytest.raises(ValueError, match="details"):
        LegacyObservedAction(
            action_id="x",
            session_id="s",
            frame_id=1,
            world_state_version=1,
            kind=ActionKind.MOVE,
            observed_at_ns=1,
            details={"bad": {1, 2}},
        )


def test_shadow_diff_taxonomy_covers_mismatch_and_unpaired_actions() -> None:
    fixture = _fixture()
    planned = tuple(frame.action.spec for frame in fixture.frames if frame.action is not None)
    observed = (
        LegacyObservedAction(
            action_id="legacy-mismatch",
            session_id=fixture.session_id,
            frame_id=1,
            world_state_version=1,
            kind=ActionKind.JUMP,
            observed_at_ns=1_000_001_000,
        ),
        LegacyObservedAction(
            action_id="legacy-only",
            session_id=fixture.session_id,
            frame_id=2,
            world_state_version=2,
            kind=ActionKind.JUMP,
            observed_at_ns=2_000_001_000,
        ),
        LegacyObservedAction(
            action_id="legacy-trailing-only",
            session_id=fixture.session_id,
            frame_id=3,
            world_state_version=3,
            kind=ActionKind.STOP,
            observed_at_ns=3_000_001_000,
        ),
    )
    diffs = ShadowRunner(fixture)._diffs(planned, observed)
    assert [item["taxonomy"] for item in diffs] == ["KIND_MISMATCH", "MATCH", "LEGACY_ONLY"]

    plan_only = ShadowRunner._diffs((planned[0],), ())
    assert plan_only[0]["taxonomy"] == "PLANNED_ONLY"


def test_shadow_runner_rejects_invalid_legacy_records() -> None:
    fixture = _fixture()
    runner = ShadowRunner(fixture)

    invalid = [dict(item) for item in fixture.legacy_observed_actions]
    invalid[0] = dict(invalid[0])
    invalid[0]["frame_id"] = 99
    object.__setattr__(fixture, "legacy_observed_actions", tuple(invalid))
    with pytest.raises(ShadowError, match="unknown frame"):
        runner._legacy_actions()

    invalid[0]["frame_id"] = 1
    invalid[0]["world_state_version"] = 99
    object.__setattr__(fixture, "legacy_observed_actions", tuple(invalid))
    with pytest.raises(ShadowError, match="frame/version"):
        runner._legacy_actions()

    invalid[0]["world_state_version"] = 1
    invalid[0]["session_id"] = "other-session"
    object.__setattr__(fixture, "legacy_observed_actions", tuple(invalid))
    with pytest.raises(ShadowError, match="session mismatch"):
        runner._legacy_actions()

    invalid[0]["session_id"] = fixture.session_id
    invalid[0]["observed_at_ns"] = 9_999_999_999
    invalid[1] = dict(invalid[1])
    invalid[1]["observed_at_ns"] = 1
    object.__setattr__(fixture, "legacy_observed_actions", tuple(invalid))
    with pytest.raises(ShadowError, match="time moved backwards"):
        runner._legacy_actions()


def test_shadow_report_is_frozen_and_flags_nonzero_real_input() -> None:
    report = ShadowReport(
        fixture_id="fixture",
        fixture_digest="fixture-digest",
        bundle_id="bundle",
        bundle_digest="bundle-digest",
        session_id="session",
        replay_output_digest="replay",
        planned_actions=({"a": {"nested": [1]}},),
        legacy_observed_actions=(),
        diffs=(),
        input_audit={"real_input_call_count": 1, "nested": {"values": [1]}},
        output_digest="output",
        status="FAIL",
    )
    with pytest.raises(ShadowError, match="non-zero"):
        report.assert_no_real_input()
    with pytest.raises(TypeError):
        report.input_audit["x"] = 1  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        report.planned_actions[0]["a"]["nested"].append(2)  # type: ignore[index,union-attr]

    with pytest.raises(ShadowError, match="status"):
        ShadowReport(
            fixture_id="fixture",
            fixture_digest="fixture-digest",
            bundle_id="bundle",
            bundle_digest="bundle-digest",
            session_id="session",
            replay_output_digest="replay",
            planned_actions=(),
            legacy_observed_actions=(),
            diffs=(),
            input_audit={"real_input_call_count": 0},
            output_digest="output",
            status="UNKNOWN",
        )


def test_shadow_runner_requires_dry_run_sink_and_strict_report_audit() -> None:
    with pytest.raises(TypeError, match="DryRunInputSink"):
        ShadowRunner(_fixture(), input_sink=object())  # type: ignore[arg-type]

    class UnsafeSubclass(DryRunInputSink):
        pass

    with pytest.raises(TypeError, match="DryRunInputSink"):
        ShadowRunner(_fixture(), input_sink=UnsafeSubclass())

    with pytest.raises(ShadowError, match="real_input"):
        ShadowReport(
            fixture_id="fixture",
            fixture_digest="fixture-digest",
            bundle_id="bundle",
            bundle_digest="bundle-digest",
            session_id="session",
            replay_output_digest="replay",
            planned_actions=(),
            legacy_observed_actions=(),
            diffs=(),
            input_audit={"real_input_call_count": -1},
            output_digest="output",
            status="FAIL",
        )


def test_shadow_report_json_is_machine_readable() -> None:
    report = ShadowRunner(_fixture()).run()
    data = json.loads(report.to_json())
    assert data["report_type"] == "shadow"
    assert data["report_id"] == report.report_id
    assert data["input_audit"]["core_v2_real_input_call_count"] == 0
