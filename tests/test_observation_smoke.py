from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tools.run_observation_smoke import (
    AssetSpec,
    ObservationSmokeError,
    ObservationSmokeFixture,
    make_fixture,
    run_smoke,
    validate_report,
    write_report,
)


def _run(fixture: ObservationSmokeFixture, root: Path, **kwargs: Any) -> dict[str, Any]:
    roots = {
        "TEST_MODEL_ROOT": root,
        "TEST_LEGACY_ROOT": root,
        "TEST_CORE_ROOT": root,
    }
    return run_smoke(
        fixture=fixture,
        asset_roots=roots,
        source_commit="a" * 40,
        evidence_id="g1-obs-002b-test-001",
        generated_at="2026-08-30T00:00:00Z",
        **kwargs,
    )


def test_three_runs_are_deterministic_and_report_is_strict(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    report = _run(fixture, tmp_path)

    assert report["status"] == "PASS"
    assert report["consistency"] == {
        "run_count": 3,
        "preprocess_equal": True,
        "tensor_equal": True,
        "result_equal": True,
        "all_equal": True,
    }
    assert len(report["runs"]) == 3
    assert len({item["preprocess_digest"] for item in report["runs"]}) == 1
    assert len({item["tensor_digest"] for item in report["runs"]}) == 1
    assert len({item["result_digest"] for item in report["runs"]}) == 1
    assert report["runtime"]["requested_provider"] == "CPUExecutionProvider"
    assert report["runtime"]["actual_provider"] == "CPUExecutionProvider"
    assert report["input_audit"] == {
        "input_owner": "legacy",
        "real_input_enabled": False,
        "real_input_call_count": 0,
        "core_v2_real_input_call_count": 0,
        "double_write_event_count": 0,
    }
    validate_report(report)


def test_schema_rejects_absolute_asset_path(tmp_path: Path) -> None:
    report = _run(make_fixture(tmp_path), tmp_path)
    forged = copy.deepcopy(report)
    forged["assets"][0]["relative_id"] = "C:/private/model.onnx"

    with pytest.raises(ObservationSmokeError, match="schema rejected"):
        validate_report(forged)


def test_schema_rejects_input_counter_drift(tmp_path: Path) -> None:
    report = _run(make_fixture(tmp_path), tmp_path)
    forged = copy.deepcopy(report)
    forged["input_audit"]["real_input_call_count"] = 1

    with pytest.raises(ObservationSmokeError, match="schema rejected"):
        validate_report(forged)


def test_recomputed_digest_does_not_hide_hash_mismatch(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    model = tmp_path / "model.onnx"
    model.write_bytes(b"tampered-model")
    report = _run(fixture, tmp_path)

    assert report["status"] == "FAIL"
    assert report["assets"][0]["status"] == "HASH_MISMATCH"
    assert report["runs"][0]["status"] == "fault"
    validate_report(report)

    forged = copy.deepcopy(report)
    forged["status"] = "PASS"
    forged["failures"] = []
    forged["assets"][0]["expected_sha256"] = "f" * 64
    forged["assets"][0]["status"] = "VERIFIED"
    unsigned = dict(forged)
    unsigned.pop("report_digest")
    forged["report_digest"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ObservationSmokeError, match="asset hash"):
        validate_report(forged)


def test_missing_asset_skips_inference_and_fails_closed(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    (tmp_path / "classes.yaml").unlink()
    report = _run(fixture, tmp_path)

    assert report["status"] == "FAIL"
    assert report["assets"][1]["status"] == "MISSING"
    assert all(item["status"] == "fault" for item in report["runs"])
    assert fixture.backend is not None
    assert fixture.backend.calls == 0
    validate_report(report)


def test_backend_input_audit_drift_is_rejected(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    assert fixture.backend is not None
    fixture.backend.input_audit = {"real_input_call_count": 1}

    with pytest.raises(ObservationSmokeError, match="input audit drift"):
        _run(fixture, tmp_path)


def test_invalid_backend_metadata_fails_closed(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    assert fixture.backend is not None
    fixture.backend.input_dtype = "tensor(float16)"

    report = _run(fixture, tmp_path)

    assert report["status"] == "FAIL"
    assert "runtime:input_dtype_invalid" in report["failures"]
    validate_report(report)


def test_asset_declaration_must_match_model_binding(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    other = tmp_path / "other.onnx"
    other.write_bytes(b"other-valid-model")
    digest = hashlib.sha256(other.read_bytes()).hexdigest()
    model = AssetSpec("model", "TEST_MODEL_ROOT", "other.onnx", digest, other.stat().st_size)
    changed = replace(fixture, assets=(model, *fixture.assets[1:]))

    report = _run(changed, tmp_path)

    assert report["status"] == "FAIL"
    assert report["assets"][0]["status"] == "INVALID"
    assert "asset:model:binding_hash_mismatch" in report["failures"]
    assert fixture.backend is not None
    assert fixture.backend.calls == 0
    validate_report(report)


def test_runtime_version_mismatch_fails_closed(tmp_path: Path) -> None:
    report = _run(make_fixture(tmp_path), tmp_path, ort_version="1.23.1")

    assert report["status"] == "FAIL"
    assert "runtime:ort_version_mismatch" in report["failures"]
    validate_report(report)

    forged = copy.deepcopy(report)
    forged["status"] = "PASS"
    forged["failures"] = []
    unsigned = dict(forged)
    unsigned.pop("report_digest")
    forged["report_digest"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ObservationSmokeError, match="runtime version is not locked"):
        validate_report(forged)


def test_recomputed_digest_does_not_hide_runtime_or_artifact_drift(tmp_path: Path) -> None:
    report = _run(make_fixture(tmp_path), tmp_path)
    forged = copy.deepcopy(report)
    forged["runtime"]["actual_provider"] = "OtherExecutionProvider"
    for run in forged["runs"]:
        run["actual_provider"] = "OtherExecutionProvider"
    unsigned = dict(forged)
    unsigned.pop("report_digest")
    forged["report_digest"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ObservationSmokeError, match="provider identity mismatch"):
        validate_report(forged)

    forged = copy.deepcopy(report)
    forged["tool_artifact_sha256"] = "f" * 64
    unsigned = dict(forged)
    unsigned.pop("report_digest")
    forged["report_digest"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ObservationSmokeError, match="tool artifact hash mismatch"):
        validate_report(forged)

    forged = copy.deepcopy(report)
    forged["limitations"].append("diagnostic leaked /tmp")
    unsigned = dict(forged)
    unsigned.pop("report_digest")
    forged["report_digest"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ObservationSmokeError, match="absolute path"):
        validate_report(forged)


def test_report_write_keeps_current_and_existing_evidence_protected(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    report = _run(fixture, tmp_path)
    with pytest.raises(ObservationSmokeError, match="protected"):
        write_report(report, tmp_path / "current.json")
    output = tmp_path / "report.json"
    write_report(report, output)
    with pytest.raises(ObservationSmokeError, match="already exists"):
        write_report(report, output)


def test_report_write_rejects_linked_parent(tmp_path: Path) -> None:
    report = _run(make_fixture(tmp_path / "fixture"), tmp_path / "fixture")
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    alias = tmp_path / "alias"
    try:
        os.symlink(sealed, alias, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ObservationSmokeError, match="protected"):
        write_report(report, alias / "report.json")
    assert not (sealed / "report.json").exists()
