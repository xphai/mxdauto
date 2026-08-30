from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from test_frame_corpus import _make_fixture, _make_tapes

from maple_automation_core.capture.pixel_store import PixelStore
from maple_automation_core.replay.frame_corpus import load_strict_json
from maple_automation_core.replay.player_marker import (
    PlayerMarkerExtraction,
    PlayerMarkerReplayDeterminismError,
    PlayerMarkerReplayError,
    PlayerMarkerReplayReport,
    PlayerMarkerReplayRunner,
    verify_player_marker_replay_report,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "player-marker-replay-report.schema.json"


def test_three_runs_are_hash_only_and_skip_wrong_size_before_extractor(
    tmp_path: Path,
) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    tapes = _make_tapes(manifest, truth_root)
    calls: list[str] = []

    def extractor(frame: object) -> object:
        calls.append(frame.sample_id)  # type: ignore[attr-defined]
        return {
            "candidate": {"sample": frame.sample_id},  # type: ignore[attr-defined]
            "evidence": {"source": "synthetic"},
        }

    report = PlayerMarkerReplayRunner(
        manifest,
        truth_root=truth_root,
        cas_root=cas_root,
        event_tapes=tapes,
        extractor=extractor,
        source_commit="a" * 40,
        config={"detector": "injected-v1"},
        as_of_ns=123,
    ).run_three_times()

    assert report.status == "PASS"
    assert report.deterministic is True
    assert report.repeat_count == 3
    assert calls == ["sample-0", "sample-1"] * 3
    assert report.sample_order_digest == report.runs[0].sample_order_digest
    assert len({run.run_digest for run in report.runs}) == 1
    assert all(run.samples == report.runs[0].samples for run in report.runs)
    assert report.to_dict()["truth_scope"] == "frame_ingestion_only"
    assert report.to_dict()["corpus_digest"] == load_strict_json(manifest)["corpus_digest"]
    assert report.to_dict()["zero_input_audit"]["real_input_call_count"] == 0  # type: ignore[index]
    assert all(
        set(sample) == {
            "sample_id",
            "status",
            "candidate_digest",
            "evidence_digest",
            "result_digest",
            "fault",
        }
        for sample in report.to_dict()["runs"][0]["samples"]  # type: ignore[index]
    )
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert "raw_pixels" not in encoded
    assert "image_ref" not in encoded
    assert "subject_id" not in encoded
    verify_player_marker_replay_report(report, source_commit="a" * 40, as_of_ns=123)


def test_report_schema_and_self_digest_reject_tampering(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    report = PlayerMarkerReplayRunner(
        manifest,
        truth_root=truth_root,
        cas_root=PixelStore(cas_root),
        event_tapes=_make_tapes(manifest, truth_root),
        extractor=lambda frame: PlayerMarkerExtraction(candidate={"id": frame.sample_id}),
    ).run_three_times()
    payload = report.to_dict()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(payload)) == []
    assert PlayerMarkerReplayReport.from_dict(payload) == report

    extra = copy.deepcopy(payload)
    extra["unexpected"] = True
    assert list(validator.iter_errors(extra))
    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(extra)

    tampered = copy.deepcopy(payload)
    tampered["runs"][0]["samples"][0]["status"] = "no_marker"  # type: ignore[index]
    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(tampered)


def test_missing_extractor_is_a_deterministic_fault_without_detector_calls(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    report = PlayerMarkerReplayRunner(
        manifest,
        truth_root=truth_root,
        cas_root=cas_root,
        event_tapes=_make_tapes(manifest, truth_root),
    ).run_three_times()

    assert report.deterministic is True
    assert report.status == "PASS"
    assert [item.status for item in report.runs[0].samples] == [
        "fault",
        "fault",
        "rejected",
    ]
    assert [item.fault for item in report.runs[0].samples] == [
        "extractor_missing",
        "extractor_missing",
        "frame_size_changed",
    ]


def test_stateful_extractor_produces_fail_report_and_verifier_rejects_it(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    state = 0

    def extractor(frame: object) -> object:
        nonlocal state
        state += 1
        return {"candidate": {"ordinal": state}}

    report = PlayerMarkerReplayRunner(
        manifest,
        truth_root=truth_root,
        cas_root=cas_root,
        event_tapes=_make_tapes(manifest, truth_root),
        extractor=extractor,
    ).run_three_times()

    assert report.status == "FAIL"
    assert report.deterministic is False
    with pytest.raises(PlayerMarkerReplayDeterminismError):
        report.assert_deterministic()
    with pytest.raises(PlayerMarkerReplayDeterminismError):
        verify_player_marker_replay_report(report)
