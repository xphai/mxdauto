from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "configs" / "g1-loc-003c-vc003-readonly-live.json"
REPORT_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "vc003-readonly-localization-report.schema.json"
LEDGER_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "vc003-readonly-localization-ledger-row.schema.json"

B2_PACKET = "4e21973f66fd5c4480c1417d1509a0e21069551d728bf02607319008cbf74f73"
LOC003B_RAW = "37076a1937fa10ce317c4899a43470dfcce9dd7c155f6a0efa8ef089f0efc4d5"
LOC003B_SEMANTIC = "9528f117200bfcb24d3723a081e83e4889f273322c798fef6fd62cfc14a361ff"
MARKER_RAW = "2d77fae38f22386a2ab1465a1c837d2b935f26c020c3a10ffd17f086ae8306b5"
MARKER_SEMANTIC = "47936cf77e46ebc62fd3d6dae241237307ebb370fd81a197745486812c58f22a"
CALIBRATION = "bde680518546eaef708f190a7087b5d7b6623a1b744826d5e9565d63d2c5d549"
EXTRACTOR = "508b309fce0988a2b0c1e7f4b2ab13a4702a969be5f0175950cb9f779c18a651"


def _digest(label: str, index: int = 0) -> str:
    return hashlib.sha256(f"{label}:{index}".encode("ascii")).hexdigest()


def _binding(kind: str, digest: str) -> dict[str, str]:
    return {
        "kind": kind,
        "expected_sha256": digest,
        "external_ref": f"external://vc003/{kind}",
    }


def _public_row(index: int) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "row_kind": "public_hash_only",
        "bucket_index": index,
        "status": "candidate",
        "generation": 0,
        "frame_digest": _digest("frame", index),
        "pixel_digest": _digest("pixel", index),
        "candidate_digest": _digest("candidate", index),
        "evidence_digest": _digest("evidence", index),
        "result_digest": _digest("result", index),
        "row_digest": _digest("row", index),
        "sample_ordinal": index,
        "bucket_offset_ns": index,
        "selected": True,
    }


def _format() -> dict[str, Any]:
    return {
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "fourcc": "MJPG",
        "backend": "dshow",
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": 5760,
        "length": 6220800,
    }


def _full_frame_geometry() -> dict[str, Any]:
    return {
        "source_size": {"width": 1920, "height": 1080},
        "content_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
        "working_size": {"width": 1920, "height": 1080},
    }


def _valid_report() -> dict[str, Any]:
    zero_counts = {
        "capture_error_count": 0,
        "admission_error_count": 0,
        "selector_error_count": 0,
        "marker_error_count": 0,
        "timing_error_count": 0,
        "lineage_error_count": 0,
        "privacy_error_count": 0,
        "cleanup_error_count": 0,
        "total_count": 0,
        "codes": [],
    }
    status_counts = {
        "accepted": 100,
        "no_frame": 0,
        "stale": 0,
        "duplicate": 0,
        "out_of_order": 0,
        "timestamp_regression": 0,
        "frame_size_changed": 0,
        "source_mismatch": 0,
        "session_mismatch": 0,
        "clock_domain_mismatch": 0,
        "source_error": 0,
    }
    return {
        "schema_version": "1.0.0",
        "report_type": "vc003_readonly_localization",
        "scope": "G1-LOC-003C",
        "truth_scope": "live_marker_integration_only",
        "scope_excluded": [
            "ObservationResult",
            "affine",
            "world",
            "map",
            "platform",
            "planner",
            "action",
            "input",
            "receiver",
            "window",
            "resolver",
            "accuracy",
        ],
        "report_id": "vc003-live-marker-20260830",
        "generated_at": "2026-08-30T10:00:00Z",
        "source_commit": "a" * 40,
        "config_sha256": _digest("config"),
        "status": "PASS",
        "execution_valid": True,
        "expected_bindings": {
            "upstream_b2_packet": _binding("g1-frame-candidate-packet", B2_PACKET),
            "loc003b_report_raw": _binding("loc003b-report-raw", LOC003B_RAW),
            "loc003b_report_semantic": _binding("loc003b-report-semantic", LOC003B_SEMANTIC),
            "base_marker_config_raw": _binding("marker-config-raw", MARKER_RAW),
            "base_marker_config_semantic": _binding("marker-config-semantic", MARKER_SEMANTIC),
            "calibration": _binding("calibration", CALIBRATION),
            "extractor": _binding("extractor", EXTRACTOR),
        },
        "capture": {
            "read_only": True,
            "source_id": "capture-card-primary",
            "selector": "VC-003 Video",
            "requested": _format(),
            "negotiated": _format(),
            "geometry": _full_frame_geometry(),
            "timestamp_origin": "host_monotonic_post_retrieve",
            "clock_domain": "monotonic",
            "accepted_cas_required": True,
            "device_fingerprint_sha256": "d" * 64,
        },
        "admission": {
            "accepted_count": 100,
            "rejected_count": 0,
            "status_counts": status_counts,
            "accepted_frame_ledger_sha256": _digest("accepted-ledger"),
            "cas_lineage_verified": True,
            "accepted_cas_count": 100,
            "accepted_packet_count": 100,
            "max_accepted_age_ns": 250000000,
            "max_inter_frame_gap_ns": 100000000,
        },
        "selector": {
            "policy": "one_representative_per_3s_bucket",
            "bucket_count": 100,
            "selected_count": 100,
            "selected_rows_digest": _digest("selected-rows"),
            "allow_duplicate_pixel_digest": True,
            "candidate_count": 100,
            "no_candidate_count": 0,
            "rejected_count": 0,
            "fault_count": 0,
        },
        "marker": {
            "extractor": "MinimapMarkerExtractor",
            "extractor_artifact_sha256": EXTRACTOR,
            "config_semantic_sha256": MARKER_SEMANTIC,
            "base_config_raw_sha256": MARKER_RAW,
            "calibration_sha256": CALIBRATION,
            "geometry": _full_frame_geometry(),
            "candidate_stage": "working_space",
            "resolver_invoked": False,
            "accuracy_evaluated": False,
            "candidate_count": 100,
            "no_candidate_count": 0,
            "fault_count": 0,
            "candidate_digest": _digest("candidate-set"),
            "result_digest": _digest("marker-results"),
        },
        "timing": {
            "warmup_seconds": 30,
            "measurement_seconds": 300,
            "bucket_count": 100,
            "bucket_seconds": 3,
            "bucket_clock": "FramePacket.received_at_ns",
            "bucket_boundary": "half_open",
            "generation": 0,
            "timestamp_origin": "host_monotonic_post_retrieve",
            "clock_domain": "monotonic",
            "monotonic": True,
            "bucket_coverage_digest": _digest("bucket-coverage"),
            "measurement_start_offset_ns": 0,
            "measurement_end_offset_ns": 300000000000,
            "max_inter_frame_gap_ns": 100000000,
        },
        "lineage": {
            "chain": (
                "VC003Source->accepted FramePacket/CAS->MinimapMarkerExtractor->"
                "working-space candidate"
            ),
            "upstream_b2_packet_sha256": B2_PACKET,
            "accepted_frame_ledger_sha256": _digest("accepted-ledger"),
            "pixel_digest_domain": "MAPLE_PIXEL_V1",
            "cas_required": True,
            "candidate_output": "working_space_candidate",
            "resolver_invoked": False,
            "accuracy_evaluated": False,
            "world_state_emitted": False,
            "private_rows_external_ref": "external://vc003/restricted-verifier-rows.jsonl",
        },
        "failure": zero_counts,
        "zero_input": {
            "audit_id": "vc003-zero-input-run-1",
            "run_index": 1,
            "scope": "G1-LOC-003C",
            "reused_from_b2": False,
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
        "privacy": {
            "audit_id": "vc003-privacy-run-1",
            "run_index": 1,
            "scope": "G1-LOC-003C",
            "reused_from_b2": False,
            "raw_artifacts_public": False,
            "coordinates_present": False,
            "absolute_paths_present": False,
            "raw_bytes_present": False,
            "device_original_id_present": False,
            "private_artifacts_external_only": True,
            "finding_count": 0,
        },
        "cleanup": {
            "status": "PASS",
            "capture_stopped": True,
            "residual_thread_count": 0,
            "residual_child_count": 0,
            "private_artifacts_released": True,
            "cleanup_failure_count": 0,
        },
        "runs": [
            {
                "run_index": 1,
                "selected_row_count": 100,
                "zero_input": {
                    "audit_id": "vc003-zero-input-run-1",
                    "run_index": 1,
                    "scope": "G1-LOC-003C",
                    "reused_from_b2": False,
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
                "privacy": {
                    "audit_id": "vc003-privacy-run-1",
                    "run_index": 1,
                    "scope": "G1-LOC-003C",
                    "reused_from_b2": False,
                    "raw_artifacts_public": False,
                    "coordinates_present": False,
                    "absolute_paths_present": False,
                    "raw_bytes_present": False,
                    "device_original_id_present": False,
                    "private_artifacts_external_only": True,
                    "finding_count": 0,
                },
                "status": "PASS",
                "execution_valid": True,
                "selected_rows_digest": _digest("selected-rows"),
            }
        ],
        "selected_row_count": 100,
        "public_selected_rows": [_public_row(index) for index in range(100)],
        "limitations": [
            (
                "truth_scope is live_marker_integration_only; no resolver or marker "
                "accuracy claim is made."
            ),
            "The read-only chain ends at a working-space candidate.",
            "Raw pixels and restricted verifier rows remain external.",
        ],
        "report_digest": _digest("report"),
    }


@pytest.fixture(scope="module")
def report_validator() -> Draft202012Validator:
    schema = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.fixture(scope="module")
def ledger_validator() -> Draft202012Validator:
    schema = json.loads(LEDGER_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_config_freezes_full_frame_live_window_and_expected_bindings() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["truth_scope"] == "live_marker_integration_only"
    assert config["generation"] == 0
    assert "ObservationResult" in config["scope_excluded"]
    assert "resolver" in config["scope_excluded"]
    assert config["measurement_window"] == {
        "warmup_seconds": 30,
        "measurement_seconds": 300,
        "bucket_count": 100,
        "bucket_seconds": 3,
        "target_admission_hz": 15.0,
        "poll_timeout_seconds": 0.05,
        "bucket_clock": "FramePacket.received_at_ns",
        "bucket_boundary": "half_open",
        "generation": 0,
    }
    capture = config["capture"]
    assert capture["backend"] == "dshow"
    assert capture["fourcc"] == "MJPG"
    assert capture["fps"] == 30.0
    assert capture["source_size"] == {"width": 1920, "height": 1080}
    assert capture["content_rect"] == {"x": 0, "y": 0, "width": 1920, "height": 1080}
    assert capture["working_size"] == {"width": 1920, "height": 1080}
    assert capture["pixel_format"] == "BGR8"
    assert config["marker"]["roi"] == {"x": 309, "y": 238, "width": 97, "height": 113}
    assert config["marker"]["calibration_sha256"] == CALIBRATION
    assert config["marker"]["extractor_artifact_sha256"] == EXTRACTOR
    assert config["marker"]["bucket_clock"] == "FramePacket.received_at_ns"
    assert config["marker"]["bucket_boundary"] == "half_open"
    assert {
        key: value["expected_sha256"] for key, value in config["expected_bindings"].items()
    } == {
        "upstream_b2_packet": B2_PACKET,
        "loc003b_report_raw": LOC003B_RAW,
        "loc003b_report_semantic": LOC003B_SEMANTIC,
        "base_marker_config_raw": MARKER_RAW,
        "base_marker_config_semantic": MARKER_SEMANTIC,
        "calibration": CALIBRATION,
        "extractor": EXTRACTOR,
    }


def test_draft202012_accepts_report_and_both_ledger_row_variants(
    report_validator: Draft202012Validator,
    ledger_validator: Draft202012Validator,
) -> None:
    report = _valid_report()
    assert list(report_validator.iter_errors(report)) == []
    rows = report["public_selected_rows"]
    assert isinstance(rows, list)
    assert len(rows) == 100
    assert all(not list(ledger_validator.iter_errors(row)) for row in rows)

    restricted = {
        "schema_version": "1.0.0",
        "row_kind": "restricted_verifier",
        "bucket_index": 0,
        "sample_id": "sample-0",
        "status": "candidate",
        "generation": 0,
        "session_id": "session-0",
        "source_id": "capture-card-primary",
        "frame_id": 0,
        "source_sequence": 0,
        "source_provenance_id": "vc003-live",
        "frame_digest": _digest("frame", 0),
        "pixel_digest": _digest("pixel", 0),
        "occurrence_artifact_sha256": _digest("occurrence", 0),
        "candidate_digest": _digest("candidate", 0),
        "evidence_digest": _digest("evidence", 0),
        "result_digest": _digest("result", 0),
        "artifact_ref": "external://vc003/restricted-verifier-rows.jsonl",
        "privacy_class": "restricted",
        "retention_class": "candidate",
        "working_candidate": {"x": 123.5, "y": 456.25},
        "source_bbox": {"x": 309, "y": 238, "width": 10, "height": 10},
        "source_centroid": [314.0, 243.0],
        "geometry": _full_frame_geometry(),
        "calibration_sha256": CALIBRATION,
    }
    assert list(ledger_validator.iter_errors(restricted)) == []


def test_report_rejects_missing_required_field(report_validator: Draft202012Validator) -> None:
    report = _valid_report()
    del report["lineage"]
    errors = list(report_validator.iter_errors(report))
    assert any(error.validator == "required" and "lineage" in error.message for error in errors)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("x", 1),
        ("coordinates", {"x": 1, "y": 2}),
        ("absolute_path", "C:/private/frame.bin"),
        ("raw_bytes", "AAAA"),
        ("device_original_id", "USB\\VID_0000"),
    ],
)
def test_public_rows_reject_coordinate_path_raw_and_device_leaks(
    report_validator: Draft202012Validator,
    field: str,
    value: Any,
) -> None:
    report = _valid_report()
    report["public_selected_rows"][0][field] = value
    errors = list(report_validator.iter_errors(report))
    assert errors, f"Expected schema rejection for leaked field {field!r}."


def test_count_errors_are_rejected(report_validator: Draft202012Validator) -> None:
    report = _valid_report()
    report["selected_row_count"] = 99
    assert list(report_validator.iter_errors(report))

    report = _valid_report()
    report["public_selected_rows"] = report["public_selected_rows"][:-1]
    assert list(report_validator.iter_errors(report))


def test_geometry_is_full_frame_and_bucket_is_half_open(
    report_validator: Draft202012Validator,
    ledger_validator: Draft202012Validator,
) -> None:
    report = _valid_report()
    report["capture"]["geometry"]["content_rect"]["width"] = 1366
    assert list(report_validator.iter_errors(report))

    report = _valid_report()
    report["marker"]["geometry"]["working_size"]["height"] = 768
    assert list(report_validator.iter_errors(report))

    row = _public_row(0)
    row["bucket_offset_ns"] = 3_000_000_000
    assert list(ledger_validator.iter_errors(row))


def test_status_and_execution_valid_are_untrusted_verifier_annotations(
    report_validator: Draft202012Validator,
) -> None:
    report = _valid_report()
    report["execution_valid"] = True
    report["status"] = "FAIL"
    assert list(report_validator.iter_errors(report)) == []

    report = _valid_report()
    report["execution_valid"] = False
    report["status"] = "PASS"
    assert list(report_validator.iter_errors(report)) == []


def test_duplicate_pixel_digest_is_valid_for_distinct_selected_occurrences(
    report_validator: Draft202012Validator,
) -> None:
    report = _valid_report()
    rows = report["public_selected_rows"]
    rows[1]["pixel_digest"] = rows[0]["pixel_digest"]
    assert list(report_validator.iter_errors(report)) == []


def test_run_audits_are_local_to_loc003c_and_cannot_reuse_b2_audit(
    report_validator: Draft202012Validator,
) -> None:
    report = _valid_report()
    report["runs"][0]["zero_input"]["reused_from_b2"] = True
    assert list(report_validator.iter_errors(report))

    report = _valid_report()
    report["runs"][0]["privacy"]["scope"] = "G1-FRM-001B2"
    assert list(report_validator.iter_errors(report))


def test_private_artifact_must_use_external_reference(
    report_validator: Draft202012Validator,
) -> None:
    report = _valid_report()
    report["artifacts"] = [
        {
            "artifact_id": "private-pixels",
            "role": "raw_pixels",
            "external_ref": "C:/private/pixels",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "privacy_class": "private",
            "retention_class": "ephemeral",
        }
    ]
    assert list(report_validator.iter_errors(report))

    report["artifacts"][0]["external_ref"] = "external://vc003/private-pixels"
    assert list(report_validator.iter_errors(report)) == []


def test_restricted_row_cannot_be_smuggled_as_public_hash_only(
    ledger_validator: Draft202012Validator,
) -> None:
    row = _public_row(0)
    row["row_kind"] = "restricted_verifier"
    row["working_candidate"] = {"x": 1, "y": 2}
    assert list(ledger_validator.iter_errors(row))


def test_report_fixture_is_not_mutated_by_validation(
    report_validator: Draft202012Validator,
) -> None:
    report = _valid_report()
    before = copy.deepcopy(report)
    list(report_validator.iter_errors(report))
    assert report == before
