from __future__ import annotations

import copy
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from test_frame_corpus import _make_fixture, _make_tapes

from maple_automation_core.capture.frame_source import canonical_calibration_sha256
from maple_automation_core.capture.pixel_store import PixelSpec, PixelStore, canonical_json
from maple_automation_core.domain.frame import FramePacket, FrameSize, SourceGeometry, SourceRect
from maple_automation_core.localization.minimap_marker import (
    MinimapMarkerConfig,
    MinimapMarkerExtractor,
)
from maple_automation_core.replay.frame_corpus import (
    canonical_digest,
    load_strict_json,
    public_privacy_summary,
)
from maple_automation_core.replay.player_marker import (
    PLAYER_MARKER_REPLAY_LIMITATIONS,
    PlayerMarkerExtraction,
    PlayerMarkerReplayDeterminismError,
    PlayerMarkerReplayError,
    PlayerMarkerReplayReport,
    PlayerMarkerReplayRunner,
    verify_player_marker_replay_report,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "player-marker-replay-report.schema.json"


def _write_json(path: Path, payload: object) -> None:
    path.write_bytes(canonical_json(payload) + b"\n")


def _replay_inputs(
    manifest: Path,
    truth_root: Path,
    *,
    as_of_offset_ns: int = 10_000,
) -> dict[str, object]:
    payload = load_strict_json(manifest)
    for tape_path in truth_root.glob("session-*.jsonl"):
        tape_path.unlink()
    rows: list[dict[str, object]] = []
    for index, sample in enumerate(payload["samples"]):
        truth = load_strict_json(truth_root / sample["truth_path"])
        if sample["wrong_size_negative"]:
            continue
        rows.append(
            {
                "session_id": sample["session_id"],
                "source_id": truth["source_id"],
                "source_sequence": sample["sequence"],
                "captured_at_ns": 100 + index,
                "admitted_at_ns": 100 + index,
                "clock_domain": "synthetic",
                "image_ref": sample["cas_ref"],
                "pixel_digest": sample["pixel_digest"],
                "source_width": truth["pixel_spec"]["width"],
                "source_height": truth["pixel_spec"]["height"],
                "retained": True,
            }
        )
    geometry = SourceGeometry(
        source_size=FrameSize(width=2, height=1),
        content_rect=SourceRect(x=0, y=0, width=2, height=1),
        working_size=FrameSize(width=2, height=1),
    )
    calibration = {
        "calibration_sha256": canonical_calibration_sha256(geometry, "synthetic-v1"),
        "geometry": geometry.to_dict(),
        "transform_version": "synthetic-v1",
        "max_age_ns": 100_000,
    }
    # The replay contract binds calibration to truth derivation metadata.  The
    # shared corpus helper intentionally starts with a zero placeholder, so
    # make this local contract fixture internally consistent before replay.
    truths: list[dict[str, object]] = []
    for sample in payload["samples"]:
        truth_path = truth_root / sample["truth_path"]
        truth = load_strict_json(truth_path)
        truth["derivation"]["calibration_sha256"] = calibration["calibration_sha256"]
        truth["record_digest"] = canonical_digest(truth, omit=("record_digest",))
        _write_json(truth_path, truth)
        sample["truth_sha256"] = sha256(truth_path.read_bytes()).hexdigest()
        truths.append(truth)
    payload["privacy_summary"] = public_privacy_summary(payload, truths)
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)
    audit: dict[str, object] = {
        "core_v2_real_input_call_count": 0,
        "double_write_event_count": 0,
        "failure_count": 0,
        "input_owner": "legacy",
        "keyboard_call_count": 0,
        "mouse_call_count": 0,
        "real_input_call_count": 0,
        "real_input_enabled": False,
        "receiver_connect_count": 0,
        "report_digest": "",
        "report_type": "b2_zero_input_audit",
        "schema_version": "1.0.0",
        "source_commit": "a" * 40,
        "status": "PASS",
        "wheel_sha256": "b" * 64,
        "window_write_count": 0,
    }
    audit["report_digest"] = canonical_digest(audit, omit=("report_digest",))
    event_tapes = _make_tapes(manifest, truth_root)
    index: dict[str, object] = {
        "schema_version": "1.0.0",
        "source_commit": payload["source_commit"],
        "corpus_digest": payload["corpus_digest"],
        "event_count": len(event_tapes),
        "tapes": [
            {
                "path": tape.name,
                "session_id": load_strict_json(truth_root / sample["truth_path"])["session_id"],
                "sha256": sha256(tape.read_bytes()).hexdigest(),
                "size_bytes": tape.stat().st_size,
            }
            for tape, sample in zip(event_tapes, payload["samples"], strict=True)
        ],
        "index_digest": "",
    }
    index["index_digest"] = canonical_digest(index, omit=("index_digest",))
    event_tape_index = truth_root / "event-tape-index.json"
    _write_json(event_tape_index, index)
    return {
        "event_tapes": event_tapes,
        "event_tape_index": event_tape_index,
        "accepted_frame_ledger": rows,
        "calibration": calibration,
        "zero_input_audit": audit,
        "as_of_offset_ns": as_of_offset_ns,
    }


def _runner(
    manifest: Path,
    truth_root: Path,
    cas_root: Path,
    extractor: object,
    **overrides: object,
) -> PlayerMarkerReplayRunner:
    inputs = _replay_inputs(manifest, truth_root)
    inputs.update(overrides)
    return PlayerMarkerReplayRunner(
        manifest,
        verification_profile="contract_fixture",
        truth_root=truth_root,
        cas_root=cas_root,
        event_tapes=inputs.pop("event_tapes"),
        event_tape_index=inputs.pop("event_tape_index"),
        extractor=extractor,
        replay_source_commit="c" * 40,
        config={"detector": "injected-v1"},
        extractor_artifact_digest="d" * 64,
        accepted_frame_ledger=inputs.pop("accepted_frame_ledger"),
        calibration=inputs.pop("calibration"),
        zero_input_audit=inputs.pop("zero_input_audit"),
        as_of_offset_ns=inputs.pop("as_of_offset_ns"),
        **inputs,
    )


def test_three_runs_use_real_frame_packet_and_skip_wrong_size_before_extractor(
    tmp_path: Path,
) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    calls: list[tuple[int, int, int, int]] = []

    def extractor(
        frame: FramePacket,
        *,
        now_ns: int,
        observed_at_ns: int,
        generation: int,
    ) -> object:
        assert isinstance(frame, FramePacket)
        calls.append((frame.frame_id, now_ns, observed_at_ns, generation))
        return {"candidate": {"frame_digest": frame.content_hash}, "evidence": "e" * 64}

    report = _runner(manifest, truth_root, cas_root, extractor).run_three_times()

    assert report.status == "PASS"
    assert report.deterministic is True
    assert report.execution_valid is True
    assert calls == [(0, 10_100, 100, 0), (1, 10_101, 101, 0)] * 3
    assert report.sample_order_digest == report.runs[0].sample_order_digest
    assert len({run.run_digest for run in report.runs}) == 1
    assert all(run.samples == report.runs[0].samples for run in report.runs)
    assert report.to_dict()["truth_scope"] == "frame_ingestion_only"
    assert report.to_dict()["verification_profile"] == "contract_fixture"
    assert report.to_dict()["limitations"] == list(PLAYER_MARKER_REPLAY_LIMITATIONS)
    assert report.to_dict()["as_of_ns"] == 10_000
    assert report.to_dict()["as_of_offset_ns"] == 10_000
    assert report.runs[0].samples[0].effective_now_ns == 10_100
    assert report.runs[0].samples[1].effective_now_ns == 10_101
    assert report.runs[0].samples[2].effective_now_ns is None
    assert report.to_dict()["zero_input_audit"]["real_input_call_count"] == 0  # type: ignore[index]
    assert all(
        set(sample)
        == {
            "sample_id",
            "status",
            "candidate_digest",
            "evidence_digest",
            "result_digest",
            "detector_result_digest",
            "detector_state_digest",
            "detector_config_digest",
            "exception_type_digest",
            "fault",
            "invoked",
            "as_of_offset_ns",
            "as_of_ns",
            "effective_now_ns",
            "observed_at_ns",
            "generation",
        }
        for sample in report.to_dict()["runs"][0]["samples"]  # type: ignore[index]
    )
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert "raw_pixels" not in encoded
    assert "image_ref" not in encoded
    assert "subject_id" not in encoded
    verify_player_marker_replay_report(
        report,
        corpus_source_commit="a" * 40,
        replay_source_commit="c" * 40,
        as_of_ns=10_000,
        as_of_offset_ns=10_000,
        sample_order=["sample-0", "sample-1", "sample-2"],
    )
    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(
            report,
            expected_verification_profile="contract_fixture",
            expected_event_tape_digest="0" * 64,
        )


def test_actual_minimap_extractor_is_compatible_with_replay_contract(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root, as_of_offset_ns=0)
    calibration = cast(dict[str, Any], inputs["calibration"])
    geometry = SourceGeometry.from_dict(cast(dict[str, Any], calibration["geometry"]))
    config = MinimapMarkerConfig(
        geometry=geometry,
        transform_version="synthetic-v1",
        calibration_sha256=cast(str, calibration["calibration_sha256"]),
        minimap_roi=SourceRect(x=0, y=0, width=2, height=1),
        max_age_ns=100_000,
    )

    class ReadOnlyPixelStore:
        def __init__(self, root: Path) -> None:
            self._store = PixelStore(root)

        def read(self, digest: str, spec: PixelSpec | None = None) -> bytes:
            return self._store.read(digest, spec)

    extractor = MinimapMarkerExtractor(
        config=config,
        pixel_store=ReadOnlyPixelStore(cas_root),
    )
    report = PlayerMarkerReplayRunner(
        manifest,
        verification_profile="contract_fixture",
        truth_root=truth_root,
        cas_root=cas_root,
        event_tapes=cast(list[Path], inputs["event_tapes"]),
        extractor=extractor,
        replay_source_commit="c" * 40,
        config=config.to_dict(),
        config_digest=config.digest,
        extractor_artifact_digest="d" * 64,
        accepted_frame_ledger=cast(list[dict[str, Any]], inputs["accepted_frame_ledger"]),
        calibration=calibration,
        zero_input_audit=cast(dict[str, Any], inputs["zero_input_audit"]),
        as_of_offset_ns=0,
        max_age_ns=config.max_age_ns,
    ).run_three_times()

    assert report.status == "PASS"
    assert report.execution_valid is True
    assert [sample.status for sample in report.runs[0].samples] == [
        "no_marker",
        "no_marker",
        "rejected",
    ]
    assert [sample.invoked for sample in report.runs[0].samples] == [True, True, False]


def test_report_schema_and_self_digest_reject_tampering(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    report = _runner(
        manifest,
        truth_root,
        cas_root,
        lambda frame: PlayerMarkerExtraction(candidate={"id": frame.frame_id}),
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


def test_missing_extractor_and_missing_b2_inputs_fail_closed(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    with pytest.raises(PlayerMarkerReplayError):
        _runner(manifest, truth_root, cas_root, None)
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(
            manifest,
            truth_root=truth_root,
            cas_root=cas_root,
            event_tapes=inputs["event_tapes"],
            extractor=lambda frame: None,
            replay_source_commit="c" * 40,
            config={"detector": "x"},
            extractor_artifact_digest="d" * 64,
            accepted_frame_ledger=inputs["accepted_frame_ledger"],
            calibration=inputs["calibration"],
            zero_input_audit=inputs["zero_input_audit"],
        )


def test_same_size_rogue_calibration_is_bound_to_truth(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    rogue = copy.deepcopy(_replay_inputs(manifest, truth_root)["calibration"])
    assert isinstance(rogue, dict)
    rogue_geometry = copy.deepcopy(rogue["geometry"])
    rogue_geometry["content_rect"]["width"] = 1
    rogue["geometry"] = rogue_geometry
    rogue["calibration_sha256"] = canonical_calibration_sha256(
        SourceGeometry.from_dict(rogue_geometry),
        rogue["transform_version"],
    )
    with pytest.raises(PlayerMarkerReplayError):
        _runner(manifest, truth_root, cas_root, lambda frame: None, calibration=rogue)


def test_split_event_tape_session_is_rejected(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    tapes = inputs["event_tapes"]
    assert isinstance(tapes, list)
    split = truth_root / "split-session.jsonl"
    split.write_bytes(tapes[0].read_bytes())
    with pytest.raises(PlayerMarkerReplayError):
        _runner(
            manifest,
            truth_root,
            cas_root,
            lambda frame: None,
            event_tapes=(tapes[0], split, *tapes[1:]),
            event_tape_index=None,
        )


def test_event_tape_index_requires_canonical_json_file(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    index_path = inputs["event_tape_index"]
    assert isinstance(index_path, Path)
    index_path.write_bytes(index_path.read_bytes() + b"\n")
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(
            manifest,
            verification_profile="contract_fixture",
            truth_root=truth_root,
            cas_root=cas_root,
            event_tapes=inputs["event_tapes"],
            event_tape_index=index_path,
            extractor=lambda frame: None,
            replay_source_commit="c" * 40,
            config={"detector": "injected-v1"},
            extractor_artifact_digest="d" * 64,
            accepted_frame_ledger=inputs["accepted_frame_ledger"],
            calibration=inputs["calibration"],
            zero_input_audit=inputs["zero_input_audit"],
            as_of_offset_ns=inputs["as_of_offset_ns"],
        )


def test_contract_fixture_may_omit_event_tape_index(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    report = _runner(
        manifest,
        truth_root,
        cas_root,
        lambda frame: None,
        event_tape_index=None,
    ).run_three_times()
    assert report.event_tape_index_artifact_digest is None
    assert "event_tape_index_artifact_digest" not in report.to_dict()
    verify_player_marker_replay_report(report)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(report.to_dict())) == []


def test_stateful_extractor_produces_fail_report_and_verifier_rejects_it(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    state = 0

    def extractor(frame: FramePacket) -> object:
        nonlocal state
        state += 1
        return {"candidate": {"ordinal": state}}

    report = _runner(manifest, truth_root, cas_root, extractor).run_three_times()

    assert report.status == "FAIL"
    assert report.deterministic is False
    assert report.execution_valid is True
    with pytest.raises(PlayerMarkerReplayDeterminismError):
        report.assert_deterministic()
    with pytest.raises(PlayerMarkerReplayDeterminismError):
        verify_player_marker_replay_report(report)


@pytest.mark.parametrize("exception_type", [ValueError, RuntimeError, KeyError])
def test_exception_type_digest_is_not_collapsed_across_runs(
    tmp_path: Path,
    exception_type: type[Exception],
) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)

    calls = 0

    def extractor(frame: FramePacket) -> object:
        nonlocal calls
        calls += 1
        # Rotate types by invocation so the replay exposes type drift rather
        # than folding every exception into one run-independent token.
        types = (ValueError, RuntimeError, KeyError)
        start = types.index(exception_type)
        raise types[(calls - 1 + start) % len(types)]("redacted")

    report = _runner(manifest, truth_root, cas_root, extractor).run_three_times()
    digests = [run.samples[0].exception_type_digest for run in report.runs]
    assert report.status == "FAIL"
    assert report.deterministic is False
    assert report.execution_valid is False
    assert len(set(digests)) == 3


def test_stable_exception_type_is_comparable_but_execution_invalid(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)

    def extractor(frame: FramePacket) -> object:
        raise ValueError("redacted")

    report = _runner(manifest, truth_root, cas_root, extractor).run_three_times()
    digests = [run.samples[0].exception_type_digest for run in report.runs]
    assert report.deterministic is True
    assert report.execution_valid is False
    assert len(set(digests)) == 1
    assert report.status == "FAIL"


def test_unknown_detector_status_is_execution_invalid(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)

    def extractor(frame: FramePacket) -> object:
        return {"status": "future_status", "candidate": None}

    report = _runner(manifest, truth_root, cas_root, extractor).run_three_times()
    assert report.status == "FAIL"
    assert report.execution_valid is False
    assert "execution_invalid" in report.execution_faults
    assert report.runs[0].samples[0].fault == "execution_invalid"


def test_declared_result_digest_cannot_hide_serialized_result_drift(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    calls = 0

    def extractor(frame: FramePacket) -> object:
        nonlocal calls
        calls += 1
        return {"candidate": {"ordinal": calls}, "result_digest": "a" * 64}

    report = _runner(manifest, truth_root, cas_root, extractor).run_three_times()
    assert report.status == "FAIL"
    assert report.execution_valid is False
    assert "execution_invalid" in report.execution_faults


def test_hidden_detector_result_without_recomputable_body_is_invalid(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)

    class HiddenResult:
        status = "detected"
        candidate: ClassVar[dict[str, str]] = {"id": "hidden"}

    report = _runner(
        manifest,
        truth_root,
        cas_root,
        lambda frame: HiddenResult(),
    ).run_three_times()
    assert report.status == "FAIL"
    assert report.execution_valid is False
    assert report.runs[0].samples[0].detector_result_digest is None
    assert "execution_invalid" in report.execution_faults


def test_audit_artifact_sha_and_public_path_are_rejected(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    audit = inputs["zero_input_audit"]
    assert isinstance(audit, dict)
    audit_path = tmp_path / "zero-audit.json"
    _write_json(audit_path, audit)
    with pytest.raises(PlayerMarkerReplayError):
        _runner(
            manifest,
            truth_root,
            cas_root,
            lambda frame: None,
            zero_input_audit=audit_path,
            zero_input_audit_artifact_sha256="0" * 64,
        )
    report = _runner(manifest, truth_root, cas_root, lambda frame: None).run_three_times()
    tampered = report.to_dict()
    tampered["limitations"] = ["C:\\Users\\operator\\private"]
    tampered["report_digest"] = sha256(b"re-signed").hexdigest()
    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(tampered)
