from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tools import run_clean_smoke as clean_smoke_module
from tools.bundle_common import sha256_file
from tools.run_clean_smoke import (
    _absolute_path_findings,
    _artifact,
    _lineage_is_allowed,
    _parse_args,
    _sanitize_text,
    _sanitize_text_file,
)


def test_checkout_smoke_exercises_frame_admission_from_installed_wheel() -> None:
    source = inspect.getsource(clean_smoke_module.run_clean_smoke)
    assert "installed-frame-admission-three-runs" in source
    assert "run_frame_admission_replay.py" in source
    assert '"--require-installed"' in source
    assert "g1-frame-requirements.lock" in source
    assert "g1-installed-runtime-smoke" in source
    assert "evidence-path-privacy" in source


def test_g1_runtime_steps_are_scoped_to_checkout_regression() -> None:
    source = inspect.getsource(clean_smoke_module.run_clean_smoke)
    mode_guard = 'if mode == "checkout-regression":'
    lock_step = source.index('"g1-runtime-lock-install"')
    runtime_step = source.index('name="g1-installed-runtime-smoke"')
    assert source.rfind(mode_guard, 0, lock_step) >= 0
    assert source.rfind(mode_guard, 0, runtime_step) >= 0


def test_clean_smoke_mode_defaults_to_sealed_g0() -> None:
    assert _parse_args([]).mode == "g0-seal"
    assert _parse_args(["--mode", "checkout-regression"]).mode == "checkout-regression"


def test_g0_seal_keeps_packaging_only_lineage() -> None:
    assert _lineage_is_allowed("g0-seal", ancestor_ok=True, unexpected_paths=[])
    assert not _lineage_is_allowed(
        "g0-seal",
        ancestor_ok=True,
        unexpected_paths=["src/maple_automation_core/capture/frame_source.py"],
    )
    assert not _lineage_is_allowed("g0-seal", ancestor_ok=False, unexpected_paths=[])


def test_checkout_regression_accepts_code_descendant_without_rebinding_seal() -> None:
    assert _lineage_is_allowed(
        "checkout-regression",
        ancestor_ok=True,
        unexpected_paths=["src/maple_automation_core/capture/frame_source.py"],
    )
    assert not _lineage_is_allowed(
        "checkout-regression",
        ancestor_ok=False,
        unexpected_paths=[],
    )
    with pytest.raises(ValueError, match="Unsupported"):
        _lineage_is_allowed("unknown", ancestor_ok=True, unexpected_paths=[])


def test_clean_smoke_sanitizes_runner_paths_and_scans_text(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    temp_root = tmp_path / "runner-temp"
    report = repo_root / "evidence" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        '{"repo": "' + str(repo_root) + '\\tools\\run.py", "temp": "' + str(temp_root) + '"}',
        encoding="utf-8",
    )

    sanitized = _sanitize_text(
        f"{repo_root}\\tools\\run.py {temp_root}\\venv\\python.exe C:\\Users\\Runner\\secret",
        repo_root=repo_root,
        temp_root=temp_root,
    )

    assert str(repo_root) not in sanitized
    assert str(temp_root) not in sanitized
    assert "[absolute-path]" in sanitized
    assert _absolute_path_findings([report]) == {report.name: 2}


def test_clean_smoke_hashes_sanitized_xml_bytes(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    junit = repo_root / "evidence" / "clean-junit.xml"
    junit.parent.mkdir(parents=True)
    junit.write_text(
        f'<testsuite><testcase file="{repo_root / "tests" / "test_ok.py"}" /></testsuite>',
        encoding="utf-8",
    )

    _sanitize_text_file(junit, repo_root=repo_root)
    artifact = _artifact(repo_root, junit, "junit", "clean-junit")

    assert artifact["sha256"] == sha256_file(junit)
    source = inspect.getsource(clean_smoke_module.run_clean_smoke)
    assert source.index("sanitize_xml_evidence(temp_root)") < source.index("report_artifacts = [")
