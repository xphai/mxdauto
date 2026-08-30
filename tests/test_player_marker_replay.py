from __future__ import annotations

import copy
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from test_frame_corpus import _make_fixture, _make_tapes

import maple_automation_core.replay.player_marker as player_marker
from maple_automation_core.capture.frame_source import canonical_calibration_sha256
from maple_automation_core.capture.pixel_store import PixelSpec, PixelStore, canonical_json
from maple_automation_core.domain.frame import FramePacket, FrameSize, SourceGeometry, SourceRect
from maple_automation_core.localization.minimap_marker import (
    MinimapMarkerConfig,
    MinimapMarkerExtractor,
)
from maple_automation_core.replay.event_tape import EventTape
from maple_automation_core.replay.frame_corpus import (
    canonical_digest,
    load_strict_json,
    public_privacy_summary,
)
from maple_automation_core.replay.player_marker import (
    PLAYER_MARKER_REPLAY_LIMITATIONS,
    PlayerMarkerExtraction,
    PlayerMarkerReplayConfig,
    PlayerMarkerReplayDeterminismError,
    PlayerMarkerReplayError,
    PlayerMarkerReplayReport,
    PlayerMarkerReplayRun,
    PlayerMarkerReplayRunner,
    PlayerMarkerReplaySample,
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


def _valid_sample(
    *,
    sample_id: str = "sample-unit",
    status: str = "detected",
    candidate_digest: str | None = "a" * 64,
    observed_at_ns: int | None = 100,
    effective_now_ns: int | None = 110,
    as_of_ns: int = 10,
    as_of_offset_ns: int | None = 10,
    generation: int = 0,
    detector_config_digest: str = "c" * 64,
) -> PlayerMarkerReplaySample:
    if status != "detected":
        candidate_digest = None
    return PlayerMarkerReplaySample.build(
        sample_id=sample_id,
        status=status,
        candidate_digest=candidate_digest,
        evidence_digest="e" * 64,
        detector_result_digest="d" * 64,
        detector_state_digest="b" * 64,
        detector_config_digest=detector_config_digest,
        invoked=True,
        observed_at_ns=observed_at_ns,
        generation=generation,
        as_of_ns=as_of_ns,
        as_of_offset_ns=as_of_offset_ns,
        effective_now_ns=effective_now_ns,
    )


def _valid_run(*, run_index: int = 1) -> PlayerMarkerReplayRun:
    sample = _valid_sample()
    order_digest = player_marker._digest([sample.sample_id])
    return PlayerMarkerReplayRun.build(
        run_index=run_index,
        sample_order_digest=order_digest,
        samples=(sample,),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "unknown"},
        {"fault": "unknown_fault"},
    ],
)
def test_extraction_adapter_rejects_unknown_status_and_fault(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        PlayerMarkerExtraction(**kwargs)


def test_extraction_adapter_serialises_public_result_values() -> None:
    class Evidence:
        def to_dict(self) -> dict[str, str]:
            return {"kind": "fixture"}

    class DeclaredBytes:
        digest = "d" * 64

    result = PlayerMarkerExtraction(
        candidate=bytes([1, 2, 3]),
        evidence=Evidence(),
    )
    body = result.to_dict()
    assert body["candidate"] == {
        "bytes_sha256": sha256(b"\x01\x02\x03").hexdigest(),
        "byte_count": 3,
    }
    assert body["evidence"] == {"kind": "fixture"}
    assert PlayerMarkerExtraction(candidate=DeclaredBytes()).to_dict()["candidate"] == {
        "declared_digest": "d" * 64
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("sample_id", "bad id"),
        ("status", "unknown"),
        ("candidate_digest", None),
        ("candidate_digest", "short"),
        ("evidence_digest", "short"),
        ("detector_result_digest", "short"),
        ("detector_state_digest", "short"),
        ("detector_config_digest", "short"),
        ("exception_type_digest", "short"),
        ("fault", "unknown_fault"),
        ("invoked", 1),
        ("as_of_ns", -1),
        ("as_of_offset_ns", 11),
        ("observed_at_ns", -1),
        ("effective_now_ns", 111),
        ("generation", -1),
        ("result_digest", "0" * 64),
    ],
)
def test_sample_contract_rejects_invalid_public_fields(field: str, value: object) -> None:
    sample = _valid_sample()
    with pytest.raises((PlayerMarkerReplayError, TypeError, ValueError)):
        replace(sample, **{field: value})


def test_sample_contract_rejects_non_detected_candidate_and_missing_effective_clock() -> None:
    with pytest.raises(PlayerMarkerReplayError):
        replace(_valid_sample(), status="no_marker")
    with pytest.raises(PlayerMarkerReplayError):
        replace(_valid_sample(), observed_at_ns=None, effective_now_ns=110)
    with pytest.raises(PlayerMarkerReplayError):
        replace(_valid_sample(), observed_at_ns=100, effective_now_ns=None)


def test_sample_builder_supports_legacy_as_of_and_rejects_conflicting_alias() -> None:
    sample = PlayerMarkerReplaySample.build(
        sample_id="legacy",
        status="no_marker",
        detector_state_digest="b" * 64,
        detector_config_digest="c" * 64,
        invoked=False,
        observed_at_ns=None,
        generation=0,
        as_of_ns=7,
    )
    assert sample.as_of_ns == 7
    assert sample.as_of_offset_ns == 7
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplaySample.build(
            sample_id="legacy",
            status="no_marker",
            detector_state_digest="s" * 64,
            detector_config_digest="c" * 64,
            invoked=False,
            observed_at_ns=None,
            generation=0,
            as_of_ns=7,
            as_of_offset_ns=8,
        )


def test_sample_from_dict_and_run_contract_reject_unknown_shapes() -> None:
    sample = _valid_sample()
    payload = sample.to_dict()
    payload["extra"] = True
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplaySample.from_dict(payload)
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplaySample.from_dict({"sample_id": "only"})

    run = _valid_run()
    with pytest.raises(PlayerMarkerReplayError):
        replace(run, run_index=4)
    with pytest.raises(TypeError):
        replace(run, samples=(object(),))
    with pytest.raises(PlayerMarkerReplayError):
        replace(run, sample_order_digest="a" * 64)
    with pytest.raises(PlayerMarkerReplayError):
        replace(run, run_digest="a" * 64)
    run_payload = run.to_dict()
    run_payload["extra"] = True
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRun.from_dict(run_payload)
    run_payload = run.to_dict()
    run_payload["samples"] = {}
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRun.from_dict(run_payload)


def test_replay_config_normalises_aliases_and_rejects_conflicts(tmp_path: Path) -> None:
    base = PlayerMarkerReplayConfig(manifest={}, truth_root=tmp_path)
    assert replace(base, as_of_ns=7).as_of_offset_ns == 7
    assert replace(base, event_tape_index_path=tmp_path / "index.json").event_tape_index == (
        tmp_path / "index.json"
    )
    assert replace(base, accepted_ledger=[]).accepted_frame_ledger == []
    assert replace(base, calibration_artifact={}).calibration == {}
    assert replace(base, zero_input_audit_path=tmp_path / "audit.json").zero_input_audit == (
        tmp_path / "audit.json"
    )
    assert (
        replace(
            base,
            zero_input_audit_sha256="a" * 64,
        ).zero_input_audit_artifact_sha256
        == "a" * 64
    )
    assert replace(base, pixels_by_digest={"a" * 64: b"pixels"}).pixels_by_digest == {
        "a" * 64: b"pixels"
    }

    invalid = [
        {"verification_profile": "unknown"},
        {"as_of_ns": 1, "as_of_offset_ns": 2},
        {"max_age_ns": -1},
        {"event_tape_index": {}, "event_tape_index_path": tmp_path / "index.json"},
        {"source_commit": "short"},
        {"config_digest": "short"},
        {"extractor_artifact_digest": "short"},
        {"zero_input_audit_artifact_sha256": "short"},
        {"accepted_frame_ledger": [], "accepted_ledger": []},
        {"calibration": {}, "calibration_artifact": {}},
        {"zero_input_audit": {}, "zero_input_audit_path": tmp_path / "audit.json"},
        {
            "zero_input_audit_sha256": "a" * 64,
            "zero_input_audit_artifact_sha256": "b" * 64,
        },
        {"config": {"not_json": object()}},
        {"pixels_by_digest": {"short": b"pixels"}},
        {"pixels_by_digest": {"a" * 64: "not-bytes"}},
    ]
    for overrides in invalid:
        with pytest.raises((PlayerMarkerReplayError, TypeError, ValueError)):
            replace(base, **overrides)


def test_report_contract_rejects_metadata_and_run_shape_tampering(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    report = _runner(manifest, truth_root, cas_root, lambda frame: None).run_three_times()
    mutations: list[tuple[str, object]] = [
        ("as_of_offset_ns", report.as_of_offset_ns + 1),
        ("timing_strategy", "wrong-timing"),
        ("repeat_count", 2),
        ("deterministic", "yes"),
        ("status", "UNKNOWN"),
        ("verification_profile", "unknown"),
        ("schema_version", "9.9.9"),
        ("report_type", "other"),
        ("scope", "other"),
        ("truth_scope", "marker_accuracy"),
        ("execution_faults", ("unknown_fault",)),
        ("execution_faults", ("execution_invalid", "execution_invalid")),
        ("limitations", ("mutable limitation",)),
        ("runs", ()),
        ("runs", (1, 2, 3)),
        ("report_id", "unbound-report"),
        ("sample_count", report.sample_count + 1),
    ]
    for field, value in mutations:
        with pytest.raises((PlayerMarkerReplayError, TypeError, ValueError)):
            replace(report, **{field: value})

    empty_order = player_marker._digest([])
    empty_run = PlayerMarkerReplayRun.build(
        run_index=2,
        sample_order_digest=empty_order,
        samples=(),
    )
    with pytest.raises(PlayerMarkerReplayError):
        replace(report, runs=(report.runs[0], empty_run, report.runs[2]))

    different_sample = _valid_sample(sample_id="different")
    different_run = PlayerMarkerReplayRun.build(
        run_index=2,
        sample_order_digest=player_marker._digest(
            [different_sample.sample_id, "sample-1", "sample-2"]
        ),
        samples=(different_sample, report.runs[1].samples[1], report.runs[1].samples[2]),
    )
    with pytest.raises(PlayerMarkerReplayError):
        replace(report, runs=(report.runs[0], different_run, report.runs[2]))

    config_sample = _valid_sample(sample_id="sample-0", detector_config_digest="f" * 64)
    config_run = PlayerMarkerReplayRun.build(
        run_index=2,
        sample_order_digest=report.sample_order_digest,
        samples=(config_sample, report.runs[1].samples[1], report.runs[1].samples[2]),
    )
    with pytest.raises(PlayerMarkerReplayError):
        replace(report, runs=(report.runs[0], config_run, report.runs[2]))

    generation_sample = _valid_sample(sample_id="sample-0", generation=1)
    generation_run = PlayerMarkerReplayRun.build(
        run_index=2,
        sample_order_digest=report.sample_order_digest,
        samples=(generation_sample, report.runs[1].samples[1], report.runs[1].samples[2]),
    )
    with pytest.raises(PlayerMarkerReplayError):
        replace(report, runs=(report.runs[0], generation_run, report.runs[2]))

    with pytest.raises(PlayerMarkerReplayError):
        replace(report, report_digest="0" * 64)


def test_report_serialisation_and_public_aliases(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    report = _runner(manifest, truth_root, cas_root, lambda frame: None).run_three_times()
    assert report.source_commit == report.corpus_source_commit
    assert report.config_digest == report.extractor_config_digest
    output = report.write_json(tmp_path / "nested" / "report.json")
    assert output.exists()
    assert PlayerMarkerReplayReport.from_dict(load_strict_json(output)) == report
    assert report.to_json().endswith("\n") is False


def test_result_and_privacy_boundaries_cover_reduction_and_token_forms() -> None:
    class BrokenWithDigest:
        digest = "d" * 64

        def to_dict(self) -> object:
            raise ValueError("redacted")

    class BrokenWithoutDigest:
        def to_dict(self) -> object:
            raise ValueError("redacted")

    assert player_marker._as_json(BrokenWithDigest(), "candidate") == {"declared_digest": "d" * 64}
    with pytest.raises(PlayerMarkerReplayError):
        player_marker._as_json(BrokenWithoutDigest(), "candidate")
    with pytest.raises(PlayerMarkerReplayError):
        player_marker._as_json(object(), "candidate")

    assert player_marker._status_token("found") == "detected"
    assert player_marker._status_token("none") == "no_marker"
    assert player_marker._status_token("fault") == "fault"
    assert player_marker._status_token("future") is None
    assert player_marker._fault_token({"code": "extractor_error"}) == "extractor_error"

    class Fault:
        code = "detector_fault"

    assert player_marker._fault_token(Fault()) == "detector_fault"
    assert player_marker._fault_token("unknown") is None
    assert player_marker._fault_token(None) is None
    with pytest.raises(PlayerMarkerReplayError):
        player_marker._assert_public_privacy({"nested": [{"path": "TARGET"}]})
    with pytest.raises(PlayerMarkerReplayError):
        player_marker._assert_public_privacy({"bad key": "value"})


def test_semantic_recompute_rejects_shape_and_binding_drift() -> None:
    run = _valid_run()
    with pytest.raises(PlayerMarkerReplayError):
        player_marker._recompute_replay_semantics(
            (),
            expected_wrong_size=None,
            extractor_config_digest=None,
            as_of_offset_ns=None,
            generation=None,
        )
    with pytest.raises(PlayerMarkerReplayError):
        player_marker._recompute_replay_semantics(
            (run,),
            expected_wrong_size=(),
            extractor_config_digest=None,
            as_of_offset_ns=None,
            generation=None,
        )
    empty_run = PlayerMarkerReplayRun.build(
        run_index=2,
        sample_order_digest=player_marker._digest([]),
        samples=(),
    )
    summary = player_marker._recompute_replay_semantics(
        (run, empty_run),
        expected_wrong_size=None,
        extractor_config_digest="f" * 64,
        as_of_offset_ns=11,
        generation=1,
    )
    assert summary.deterministic is False
    assert summary.execution_valid is False
    fault_sample = PlayerMarkerReplaySample.build(
        sample_id="sample-unit",
        status="fault",
        fault="extractor_error",
        detector_result_digest="d" * 64,
        detector_state_digest="b" * 64,
        detector_config_digest="c" * 64,
        invoked=True,
        observed_at_ns=100,
        generation=0,
        as_of_offset_ns=10,
        effective_now_ns=110,
    )
    fault_run = PlayerMarkerReplayRun.build(
        run_index=1,
        sample_order_digest=player_marker._digest([fault_sample.sample_id]),
        samples=(fault_sample,),
    )
    fault_summary = player_marker._recompute_replay_semantics(
        (fault_run,),
        expected_wrong_size=(False,),
        extractor_config_digest="c" * 64,
        as_of_offset_ns=10,
        generation=0,
    )
    assert fault_summary.execution_faults == ("extractor_error",)


def _as_b2_report_payload(report: PlayerMarkerReplayReport) -> dict[str, object]:
    payload = report.to_dict()
    payload["verification_profile"] = "b2_gate"
    audit = payload["zero_input_audit"]
    assert isinstance(audit, dict)
    payload["zero_input_audit_artifact_digest"] = sha256(canonical_json(audit) + b"\n").hexdigest()
    payload["event_tape_index_artifact_digest"] = "e" * 64
    body = {key: value for key, value in payload.items() if key != "report_digest"}
    payload["report_digest"] = player_marker._digest(body)
    return payload


def test_b2_verifier_requires_external_provenance_and_canonical_manifest(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    report = _runner(manifest, truth_root, cas_root, lambda frame: None).run_three_times()
    b2_report = PlayerMarkerReplayReport.from_dict(_as_b2_report_payload(report))
    common: dict[str, object] = {
        "manifest": manifest,
        "expected_verification_profile": "b2_gate",
        "expected_extractor_artifact_digest": b2_report.extractor_artifact_digest,
        "expected_event_tape_digest": b2_report.event_tape_digest,
        "expected_event_tape_index_artifact_digest": b2_report.event_tape_index_artifact_digest,
        "expected_accepted_ledger_digest": b2_report.accepted_ledger_digest,
        "expected_calibration_artifact_digest": b2_report.calibration_artifact_digest,
        "expected_zero_input_audit_artifact_digest": b2_report.zero_input_audit_artifact_digest,
        "expected_generation": b2_report.generation,
    }
    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(b2_report)
    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(b2_report, manifest={})
    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(b2_report, **common)

    for missing in (
        "expected_verification_profile",
        "expected_extractor_artifact_digest",
        "expected_event_tape_digest",
        "expected_event_tape_index_artifact_digest",
        "expected_accepted_ledger_digest",
        "expected_calibration_artifact_digest",
        "expected_zero_input_audit_artifact_digest",
        "expected_generation",
    ):
        arguments = dict(common)
        arguments.pop(missing)
        with pytest.raises(PlayerMarkerReplayError):
            verify_player_marker_replay_report(b2_report, **arguments)

    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(
            b2_report,
            manifest=manifest,
            expected_verification_profile="contract_fixture",
        )
    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(
            b2_report,
            manifest=manifest,
            expected_verification_profile="invalid",
        )


def test_verifier_rejects_each_external_digest_and_metadata_mismatch(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    report = _runner(manifest, truth_root, cas_root, lambda frame: None).run_three_times()
    mismatch = {
        "expected_extractor_artifact_digest": "0" * 64,
        "expected_event_tape_digest": "0" * 64,
        "expected_event_tape_index_artifact_digest": "0" * 64,
        "expected_accepted_ledger_digest": "0" * 64,
        "expected_calibration_artifact_digest": "0" * 64,
        "expected_zero_input_audit_artifact_digest": "0" * 64,
        "expected_generation": 1,
        "corpus_source_commit": "b" * 40,
        "replay_source_commit": "b" * 40,
        "manifest_digest": "0" * 64,
        "config_digest": "0" * 64,
        "as_of_offset_ns": 11,
        "sample_order": ["wrong"],
    }
    for key, value in mismatch.items():
        with pytest.raises((PlayerMarkerReplayError, PlayerMarkerReplayDeterminismError)):
            verify_player_marker_replay_report(report, **{key: value})
    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(report, as_of_ns=10, as_of_offset_ns=11)


def test_verifier_binds_canonical_manifest_and_admission_shape(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    report = _runner(manifest, truth_root, cas_root, lambda frame: None).run_three_times()
    verify_player_marker_replay_report(report, manifest=manifest)
    payload = load_strict_json(manifest)

    malformed: list[dict[str, object]] = []
    malformed.append({**payload, "samples": []})
    malformed.append({**payload, "samples": {}})
    duplicate = copy.deepcopy(payload)
    duplicate["samples"][1]["sample_id"] = duplicate["samples"][0]["sample_id"]
    malformed.append(duplicate)
    wrong_bool = copy.deepcopy(payload)
    wrong_bool["samples"][0]["wrong_size_negative"] = "false"
    malformed.append(wrong_bool)
    missing_truth = copy.deepcopy(payload)
    del missing_truth["samples"][0]["truth_path"]
    malformed.append(missing_truth)
    unsafe_truth = copy.deepcopy(payload)
    unsafe_truth["samples"][0]["truth_path"] = "../truth.json"
    malformed.append(unsafe_truth)
    mismatched_identity = copy.deepcopy(payload)
    mismatched_identity["samples"][0]["truth_id"] = "other-truth"
    malformed.append(mismatched_identity)
    unsupported_admission = copy.deepcopy(payload)
    unsupported_admission["samples"][0]["wrong_size_negative"] = True
    malformed.append(unsupported_admission)
    for index, variant in enumerate(malformed):
        variant_path = truth_root / f"manifest-variant-{index}.json"
        _write_json(variant_path, variant)
        with pytest.raises(PlayerMarkerReplayError):
            verify_player_marker_replay_report(report, manifest=variant_path)

    noncanonical = truth_root / "manifest-noncanonical.json"
    noncanonical.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PlayerMarkerReplayError):
        verify_player_marker_replay_report(report, manifest=noncanonical)


def test_runner_constructor_rejects_ambiguous_alias_inputs(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    common = {
        "extractor": lambda frame: None,
        "replay_source_commit": "c" * 40,
        "config": {"detector": "injected-v1"},
        "extractor_artifact_digest": "d" * 64,
    }
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(
            manifest,
            event_tapes=inputs["event_tapes"],
            event_tape_paths=inputs["event_tapes"],
            **common,
        )
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(
            manifest,
            event_tape_index=inputs["event_tape_index"],
            event_tape_index_path=inputs["event_tape_index"],
            **common,
        )
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(
            manifest,
            accepted_frame_ledger=inputs["accepted_frame_ledger"],
            accepted_ledger=inputs["accepted_frame_ledger"],
            **common,
        )
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(
            manifest,
            calibration=inputs["calibration"],
            calibration_artifact=inputs["calibration"],
            **common,
        )
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(
            manifest,
            zero_input_audit=inputs["zero_input_audit"],
            zero_input_audit_path=tmp_path / "audit.json",
            **common,
        )
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(
            manifest,
            zero_input_audit_artifact_sha256="a" * 64,
            zero_input_audit_sha256="b" * 64,
            **common,
        )

    config = PlayerMarkerReplayConfig(manifest=manifest)
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(config, truth_root=truth_root, extractor=lambda frame: None)


def test_load_corpus_rejects_file_and_in_memory_manifest_shapes(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    common = {
        "verification_profile": "contract_fixture",
        "truth_root": truth_root,
        "cas_root": cas_root,
        "event_tapes": inputs["event_tapes"],
        "event_tape_index": inputs["event_tape_index"],
        "extractor": lambda frame: None,
        "replay_source_commit": "c" * 40,
        "config": {"detector": "injected-v1"},
        "extractor_artifact_digest": "d" * 64,
        "accepted_frame_ledger": inputs["accepted_frame_ledger"],
        "calibration": inputs["calibration"],
        "zero_input_audit": inputs["zero_input_audit"],
        "as_of_offset_ns": inputs["as_of_offset_ns"],
    }
    invalid_file = truth_root / "invalid-manifest.json"
    invalid_file.write_text("{", encoding="utf-8")
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(invalid_file, **common)
    noncanonical = truth_root / "noncanonical-manifest.json"
    noncanonical.write_text(json.dumps(load_strict_json(manifest)), encoding="utf-8")
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(noncanonical, **common)
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(
            {},
            verification_profile="contract_fixture",
            extractor=lambda frame: None,
        )
    with pytest.raises(PlayerMarkerReplayError):
        PlayerMarkerReplayRunner(
            manifest,
            verification_profile="b2_gate",
            truth_root=truth_root,
            extractor=lambda frame: None,
        )

    monkeypatch.setattr(player_marker, "verify_corpus_manifest", lambda *args, **kwargs: None)
    payload = load_strict_json(manifest)
    variants: list[dict[str, object]] = [
        {**payload, "samples": []},
        {**payload, "samples": {}},
    ]
    bad_sequence = copy.deepcopy(payload)
    bad_sequence["samples"][0]["sequence"] = "zero"
    variants.append(bad_sequence)
    bad_truth_path = copy.deepcopy(payload)
    bad_truth_path["samples"][0]["truth_path"] = None
    variants.append(bad_truth_path)
    missing_truth = copy.deepcopy(payload)
    missing_truth["samples"][0]["truth_path"] = "missing.json"
    variants.append(missing_truth)
    bad_identity = copy.deepcopy(payload)
    bad_identity["samples"][0]["truth_id"] = "other"
    variants.append(bad_identity)
    bad_bool = copy.deepcopy(payload)
    bad_bool["samples"][0]["wrong_size_negative"] = None
    variants.append(bad_bool)
    bad_digest = copy.deepcopy(payload)
    bad_digest["corpus_digest"] = "0" * 64
    variants.append(bad_digest)
    for variant in variants:
        with pytest.raises(PlayerMarkerReplayError):
            PlayerMarkerReplayRunner(
                variant,
                verification_profile="contract_fixture",
                truth_root=truth_root,
                extractor=lambda frame: None,
            )


def test_event_tape_index_rejects_provenance_and_path_variants(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    index_path = inputs["event_tape_index"]
    assert isinstance(index_path, Path)
    base_index = load_strict_json(index_path)
    event_tapes = inputs["event_tapes"]

    def make_runner(index: object) -> PlayerMarkerReplayRunner:
        return PlayerMarkerReplayRunner(
            manifest,
            verification_profile="contract_fixture",
            truth_root=truth_root,
            cas_root=cas_root,
            event_tapes=event_tapes,
            event_tape_index=index,
            extractor=lambda frame: None,
            replay_source_commit="c" * 40,
            config={"detector": "injected-v1"},
            extractor_artifact_digest="d" * 64,
            accepted_frame_ledger=inputs["accepted_frame_ledger"],
            calibration=inputs["calibration"],
            zero_input_audit=inputs["zero_input_audit"],
            as_of_offset_ns=inputs["as_of_offset_ns"],
        )

    # Structured contract indexes exercise the in-memory form, while their
    # metadata remains bound to every tape/session.
    assert make_runner(base_index)._event_tape_index_artifact_digest is not None
    structured_unknown_session = copy.deepcopy(base_index)
    structured_unknown_session["tapes"][0]["session_id"] = "unknown-session"
    structured_unknown_session["index_digest"] = canonical_digest(
        structured_unknown_session, omit=("index_digest",)
    )
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(structured_unknown_session)

    variants: list[tuple[str, object, bool]] = []
    missing_key = copy.deepcopy(base_index)
    del missing_key["schema_version"]
    variants.append(("missing-key", missing_key, True))
    wrong_schema = copy.deepcopy(base_index)
    wrong_schema["schema_version"] = "9.9.9"
    variants.append(("wrong-schema", wrong_schema, True))
    wrong_source = copy.deepcopy(base_index)
    wrong_source["source_commit"] = "b" * 40
    variants.append(("wrong-source", wrong_source, True))
    wrong_corpus = copy.deepcopy(base_index)
    wrong_corpus["corpus_digest"] = "0" * 64
    variants.append(("wrong-corpus", wrong_corpus, True))
    wrong_index_digest = copy.deepcopy(base_index)
    wrong_index_digest["index_digest"] = "0" * 64
    variants.append(("wrong-index-digest", wrong_index_digest, False))
    not_array = copy.deepcopy(base_index)
    not_array["tapes"] = {}
    variants.append(("tapes-object", not_array, True))
    empty_array = copy.deepcopy(base_index)
    empty_array["tapes"] = []
    variants.append(("tapes-empty", empty_array, True))
    bad_entry_keys = copy.deepcopy(base_index)
    bad_entry_keys["tapes"][0]["extra"] = True
    variants.append(("entry-keys", bad_entry_keys, True))
    bad_path = copy.deepcopy(base_index)
    bad_path["tapes"][0]["path"] = "../escape.jsonl"
    variants.append(("entry-path", bad_path, True))
    bad_digest = copy.deepcopy(base_index)
    bad_digest["tapes"][0]["sha256"] = "short"
    variants.append(("entry-sha", bad_digest, True))
    bad_size = copy.deepcopy(base_index)
    bad_size["tapes"][0]["size_bytes"] = 0
    variants.append(("entry-size", bad_size, True))
    duplicate_path = copy.deepcopy(base_index)
    duplicate_path["tapes"][1]["path"] = duplicate_path["tapes"][0]["path"]
    variants.append(("duplicate-path", duplicate_path, True))
    duplicate_session = copy.deepcopy(base_index)
    duplicate_session["tapes"][1]["session_id"] = duplicate_session["tapes"][0]["session_id"]
    variants.append(("duplicate-session", duplicate_session, True))
    wrong_count = copy.deepcopy(base_index)
    wrong_count["event_count"] += 1
    variants.append(("event-count", wrong_count, True))

    for name, variant, resign in variants:
        if resign:
            variant["index_digest"] = canonical_digest(variant, omit=("index_digest",))
        path = truth_root / f"index-{name}.json"
        _write_json(path, variant)
        with pytest.raises((PlayerMarkerReplayError, ValueError, TypeError)):
            make_runner(path)

    configured_mismatch = copy.deepcopy(base_index)
    configured_mismatch["tapes"][0]["path"] = "missing.jsonl"
    configured_mismatch["index_digest"] = canonical_digest(
        configured_mismatch, omit=("index_digest",)
    )
    configured_path = truth_root / "index-configured-mismatch.json"
    _write_json(configured_path, configured_mismatch)
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(configured_path)


def test_event_tape_index_rejects_tape_artifact_and_session_content_variants(
    tmp_path: Path,
) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    base_index_path = inputs["event_tape_index"]
    assert isinstance(base_index_path, Path)
    base_index = load_strict_json(base_index_path)
    tapes = list(inputs["event_tapes"])

    def make_runner(index_path: Path, configured_tapes: list[Path]) -> PlayerMarkerReplayRunner:
        return PlayerMarkerReplayRunner(
            manifest,
            verification_profile="contract_fixture",
            truth_root=truth_root,
            cas_root=cas_root,
            event_tapes=configured_tapes,
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

    metadata_mismatch = copy.deepcopy(base_index)
    metadata_mismatch["tapes"][0]["sha256"] = "0" * 64
    metadata_mismatch["index_digest"] = canonical_digest(metadata_mismatch, omit=("index_digest",))
    metadata_path = truth_root / "index-metadata-mismatch.json"
    _write_json(metadata_path, metadata_mismatch)
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(metadata_path, tapes)

    empty_tape = truth_root / "empty-indexed.jsonl"
    empty_tape.write_bytes(b"")
    empty_index = copy.deepcopy(base_index)
    empty_index["tapes"][0].update({"path": empty_tape.name, "sha256": "0" * 64, "size_bytes": 1})
    empty_index["index_digest"] = canonical_digest(empty_index, omit=("index_digest",))
    empty_index_path = truth_root / "index-empty-tape.json"
    _write_json(empty_index_path, empty_index)
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(empty_index_path, [empty_tape, *tapes[1:]])

    malformed_tape = truth_root / "malformed-indexed.jsonl"
    malformed_tape.write_bytes(b"not-json\n")
    malformed_index = copy.deepcopy(base_index)
    malformed_index["tapes"][0].update(
        {
            "path": malformed_tape.name,
            "sha256": sha256(malformed_tape.read_bytes()).hexdigest(),
            "size_bytes": malformed_tape.stat().st_size,
        }
    )
    malformed_index["index_digest"] = canonical_digest(malformed_index, omit=("index_digest",))
    malformed_path = truth_root / "index-malformed-tape.json"
    _write_json(malformed_path, malformed_index)
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(malformed_path, [malformed_tape, *tapes[1:]])


def test_event_tape_payload_validation_rejects_resigned_lineage_variants(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    originals = list(inputs["event_tapes"])
    first_record = EventTape(originals[0]).read_all()[0]

    def rewritten(
        name: str,
        *,
        event_type: str | None = None,
        frame_id: int | None = None,
        payload_updates: dict[str, object] | None = None,
    ) -> Path:
        payload = dict(first_record.payload)
        if payload_updates:
            payload.update(payload_updates)
        path = truth_root / f"{name}.jsonl"
        EventTape(path).append(
            event_type=event_type or first_record.event_type,
            payload=payload,
            session_id=first_record.session_id,
            frame_id=first_record.frame_id if frame_id is None else frame_id,
            world_state_version=first_record.world_state_version,
            recorded_at_ns=first_record.recorded_at_ns,
        )
        return path

    def make_runner(first_tape: Path) -> PlayerMarkerReplayRunner:
        return _runner(
            manifest,
            truth_root,
            cas_root,
            lambda frame: None,
            event_tapes=(first_tape, *originals[1:]),
            event_tape_index=None,
        )

    variants = [
        rewritten("wrong-frame", frame_id=99),
        rewritten("wrong-scope", payload_updates={"truth_scope": "other"}),
        rewritten("wrong-truth-pixel", payload_updates={"truth_pixel_digest": "0" * 64}),
        rewritten("wrong-event-type", event_type="frame.fatal"),
        rewritten("wrong-admission", payload_updates={"admission_status": "other"}),
    ]
    for path in variants:
        with pytest.raises(PlayerMarkerReplayError):
            make_runner(path)

    wrong_size_originals = list(originals)
    wrong_size_record = EventTape(originals[2]).read_all()[0]

    def rewritten_wrong_size(name: str, *, event_type: str, updates: dict[str, object]) -> Path:
        payload = dict(wrong_size_record.payload)
        payload.update(updates)
        path = truth_root / f"{name}.jsonl"
        EventTape(path).append(
            event_type=event_type,
            payload=payload,
            session_id=wrong_size_record.session_id,
            frame_id=wrong_size_record.frame_id,
            world_state_version=wrong_size_record.world_state_version,
            recorded_at_ns=wrong_size_record.recorded_at_ns,
        )
        return path

    wrong_size_type = rewritten_wrong_size(
        "wrong-size-type",
        event_type="frame.accepted",
        updates={},
    )
    with pytest.raises(PlayerMarkerReplayError):
        _runner(
            manifest,
            truth_root,
            cas_root,
            lambda frame: None,
            event_tapes=(*wrong_size_originals[:2], wrong_size_type),
            event_tape_index=None,
        )
    wrong_size_payload = rewritten_wrong_size(
        "wrong-size-payload",
        event_type=wrong_size_record.event_type,
        updates={"plan_suppressed": False},
    )
    with pytest.raises(PlayerMarkerReplayError):
        _runner(
            manifest,
            truth_root,
            cas_root,
            lambda frame: None,
            event_tapes=(*wrong_size_originals[:2], wrong_size_payload),
            event_tape_index=None,
        )

    extra = truth_root / "extra-record.jsonl"
    tape = EventTape(extra)
    tape.append(
        event_type=first_record.event_type,
        payload=dict(first_record.payload),
        session_id=first_record.session_id,
        frame_id=first_record.frame_id,
        world_state_version=first_record.world_state_version,
        recorded_at_ns=first_record.recorded_at_ns,
    )
    tape.append(
        event_type="frame.accepted",
        payload={"truth_scope": "frame_ingestion_only", "truth_id": "extra"},
        session_id=first_record.session_id,
        frame_id=99,
        world_state_version=first_record.world_state_version,
        recorded_at_ns=first_record.recorded_at_ns + 1,
    )
    with pytest.raises(PlayerMarkerReplayError):
        _runner(
            manifest,
            truth_root,
            cas_root,
            lambda frame: None,
            event_tapes=(extra, *originals[1:]),
            event_tape_index=None,
        )


def test_accepted_ledger_loader_supports_public_forms_and_rejects_bad_rows(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    rows = copy.deepcopy(inputs["accepted_frame_ledger"])
    assert isinstance(rows, list)

    def make_runner(ledger: object) -> PlayerMarkerReplayRunner:
        return PlayerMarkerReplayRunner(
            manifest,
            verification_profile="contract_fixture",
            truth_root=truth_root,
            cas_root=cas_root,
            event_tapes=inputs["event_tapes"],
            event_tape_index=inputs["event_tape_index"],
            extractor=lambda frame: None,
            replay_source_commit="c" * 40,
            config={"detector": "injected-v1"},
            extractor_artifact_digest="d" * 64,
            accepted_frame_ledger=ledger,
            calibration=inputs["calibration"],
            zero_input_audit=inputs["zero_input_audit"],
            as_of_offset_ns=inputs["as_of_offset_ns"],
        )

    assert make_runner(rows).sample_order == ("sample-0", "sample-1", "sample-2")
    assert make_runner({"rows": rows}).sample_order == ("sample-0", "sample-1", "sample-2")
    ledger_path = truth_root / "ledger.jsonl"
    ledger_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    assert make_runner(ledger_path).sample_order[0] == "sample-0"

    malformed_rows: list[object] = [
        [],
        {},
    ]
    missing_required = copy.deepcopy(rows[0])
    del missing_required["clock_domain"]
    malformed_rows.append([missing_required, *rows[1:]])
    bad_sequence = copy.deepcopy(rows[0])
    bad_sequence["source_sequence"] = "zero"
    malformed_rows.append([bad_sequence, *rows[1:]])
    bad_captured = copy.deepcopy(rows[0])
    bad_captured["captured_at_ns"] = -1
    malformed_rows.append([bad_captured, *rows[1:]])
    bad_admitted = copy.deepcopy(rows[0])
    bad_admitted["admitted_at_ns"] = 0
    malformed_rows.append([bad_admitted, *rows[1:]])
    bad_received = copy.deepcopy(rows[0])
    bad_received["received_at_ns"] = 0
    malformed_rows.append([bad_received, *rows[1:]])
    bad_age = copy.deepcopy(rows[0])
    bad_age["age_ns"] = 99
    malformed_rows.append([bad_age, *rows[1:]])
    negative_age = copy.deepcopy(rows[0])
    negative_age["age_ns"] = -1
    malformed_rows.append([negative_age, *rows[1:]])
    empty_clock = copy.deepcopy(rows[0])
    empty_clock["clock_domain"] = ""
    malformed_rows.append([empty_clock, *rows[1:]])
    bad_pixel_digest = copy.deepcopy(rows[0])
    bad_pixel_digest["pixel_digest"] = "short"
    malformed_rows.append([bad_pixel_digest, *rows[1:]])
    bad_image_ref = copy.deepcopy(rows[0])
    bad_image_ref["image_ref"] = "not-cas"
    malformed_rows.append([bad_image_ref, *rows[1:]])
    mismatched_image_ref = copy.deepcopy(rows[0])
    mismatched_image_ref["image_ref"] = "cas://sha256/" + "0" * 64
    malformed_rows.append([mismatched_image_ref, *rows[1:]])
    bad_width = copy.deepcopy(rows[0])
    bad_width["source_width"] = 0
    malformed_rows.append([bad_width, *rows[1:]])
    bad_height = copy.deepcopy(rows[0])
    bad_height["source_height"] = 0
    malformed_rows.append([bad_height, *rows[1:]])
    bad_retained = copy.deepcopy(rows[0])
    bad_retained["retained"] = "yes"
    malformed_rows.append([bad_retained, *rows[1:]])
    bad_max_age = copy.deepcopy(rows[0])
    bad_max_age["max_age_ns"] = -1
    malformed_rows.append([bad_max_age, *rows[1:]])
    duplicate = copy.deepcopy(rows)
    duplicate.append(copy.deepcopy(rows[0]))
    malformed_rows.append(duplicate)
    for malformed in malformed_rows:
        with pytest.raises((PlayerMarkerReplayError, TypeError, ValueError)):
            make_runner(malformed)

    with pytest.raises(PlayerMarkerReplayError):
        make_runner(None)
    absent = truth_root / "missing-ledger.jsonl"
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(absent)
    invalid_utf8 = truth_root / "invalid-ledger.jsonl"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(invalid_utf8)
    blank = truth_root / "blank-ledger.jsonl"
    blank.write_text("\n", encoding="utf-8")
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(blank)
    invalid_json = truth_root / "invalid-json-ledger.jsonl"
    invalid_json.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(invalid_json)
    duplicate_json = truth_root / "duplicate-json-ledger.jsonl"
    duplicate_json.write_text(
        '{"session_id":"session-0","session_id":"session-0"}\n', encoding="utf-8"
    )
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(duplicate_json)


def test_calibration_loader_supports_artifact_and_rejects_invalid_geometry(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    calibration = copy.deepcopy(inputs["calibration"])
    assert isinstance(calibration, dict)

    def make_runner(value: object, **kwargs: object) -> PlayerMarkerReplayRunner:
        return PlayerMarkerReplayRunner(
            manifest,
            verification_profile="contract_fixture",
            truth_root=truth_root,
            cas_root=cas_root,
            event_tapes=inputs["event_tapes"],
            event_tape_index=inputs["event_tape_index"],
            extractor=lambda frame: None,
            replay_source_commit="c" * 40,
            config={"detector": "injected-v1"},
            extractor_artifact_digest="d" * 64,
            accepted_frame_ledger=inputs["accepted_frame_ledger"],
            calibration=value,
            zero_input_audit=inputs["zero_input_audit"],
            as_of_offset_ns=inputs["as_of_offset_ns"],
            **kwargs,
        )

    assert make_runner(calibration).sample_order[0] == "sample-0"
    calibration_path = truth_root / "calibration.json"
    _write_json(calibration_path, calibration)
    assert make_runner(calibration_path).sample_order[-1] == "sample-2"
    no_max_age = copy.deepcopy(calibration)
    del no_max_age["max_age_ns"]
    assert make_runner(no_max_age).sample_order[0] == "sample-0"

    invalid: list[object] = [
        None,
        {},
        {**calibration, "geometry": None},
        {**calibration, "transform_version": 1},
        {**calibration, "transform_version": ""},
        {**calibration, "calibration_sha256": "0" * 64},
        {**calibration, "max_age_ns": -1},
    ]
    invalid_geometry = copy.deepcopy(calibration)
    invalid_geometry["geometry"] = {"source_size": {"width": 0, "height": 1}}
    invalid.append(invalid_geometry)
    for value in invalid:
        with pytest.raises((PlayerMarkerReplayError, TypeError, ValueError)):
            make_runner(value)
    bad_path = truth_root / "bad-calibration.json"
    bad_path.write_text("{", encoding="utf-8")
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(bad_path)


def test_zero_input_audit_is_verified_from_mapping_and_canonical_file(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)
    audit = copy.deepcopy(inputs["zero_input_audit"])
    assert isinstance(audit, dict)

    def make_runner(value: object, **kwargs: object) -> PlayerMarkerReplayRunner:
        return PlayerMarkerReplayRunner(
            manifest,
            verification_profile="contract_fixture",
            truth_root=truth_root,
            cas_root=cas_root,
            event_tapes=inputs["event_tapes"],
            event_tape_index=inputs["event_tape_index"],
            extractor=lambda frame: None,
            replay_source_commit="c" * 40,
            config={"detector": "injected-v1"},
            extractor_artifact_digest="d" * 64,
            accepted_frame_ledger=inputs["accepted_frame_ledger"],
            calibration=inputs["calibration"],
            zero_input_audit=value,
            as_of_offset_ns=inputs["as_of_offset_ns"],
            **kwargs,
        )

    audit_path = truth_root / "zero-audit.json"
    _write_json(audit_path, audit)
    audit_sha = sha256(audit_path.read_bytes()).hexdigest()
    assert make_runner(audit_path, zero_input_audit_artifact_sha256=audit_sha).sample_order[0] == (
        "sample-0"
    )

    variants: list[dict[str, object]] = []
    missing_key = copy.deepcopy(audit)
    del missing_key["status"]
    variants.append(missing_key)
    extra_key = copy.deepcopy(audit)
    extra_key["extra"] = True
    variants.append(extra_key)
    wrong_schema = copy.deepcopy(audit)
    wrong_schema["schema_version"] = "9.9.9"
    variants.append(wrong_schema)
    wrong_type = copy.deepcopy(audit)
    wrong_type["report_type"] = "other"
    variants.append(wrong_type)
    wrong_status = copy.deepcopy(audit)
    wrong_status["status"] = "FAIL"
    variants.append(wrong_status)
    wrong_owner = copy.deepcopy(audit)
    wrong_owner["input_owner"] = "new"
    variants.append(wrong_owner)
    real_input = copy.deepcopy(audit)
    real_input["real_input_enabled"] = True
    variants.append(real_input)
    wrong_commit = copy.deepcopy(audit)
    wrong_commit["source_commit"] = "b" * 40
    variants.append(wrong_commit)
    invalid_commit = copy.deepcopy(audit)
    invalid_commit["source_commit"] = "short"
    variants.append(invalid_commit)
    invalid_report_digest = copy.deepcopy(audit)
    invalid_report_digest["report_digest"] = "short"
    variants.append(invalid_report_digest)
    invalid_wheel = copy.deepcopy(audit)
    invalid_wheel["wheel_sha256"] = "short"
    variants.append(invalid_wheel)
    nonzero_counter = copy.deepcopy(audit)
    nonzero_counter["failure_count"] = 1
    variants.append(nonzero_counter)
    wrong_report_digest = copy.deepcopy(audit)
    wrong_report_digest["report_digest"] = "0" * 64
    variants.append(wrong_report_digest)
    for variant in variants:
        with pytest.raises(PlayerMarkerReplayError):
            make_runner(variant)

    with pytest.raises(PlayerMarkerReplayError):
        make_runner(None)
    noncanonical = truth_root / "zero-audit-noncanonical.json"
    noncanonical.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(noncanonical)
    with pytest.raises(PlayerMarkerReplayError):
        make_runner(audit_path, zero_input_audit_artifact_sha256="0" * 64)


def test_extractor_config_and_artifact_resolution_is_recomputed(tmp_path: Path) -> None:
    manifest, truth_root, cas_root = _make_fixture(tmp_path)
    inputs = _replay_inputs(manifest, truth_root)

    def make_runner(extractor: object, **kwargs: object) -> PlayerMarkerReplayRunner:
        config = kwargs.pop("config", {"detector": "injected-v1"})
        artifact = kwargs.pop("extractor_artifact_digest", "d" * 64)
        return PlayerMarkerReplayRunner(
            manifest,
            verification_profile="contract_fixture",
            truth_root=truth_root,
            cas_root=cas_root,
            event_tapes=inputs["event_tapes"],
            event_tape_index=inputs["event_tape_index"],
            extractor=extractor,
            replay_source_commit="c" * 40,
            config=config,
            extractor_artifact_digest=artifact,
            accepted_frame_ledger=inputs["accepted_frame_ledger"],
            calibration=inputs["calibration"],
            zero_input_audit=inputs["zero_input_audit"],
            as_of_offset_ns=inputs["as_of_offset_ns"],
            **kwargs,
        )

    class MappingExtractor:
        config: ClassVar[dict[str, str]] = {"detector": "injected-v1"}

        def extract(self, frame: FramePacket) -> object:
            return None

    class ConfigObject:
        def to_dict(self) -> dict[str, str]:
            return {"detector": "injected-v1"}

    class ObjectExtractor:
        config = ConfigObject()
        artifact_digest = "e" * 64

        def extract(self, frame: FramePacket) -> object:
            return None

    assert make_runner(MappingExtractor()).extractor_config_digest == player_marker._digest(
        {"detector": "injected-v1"}
    )
    assert (
        make_runner(ObjectExtractor(), extractor_artifact_digest=None).extractor_artifact_digest
        == "e" * 64
    )
    assert make_runner(lambda frame: None).extractor_config_digest == player_marker._digest(
        {"detector": "injected-v1"}
    )

    class BadConfigExtractor:
        config = object()

        def extract(self, frame: FramePacket) -> object:
            return None

    invalid: list[tuple[object, dict[str, object]]] = [
        (lambda frame: None, {"config": None}),
        (BadConfigExtractor(), {}),
        (MappingExtractor(), {"config": {"detector": "other"}}),
        (MappingExtractor(), {"config_digest": "0" * 64}),
        (MappingExtractor(), {"extractor_artifact_digest": "short"}),
        (lambda frame: None, {"extractor_artifact_digest": None, "config": {"detector": "x"}}),
    ]
    for extractor, kwargs in invalid:
        with pytest.raises((PlayerMarkerReplayError, TypeError, ValueError)):
            make_runner(extractor, **kwargs)

    class DeclaredConfig:
        digest = "f" * 64

        def to_dict(self) -> dict[str, str]:
            return {"detector": "injected-v1"}

    class DeclaredExtractor:
        config = DeclaredConfig()

        def extract(self, frame: FramePacket) -> object:
            return None

    with pytest.raises(PlayerMarkerReplayError):
        make_runner(DeclaredExtractor(), extractor_artifact_digest="d" * 64)

    class InvalidDeclaredConfig:
        digest = "short"

        def to_dict(self) -> dict[str, str]:
            return {"detector": "injected-v1"}

    class InvalidDeclaredExtractor:
        config = InvalidDeclaredConfig()

        def extract(self, frame: FramePacket) -> object:
            return None

    with pytest.raises(PlayerMarkerReplayError):
        make_runner(InvalidDeclaredExtractor())

    class NonCallableExtract:
        extract = object()
        config: ClassVar[dict[str, str]] = {"detector": "injected-v1"}

    with pytest.raises(PlayerMarkerReplayError):
        make_runner(NonCallableExtract())


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
