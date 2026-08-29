from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tools.build_g1_frame_candidate import build_candidate_packet, canonical_packet_digest
from tools.bundle_common import sha256_file
from tools.verify_g1_frame_candidate import _capture_formats_match, verify_g1_frame_candidate
from tools.verify_hardware_smoke_report import (
    ZERO_FAILURE_FIELDS,
    canonical_report_digest,
    verify_hardware_smoke_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _hardware_report(tmp_path: Path, *, fps: float = 30.0) -> dict[str, object]:
    fmt: dict[str, object] = {
        "width": 1920,
        "height": 1080,
        "fps": fps,
        "fourcc": "MJPG",
        "backend": "dshow",
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": 5760,
        "length": 6_220_800,
    }
    report: dict[str, object] = {
        "schema_version": "1.0.0",
        "report_type": "vc003_hardware_smoke",
        "report_id": "hardware-smoke-001",
        "generated_at": "2026-08-29T00:00:00Z",
        "status": "PASS",
        "source_commit": "a" * 40,
        "tool_artifact_sha256": "1" * 64,
        "wheel_sha256": "b" * 64,
        "dependency_lock_sha256": "c" * 64,
        "config_sha256": "d" * 64,
        "calibration_sha256": "e" * 64,
        "ci_run": {"run_id": "1", "run_attempt": "1", "workflow": "b1", "status": "success"},
        "source": {
            "logical_source_id": "capture-card-primary",
            "selector": "VC-003 Video",
            "device_fingerprint_sha256": "f" * 64,
            "requested": fmt,
            "negotiated": fmt,
            "timestamp_origin": "host_monotonic_post_retrieve",
            "clock_domain": "monotonic",
            "upstream_queue": "unknown",
            "upstream_queue_depth": "unknown",
        },
        "measurement_window": {
            "warmup_seconds": 30.0,
            "measurement_start_ns": 1_000_000_000,
            "measurement_end_ns": 301_000_000_000,
            "measured_seconds": 300.0,
            "continuous": True,
            "session_id": "session-001",
        },
        "metrics": {
            "successful_frames": 9000,
            "admitted_frames": 4500,
            "no_frame_polls": 0,
            "timeout_count": 0,
            "capture_rate_fps": 30.0,
            "admission_rate_fps": 15.0,
            "max_inter_frame_gap_ns": 100_000_000,
            "min_inter_frame_gap_ns": 100_000_000,
            "max_accepted_age_ns": 100_000_000,
            "min_accepted_age_ns": 0,
        },
        "failure_counts": {name: 0 for name in ZERO_FAILURE_FIELDS},
        "raw_slot": {
            "produced": 9000,
            "delivered": 9000,
            "superseded": 0,
            "pending": 0,
            "discarded_on_reset": 0,
            "max_depth": 1,
            "reset_count": 0,
            "last_produced_sequence": 9000,
            "final_delivered_sequence": 9000,
            "final_drain_performed": True,
            "final_drain_matches_last_produced": True,
        },
        "process": {
            "capture_thread_alive": False,
            "backend_child_alive": False,
            "residual_thread_count": 0,
            "residual_child_count": 0,
            "stop_elapsed_seconds": 0.1,
        },
        "input_audit": {
            "input_owner": "legacy",
            "real_input_enabled": False,
            "real_input_call_count": 0,
            "core_v2_real_input_call_count": 0,
            "receiver_connect_count": 0,
            "window_write_count": 0,
            "keyboard_call_count": 0,
            "mouse_call_count": 0,
            "double_write_event_count": 0,
        },
        "privacy_audit": {
            "status": "PASS",
            "raw_artifacts_public": False,
            "pii_findings": 0,
            "failure_count": 0,
        },
        "failures": [],
        "limitations": ["upstream queue depth is unknown"],
        "artifacts": [],
    }
    report["report_digest"] = canonical_report_digest(report)
    return report


def test_hardware_schema_is_strict_and_report_digest_excludes_itself() -> None:
    schema = json.loads(
        (PROJECT_ROOT / "schemas" / "vc003-hardware-smoke-report.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    report = _hardware_report(PROJECT_ROOT)
    assert list(Draft202012Validator(schema).iter_errors(report)) == []
    assert report["report_digest"] == canonical_report_digest(report)
    tampered = dict(report)
    tampered["status"] = "FAIL"
    tampered["report_digest"] = canonical_report_digest(tampered)
    assert verify_hardware_smoke_report(tampered)  # status is derived, not trusted


def test_hardware_recomputes_rate_and_rejects_rounded_up_29_97() -> None:
    reporting_noise = _hardware_report(PROJECT_ROOT, fps=30.00003)
    reporting_noise["report_digest"] = canonical_report_digest(reporting_noise)
    assert verify_hardware_smoke_report(reporting_noise) == []

    report = _hardware_report(PROJECT_ROOT, fps=29.97)
    report["source"] = dict(report["source"])  # type: ignore[arg-type]
    cast_source = report["source"]
    assert isinstance(cast_source, dict)
    cast_source["negotiated"] = dict(cast_source["negotiated"])
    cast_source["negotiated"]["fps"] = 29.97
    report["report_digest"] = canonical_report_digest(report)
    errors = verify_hardware_smoke_report(report)
    assert any("29.97" in error for error in errors)

    rate_report = _hardware_report(PROJECT_ROOT)
    metrics = dict(rate_report["metrics"])  # type: ignore[arg-type]
    metrics["successful_frames"] = 8969
    metrics["capture_rate_fps"] = 29.89
    rate_report["metrics"] = metrics
    rate_report["report_digest"] = canonical_report_digest(rate_report)
    assert any("capture rate" in error for error in verify_hardware_smoke_report(rate_report))


def test_candidate_capture_format_cross_link_allows_only_sub_millihertz_fps_noise() -> None:
    requested = {
        "backend": "dshow",
        "channels": 3,
        "dtype": "uint8",
        "fourcc": "MJPG",
        "fps": 30.0,
        "height": 1080,
        "length": 6_220_800,
        "pixel_format": "BGR8",
        "stride": 5760,
        "width": 1920,
    }
    negotiated = {**requested, "fps": 30.00003000003}

    assert _capture_formats_match(requested, negotiated)
    assert not _capture_formats_match(requested, {**negotiated, "fps": 29.97})
    assert not _capture_formats_match(requested, {**negotiated, "fourcc": "YUY2"})


def _candidate_tree(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name, data in {
        "wheel.whl": b"wheel",
        "lock.txt": b"lock",
        "config.json": b"{}",
        "calibration.json": b"{}",
        "pixel-contract.json": b'{"schema_version":"1.0.0"}',
        "g0-packet.json": b"g0 packet",
        "g0-bundle.json": b"g0 bundle",
    }.items():
        (tmp_path / name).write_bytes(data)
    artifacts: dict[str, object] = {}
    for role in (
        "source_provenance",
        "frame_ledger",
        "corpus_manifest",
        "truth_set",
        "deterministic_replay",
        "event_tape",
        "provenance_audit",
        "privacy_audit",
        "zero_input_audit",
    ):
        path = tmp_path / f"{role}.json"
        path.write_text(json.dumps({"source_commit": "a" * 40}), encoding="utf-8")
        artifacts[role] = {"path": path, "role": role}
    report = _hardware_report(tmp_path)
    report["wheel_sha256"] = sha256_file(tmp_path / "wheel.whl")
    report["dependency_lock_sha256"] = sha256_file(tmp_path / "lock.txt")
    report["config_sha256"] = sha256_file(tmp_path / "config.json")
    report["calibration_sha256"] = sha256_file(tmp_path / "calibration.json")
    report["report_digest"] = canonical_report_digest(report)
    hardware_path = tmp_path / "hardware.json"
    hardware_path.write_text(json.dumps(report), encoding="utf-8")
    artifacts["hardware_smoke"] = {"path": hardware_path, "role": "hardware_smoke"}
    packet = build_candidate_packet(
        repo_root=tmp_path,
        source_commit="a" * 40,
        wheel_path=tmp_path / "wheel.whl",
        dependency_lock_path=tmp_path / "lock.txt",
        g0_packet_path=tmp_path / "g0-packet.json",
        g0_bundle_path=tmp_path / "g0-bundle.json",
        capture_config_path=tmp_path / "config.json",
        calibration_path=tmp_path / "calibration.json",
        pixel_contract_path=tmp_path / "pixel-contract.json",
        artifacts=artifacts,
        generated_at="2026-08-29T00:00:00Z",
    )
    return tmp_path, packet


def test_candidate_packet_schema_and_local_hashes(tmp_path: Path) -> None:
    root, packet = _candidate_tree(tmp_path)
    schema = json.loads(
        (PROJECT_ROOT / "schemas" / "g1-frame-candidate-packet.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(packet)) == []
    assert packet["packet_digest"] == canonical_packet_digest(packet)
    assert verify_g1_frame_candidate(packet, repo_root=root, metadata_only=True) == []


def test_full_candidate_rejects_synthetic_three_sample_or_unprofiled_evidence(
    tmp_path: Path,
) -> None:
    root, packet = _candidate_tree(tmp_path)
    errors = verify_g1_frame_candidate(packet, repo_root=root, metadata_only=False)
    assert any("verification_profile=b2_gate" in error for error in errors)
    assert any("at least 300 samples" in error for error in errors)
    assert any("measured backend version" in error for error in errors)


def test_candidate_rejects_local_tamper_traversal_and_symlink(tmp_path: Path) -> None:
    root, packet = _candidate_tree(tmp_path)
    target = root / "source_provenance.json"
    target.write_text(target.read_text(encoding="utf-8") + "tamper", encoding="utf-8")
    assert any(
        "hash mismatch" in error for error in verify_g1_frame_candidate(packet, repo_root=root)
    )

    root, packet = _candidate_tree(tmp_path / "nested")
    artifact = next(
        item for item in packet["artifacts"] if item["artifact_id"] == "source_provenance"
    )
    artifact["locator"] = {"kind": "local", "path": "../outside.json", "access_class": "restricted"}
    packet["packet_digest"] = canonical_packet_digest(packet)
    assert any(
        "unsafe path" in error for error in verify_g1_frame_candidate(packet, repo_root=root)
    )

    outside = root.parent / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    link = root / "source-link.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    artifact["locator"] = {
        "kind": "local",
        "path": "source-link.json",
        "access_class": "restricted",
    }
    artifact["sha256"] = sha256_file(outside)
    artifact["size_bytes"] = outside.stat().st_size
    packet["packet_digest"] = canonical_packet_digest(packet)
    assert any("symlink" in error for error in verify_g1_frame_candidate(packet, repo_root=root))


def test_candidate_zero_input_contradiction_survives_resigning(tmp_path: Path) -> None:
    root, packet = _candidate_tree(tmp_path)
    packet["input_audit"] = dict(packet["input_audit"])  # type: ignore[arg-type]
    packet["input_audit"]["real_input_call_count"] = 1  # type: ignore[index]
    packet["packet_digest"] = canonical_packet_digest(packet)
    errors = verify_g1_frame_candidate(packet, repo_root=root)
    assert any("zero-input" in error for error in errors)
