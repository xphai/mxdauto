from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from maple_automation_core.replay import (
    FRAME_ADMISSION_REQUIRED_STATUSES,
    FrameAdmissionFixture,
    FrameAdmissionReplayError,
    FrameAdmissionReplayRunner,
    canonical_digest,
    verify_frame_admission_report,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "g1" / "frame_admission_v1.json"
SCHEMA = ROOT / "schemas" / "frame-admission-report.schema.json"


def _fixture_data() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_runs_three_times_with_identical_canonical_digests() -> None:
    runner = FrameAdmissionReplayRunner(FIXTURE, repo_root=ROOT)
    report = runner.run_repeated(3, source_commit="a" * 40, generated_at="2026-08-29T00:00:00Z")
    payload = report.to_dict()

    assert report.status == "PASS"
    assert report.deterministic is True
    assert report.repeat_count == 3
    assert len({run["event_digest"] for run in payload["runs"]}) == 1  # type: ignore[index]
    assert len({run["output_digest"] for run in payload["runs"]}) == 1  # type: ignore[index]
    assert payload["source_commit"] == "a" * 40
    assert payload["input_audit"] == {
        "real_input_call_count": 0,
        "core_v2_real_input_call_count": 0,
        "double_write_event_count": 0,
        "connected": False,
        "input_owner": "legacy",
    }
    coverage = payload["status_coverage"]  # type: ignore[assignment]
    assert coverage["required_statuses"] == list(FRAME_ADMISSION_REQUIRED_STATUSES)  # type: ignore[index]
    assert coverage["all_required_statuses_observed"] is True  # type: ignore[index]
    behavior = payload["behavior_coverage"]  # type: ignore[assignment]
    assert behavior["complete"] is True  # type: ignore[index]


def test_fixture_covers_latch_events_latest_slot_and_reset() -> None:
    report = FrameAdmissionReplayRunner(FIXTURE, repo_root=ROOT).run_repeated(
        3,
        source_commit="b" * 40,
        generated_at="2026-08-29T00:00:00Z",
    )
    payload = report.to_dict()
    events = [event for run in payload["runs"][:1] for event in run["events"]]  # type: ignore[index]
    statuses = {event["status"] for event in events}  # type: ignore[index]
    assert statuses == set(FRAME_ADMISSION_REQUIRED_STATUSES)
    latest_observations = [
        item
        for run in payload["runs"][:1]  # type: ignore[index]
        for item in run["observations"]  # type: ignore[index]
        if item["scenario_id"] == "latest-overwrite-expire-reset"
    ]
    assert [item["latest_frame_id"] for item in latest_observations] == [40, 41, None, None, 0]  # type: ignore[index]
    assert latest_observations[-1]["session_id"] == "g1-session-reset"  # type: ignore[index]

    timestamp_reasons = {
        event["event"]["reason"]  # type: ignore[index]
        for event in events
        if event["status"] == "timestamp_regression"  # type: ignore[index]
    }
    assert "captured_at_ns is newer than receive clock" in timestamp_reasons
    assert "captured_at_ns moved backwards" in timestamp_reasons
    assert "receive clock moved backwards" in timestamp_reasons


def test_report_schema_is_strict_and_self_digest_is_checked() -> None:
    report = FrameAdmissionReplayRunner(FIXTURE, repo_root=ROOT).run_repeated(
        3,
        source_commit="c" * 40,
        generated_at="2026-08-29T00:00:00Z",
    )
    payload = report.to_dict()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(payload)) == []

    tampered = copy.deepcopy(payload)
    tampered["unexpected"] = True
    assert list(validator.iter_errors(tampered))

    contradictory = copy.deepcopy(payload)
    contradictory["deterministic"] = False
    assert list(validator.iter_errors(contradictory))

    contradictory = copy.deepcopy(payload)
    contradictory["runs"][0]["status"] = "FAIL"  # type: ignore[index]
    contradictory["runs"][0]["failures"] = ["synthetic contradiction"]  # type: ignore[index]
    assert list(validator.iter_errors(contradictory))


@pytest.mark.parametrize("tamper", ["repeat-count", "coverage-counts", "run-digest"])
def test_semantic_verifier_rejects_resigned_internal_contradictions(tamper: str) -> None:
    runner = FrameAdmissionReplayRunner(FIXTURE, repo_root=ROOT)
    payload = runner.run_repeated(
        3,
        source_commit="f" * 40,
        generated_at="2026-08-29T00:00:00Z",
    ).to_dict()
    if tamper == "repeat-count":
        payload["repeat_count"] = 999
    elif tamper == "coverage-counts":
        payload["status_coverage"]["counts"] = {  # type: ignore[index]
            status: 0 for status in FRAME_ADMISSION_REQUIRED_STATUSES
        }
    else:
        payload["runs"][0]["run_digest"] = "0" * 64  # type: ignore[index]
    digest_payload = dict(payload)
    digest_payload.pop("report_digest", None)
    digest_payload.pop("generated_at", None)
    payload["report_digest"] = canonical_digest(digest_payload)

    with pytest.raises(FrameAdmissionReplayError):
        verify_frame_admission_report(payload, runner.fixture)


def test_semantic_verifier_replays_fixture_before_accepting_resigned_events() -> None:
    runner = FrameAdmissionReplayRunner(FIXTURE, repo_root=ROOT)
    payload = runner.run_repeated(
        3,
        source_commit="1" * 40,
        generated_at="2026-08-29T00:00:00Z",
    ).to_dict()
    for run in payload["runs"]:  # type: ignore[union-attr]
        accepted = next(event for event in run["events"] if event["status"] == "accepted")
        accepted["event"]["plan_suppressed"] = True
        accepted["packet_frame_id"] = None
        accepted["packet_digest"] = None
        run["event_digest"] = canonical_digest({"events": run["events"]})
        deterministic_payload = {
            "scenarios": run["scenarios"],
            "events": run["events"],
            "observations": run["observations"],
            "status_counts": run["status_counts"],
            "failures": run["failures"],
            "status": run["status"],
        }
        run["output_digest"] = canonical_digest(deterministic_payload)
        run["run_digest"] = run["output_digest"]
    digest_payload = dict(payload)
    digest_payload.pop("report_digest", None)
    digest_payload.pop("generated_at", None)
    payload["report_digest"] = canonical_digest(digest_payload)

    with pytest.raises(FrameAdmissionReplayError, match="differs from fixture replay"):
        verify_frame_admission_report(payload, runner.fixture)


def test_tampered_expectation_is_a_failed_evidence_report() -> None:
    fixture = _fixture_data()
    first_scenario = fixture["scenarios"][0]  # type: ignore[index]
    first_scenario["operations"][0]["expect_status"] = "stale"  # type: ignore[index]
    report = FrameAdmissionReplayRunner(fixture, repo_root=ROOT).run_repeated(
        3,
        source_commit="d" * 40,
        generated_at="2026-08-29T00:00:00Z",
    )
    assert report.status == "FAIL"
    assert report.deterministic is True
    assert report.to_dict()["runs"][0]["failures"]  # type: ignore[index]


def test_missing_required_status_cannot_produce_pass() -> None:
    fixture = _fixture_data()
    fixture["scenarios"] = [
        scenario
        for scenario in fixture["scenarios"]  # type: ignore[union-attr]
        if scenario["scenario_id"] != "clock-domain-mismatch-latch"
    ]
    report = FrameAdmissionReplayRunner(fixture, repo_root=ROOT).run_repeated(
        3,
        source_commit="e" * 40,
        generated_at="2026-08-29T00:00:00Z",
    )
    payload = report.to_dict()
    assert payload["status"] == "FAIL"
    assert payload["status_coverage"]["all_required_statuses_observed"] is False  # type: ignore[index]


def test_fixture_validation_rejects_duplicate_or_empty_scenarios() -> None:
    fixture = _fixture_data()
    fixture["scenarios"] = []
    with pytest.raises(FrameAdmissionReplayError, match="at least one"):
        FrameAdmissionFixture.from_dict(fixture)  # type: ignore[arg-type]

    fixture = _fixture_data()
    fixture["scenarios"][1]["scenario_id"] = fixture["scenarios"][0]["scenario_id"]  # type: ignore[index]
    with pytest.raises(FrameAdmissionReplayError, match="duplicate"):
        FrameAdmissionFixture.from_dict(fixture)  # type: ignore[arg-type]


def test_runner_requires_three_repetitions() -> None:
    runner = FrameAdmissionReplayRunner(FIXTURE, repo_root=ROOT)
    with pytest.raises(ValueError, match="at least 3"):
        runner.run_repeated(2)
