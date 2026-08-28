from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from maple_automation_core.replay import (
    GoldenAction,
    GoldenFixture,
    GoldenFrame,
    GoldenReplayRunner,
    ReplayDeterminismError,
    ReplayError,
    ReplayReport,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "pilot_minimal_v1.json"


def _raw_fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_golden_fixture_has_deidentification_metadata_and_roundtrips() -> None:
    fixture = GoldenFixture.from_path(FIXTURE_PATH)
    raw_digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()

    assert fixture.fixture_id == "golden-pilot-minimal-v1"
    assert fixture.source == "synthetic-deidentified"
    assert fixture.bundle_id == "bundle-golden-pilot-v1"
    assert fixture.metadata["de_identification"]["status"] == "complete"  # type: ignore[index]
    assert fixture.frames[0].packet.source_geometry.downsample[0] > 1
    assert raw_digest == GoldenFixture.from_path_with_digest(FIXTURE_PATH)[1]
    assert fixture.digest() != raw_digest
    assert GoldenFixture.from_dict(fixture.to_dict()) == fixture
    with pytest.raises(TypeError):
        fixture.bundle["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        fixture.metadata["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        fixture.legacy_observed_actions[0]["new"] = True  # type: ignore[index]


def test_golden_replay_repeats_world_action_and_event_digests(tmp_path: Path) -> None:
    runner = GoldenReplayRunner(FIXTURE_PATH, tape_path=tmp_path / "events.jsonl")
    first = runner.run()
    assert len(first.world_states) == 3
    assert len(first.action_specs) == len(first.action_handles) == len(first.action_results) == 2
    assert first.bundle_digest
    assert first.action_results[0].origin == first.action_specs[0].origin
    assert first.action_results[0].result.frame_id == 2
    assert [event.event_type for event in first.events] == [
        "frame.observed",
        "action.proposed",
        "action.issued",
        "action.started",
        "frame.observed",
        "action.terminal",
        "input.release_all",
        "action.proposed",
        "action.issued",
        "action.started",
        "frame.observed",
        "action.terminal",
        "input.release_all",
    ]
    assert first.event_sequence_digest
    assert first.events[-1].previous_record_hash == first.events[-2].record_hash
    assert first.to_dict()["bundle_digest"] == first.bundle_digest

    report = runner.run_three_times()
    assert report.deterministic is True
    assert report.status == "PASS"
    assert report.repeat_count == 3
    assert len({item["event_digest"] for item in report.runs}) == 1
    assert len({item["event_sequence_digest"] for item in report.runs}) == 1
    report.assert_deterministic()
    assert json.loads(report.to_json())["report_digest"]
    report_path = report.write_json(tmp_path / "evidence" / "replay.json")
    assert json.loads(report_path.read_text(encoding="utf-8"))["report_id"] == report.report_id


def test_golden_replay_mapping_and_explicit_tape_are_reproducible(tmp_path: Path) -> None:
    data = _raw_fixture()
    runner = GoldenReplayRunner(data, tape_path=tmp_path / "nested" / "events.jsonl")
    first = runner.replay()
    second = runner.run()

    assert first.output_digest == second.output_digest
    assert first.event_digest == second.event_digest
    assert first.fixture_digest == GoldenFixture.from_dict(data).digest()
    assert len(first.events) == 13


def test_golden_fixture_rejects_tampering_and_temporal_drift() -> None:
    data = _raw_fixture()

    mismatched_frame = copy.deepcopy(data)
    mismatched_frame["frames"][0]["world_state"]["frame_id"] = 99  # type: ignore[index]
    with pytest.raises((ValueError, ReplayError), match="frame_id"):
        GoldenFixture.from_dict(mismatched_frame)  # type: ignore[arg-type]

    mismatched_result = copy.deepcopy(data)
    mismatched_result["frames"][0]["action"]["result_world_state_version"] = 3  # type: ignore[index]
    with pytest.raises((ValueError, ReplayError), match="frame/version"):
        GoldenFixture.from_dict(mismatched_result)  # type: ignore[arg-type]

    stale_world = copy.deepcopy(data)
    stale_world["frames"][0]["world_state"]["observed_at_ns"] = 0  # type: ignore[index]
    with pytest.raises((ValueError, ReplayError), match="observed_at_ns"):
        GoldenFixture.from_dict(stale_world)  # type: ignore[arg-type]

    stale_result = copy.deepcopy(data)
    stale_result["frames"][0]["action"]["completed_at_ns"] = 1  # type: ignore[index]
    with pytest.raises((ValueError, ReplayError), match="completed_at_ns"):
        GoldenFixture.from_dict(stale_result)  # type: ignore[arg-type]

    duplicate_action = copy.deepcopy(data)
    duplicate_action["frames"][1]["action"]["spec"]["action_id"] = "core-plan-move-001"  # type: ignore[index]
    with pytest.raises((ValueError, ReplayError), match="action_id"):
        GoldenFixture.from_dict(duplicate_action)  # type: ignore[arg-type]

    crossing_lifecycle = copy.deepcopy(data)
    crossing_lifecycle["frames"][1]["action"]["spec"]["requested_at_ns"] = 3_100_000_000  # type: ignore[index]
    crossing_lifecycle["frames"][1]["action"]["completed_at_ns"] = 3_100_000_001  # type: ignore[index]
    with pytest.raises(ReplayError, match="crosses the next frame"):
        GoldenFixture.from_dict(crossing_lifecycle)  # type: ignore[arg-type]


def test_golden_fixture_rejects_legacy_mismatch_and_duplicate_json(tmp_path: Path) -> None:
    data = _raw_fixture()
    unknown_legacy = copy.deepcopy(data)
    unknown_legacy["legacy_observed_actions"][0]["frame_id"] = 77  # type: ignore[index]
    with pytest.raises((ValueError, ReplayError), match="unknown frame"):
        GoldenFixture.from_dict(unknown_legacy)  # type: ignore[arg-type]

    duplicate_legacy = copy.deepcopy(data)
    duplicate_legacy["legacy_observed_actions"][1]["action_id"] = "legacy-observed-move-001"  # type: ignore[index]
    with pytest.raises((ValueError, ReplayError), match="action_id"):
        GoldenFixture.from_dict(duplicate_legacy)  # type: ignore[arg-type]

    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text('{"fixture_id":"one","fixture_id":"two"}', encoding="utf-8")
    with pytest.raises(ReplayError, match="invalid golden fixture JSON"):
        GoldenFixture.from_path(duplicate_path)

    nonstandard_path = tmp_path / "nan.json"
    nonstandard_path.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(ReplayError, match="invalid golden fixture JSON"):
        GoldenFixture.from_path(nonstandard_path)

    with pytest.raises(ReplayError, match="invalid golden fixture JSON"):
        GoldenFixture.from_path(tmp_path / "missing.json")


def test_golden_fixture_constructor_and_runner_error_paths() -> None:
    data = _raw_fixture()
    no_frames = copy.deepcopy(data)
    no_frames["frames"] = []
    with pytest.raises(ReplayError, match="at least one frame"):
        GoldenFixture.from_dict(no_frames)  # type: ignore[arg-type]

    no_bundle_id = copy.deepcopy(data)
    no_bundle_id["bundle"] = {"source_commit": "0" * 40}
    with pytest.raises(ReplayError, match="bundle"):
        GoldenFixture.from_dict(no_bundle_id)  # type: ignore[arg-type]

    bad_metadata = copy.deepcopy(data)
    bad_metadata["metadata"] = []
    with pytest.raises(ValueError, match="metadata"):
        GoldenFixture.from_dict(bad_metadata)  # type: ignore[arg-type]

    short_timeout = copy.deepcopy(data)
    short_timeout["frames"][0]["action"]["spec"]["timeout_ns"] = 1  # type: ignore[index]
    short_runner = GoldenReplayRunner(short_timeout)  # type: ignore[arg-type]
    with pytest.raises(ReplayError, match="timeout"):
        short_runner.run()

    with pytest.raises(ValueError, match="repetitions"):
        GoldenReplayRunner(data).run_repeated(0)  # type: ignore[arg-type]


def test_golden_report_validation_and_determinism_error() -> None:
    runs = ({"output_digest": "a"},)
    report = ReplayReport(
        fixture_id="fixture",
        bundle_id="bundle",
        session_id="session",
        fixture_digest="fixture-digest",
        repeat_count=1,
        deterministic=False,
        runs=runs,
        output_digest="report-digest",
        status="FAIL",
    )
    with pytest.raises(ReplayDeterminismError):
        report.assert_deterministic()
    with pytest.raises(TypeError, match="runs"):
        ReplayReport(
            fixture_id="fixture",
            bundle_id="bundle",
            session_id="session",
            fixture_digest="fixture-digest",
            repeat_count=1,
            deterministic=True,
            runs=[{"output_digest": "a"}],  # type: ignore[arg-type]
            output_digest="report-digest",
            status="PASS",
        )
    with pytest.raises(ReplayError, match="status"):
        ReplayReport(
            fixture_id="fixture",
            bundle_id="bundle",
            session_id="session",
            fixture_digest="fixture-digest",
            repeat_count=1,
            deterministic=True,
            runs=runs,
            output_digest="report-digest",
            status="UNKNOWN",
        )


def test_golden_value_hydration_missing_keys() -> None:
    fixture_data = _raw_fixture()
    frame_data = fixture_data["frames"][0]  # type: ignore[index]
    with pytest.raises(ReplayError, match="golden frame missing key"):
        GoldenFrame.from_dict({"packet": frame_data["packet"]})  # type: ignore[index]
    with pytest.raises(ReplayError, match="golden action missing key"):
        GoldenAction.from_dict({"spec": frame_data["action"]["spec"]})  # type: ignore[index]
    with pytest.raises(ReplayError, match="golden fixture missing key"):
        GoldenFixture.from_dict({"fixture_id": "missing"})
