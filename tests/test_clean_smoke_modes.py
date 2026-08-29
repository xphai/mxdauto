from __future__ import annotations

import inspect

import pytest

from tools import run_clean_smoke as clean_smoke_module
from tools.run_clean_smoke import _lineage_is_allowed, _parse_args


def test_checkout_smoke_exercises_frame_admission_from_installed_wheel() -> None:
    source = inspect.getsource(clean_smoke_module.run_clean_smoke)
    assert "installed-frame-admission-three-runs" in source
    assert "run_frame_admission_replay.py" in source
    assert '"--require-installed"' in source


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
