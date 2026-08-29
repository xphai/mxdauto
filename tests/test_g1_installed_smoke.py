from __future__ import annotations

import json
import tempfile
import tomllib
from pathlib import Path

import pytest

from maple_automation_core.replay.frame_corpus import public_privacy_summary
from tools.run_g1_installed_smoke import (
    EXPECTED_ARTIFACT_HASHES,
    EXPECTED_G1_LOCK,
    EXPECTED_NUMPY,
    EXPECTED_OPENCV_DISTRIBUTION,
    ROOT,
    SmokeFailure,
    _load_runtime,
    _parse_g1_lock,
    _vc_lifecycle_smoke,
    run_smoke,
)


def test_runtime_dependencies_are_exact_and_python312_compatible() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["requires-python"] == ">=3.12"
    assert sorted(pyproject["project"]["dependencies"]) == sorted(
        [f"numpy=={EXPECTED_NUMPY}", f"opencv-python-headless=={EXPECTED_OPENCV_DISTRIBUTION}"]
    )

    g1_lock = _parse_g1_lock(ROOT / "configs" / EXPECTED_G1_LOCK)
    assert g1_lock["numpy"] == {
        "version": EXPECTED_NUMPY,
        "hashes": [EXPECTED_ARTIFACT_HASHES["numpy"]],
    }
    assert g1_lock["opencv-python-headless"] == {
        "version": EXPECTED_OPENCV_DISTRIBUTION,
        "hashes": [EXPECTED_ARTIFACT_HASHES["opencv-python-headless"]],
    }


def test_fake_vc003_smoke_has_single_lifecycle_and_zero_input() -> None:
    vc_module = __import__(
        "maple_automation_core.capture.vc003_source",
        fromlist=["VC003Source"],
    )
    result = _vc_lifecycle_smoke(vc_module)
    assert result["lifecycle"] == "stopped"
    assert result["accounting_holds"] is True
    assert result["max_depth"] == 1
    assert result["fake_backend"]["start_calls"] == 1
    assert result["fake_backend"]["stop_calls"] == 1
    assert result["real_device_opened"] is False


def test_require_installed_rejects_checkout_runtime() -> None:
    with pytest.raises(SmokeFailure, match="resolved from checkout"):
        _load_runtime(True)


def test_default_smoke_runs_only_offline_fixture_paths(tmp_path: Path) -> None:
    report = run_smoke(repo_root=ROOT, require_installed=False)
    assert report["status"] == "PASS", json.dumps(report, sort_keys=True)
    assert report["input_audit"]["real_input_call_count"] == 0
    assert report["input_audit"]["receiver_connect_count"] == 0
    assert report["input_audit"]["window_write_count"] == 0
    assert report["corpus_audit"]["audit_status"] == "PASS"
    assert report["corpus_audit"]["sample_count"] == 3
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert str(ROOT).casefold() not in serialized.casefold()
    assert str(Path(tempfile.gettempdir())).casefold() not in serialized.casefold()
    privacy = public_privacy_summary(report, [])
    assert privacy["pii_findings"] == 0
    assert report["privacy_scan"]["status"] == "PASS"
    assert not list(tmp_path.iterdir())


def test_runtime_only_profile_skips_dev_corpus_subprocess() -> None:
    report = run_smoke(repo_root=ROOT, require_installed=False, runtime_only=True)
    assert report["status"] == "PASS", json.dumps(report, sort_keys=True)
    assert report["scope"] == "runtime-only"
    assert report["runtime_only"] is True
    assert "corpus_audit" not in report
    corpus_check = next(
        check for check in report["checks"] if check["name"] == "corpus-audit-installed-import"
    )
    assert corpus_check["status"] == "SKIPPED"
    assert report["privacy_scan"]["pii_findings"] == 0
