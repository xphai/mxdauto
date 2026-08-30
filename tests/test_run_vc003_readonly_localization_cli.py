"""Focused contract tests for the VC-003 runner and independent verifier."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from maple_automation_core.capture.pixel_store import PixelStore
from maple_automation_core.capture.vc003_source import VC003SourceConfig

ROOT = Path(__file__).resolve().parents[1]


def _load_tool(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> Any:
    return _load_tool("run_vc003_readonly_localization")


@pytest.fixture(scope="module")
def verifier() -> Any:
    return _load_tool("verify_vc003_readonly_localization")


def test_runner_parser_keeps_production_window_and_requires_private_outputs(runner: Any) -> None:
    args = runner._parse_args(
        [
            "--report",
            "report.json",
            "--private-cas-root",
            "cas",
            "--private-rows",
            "rows.jsonl",
            "--source-commit",
            "a" * 40,
        ]
    )
    assert args.report == Path("report.json")
    assert args.private_cas_root == Path("cas")
    assert args.private_rows == Path("rows.jsonl")
    assert not hasattr(args, "warmup_seconds")
    assert not hasattr(args, "measurement_seconds")


def test_runner_short_window_is_only_available_to_injected_test_path(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeIntegration:
        def __init__(self, *_args: Any, thresholds: Any, **_kwargs: Any) -> None:
            self.thresholds = thresholds
            self.selector = SimpleNamespace(selected=())
            self.rows: tuple[Any, ...] = ()
            self.results: tuple[Any, ...] = ()

        def validate(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(valid=True)

    monkeypatch.setattr(runner, "VC003LiveMarkerRunner", FakeIntegration)
    config = runner.load_strict_json(runner.CONFIG_PATH)
    report = runner.run_measurement(
        source=SimpleNamespace(),
        source_config=VC003SourceConfig(session_id="test-session"),
        clock=lambda: 123,
        warmup_seconds=0,
        measurement_seconds=0,
        source_commit="a" * 40,
        config=config,
        config_sha256=runner.sha256_file(runner.CONFIG_PATH),
    )

    assert report["timing"]["warmup_seconds"] == 0
    assert report["timing"]["measurement_seconds"] == 0
    assert report["status"] == "FAIL"


def test_runner_writer_is_canonical_single_lf_and_atomic(tmp_path: Path, runner: Any) -> None:
    output = tmp_path / "nested" / "report.json"
    payload = {"z": [2, 1], "a": "value"}

    runner._write_atomic_json(output, payload)

    assert output.read_bytes() == runner.canonical_json(payload) + b"\n"
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


def test_read_only_pixel_store_has_no_write_surface(tmp_path: Path, runner: Any) -> None:
    store = runner.ReadOnlyPixelStore(PixelStore(tmp_path))
    assert callable(store.read)
    assert not hasattr(store, "put")
    assert not hasattr(store, "write")


def test_verifier_recomputes_public_row_digest_and_privacy(verifier: Any) -> None:
    row = {
        "schema_version": "1.0.0",
        "row_kind": "public_hash_only",
        "bucket_index": 0,
        "status": "candidate",
        "generation": 0,
        "frame_digest": "a" * 64,
        "pixel_digest": "b" * 64,
        "candidate_digest": "c" * 64,
        "evidence_digest": "d" * 64,
        "result_digest": "e" * 64,
        "sample_ordinal": 0,
        "bucket_offset_ns": 0,
        "selected": True,
    }
    row["row_digest"] = verifier._canonical_digest(row)
    errors, rows = verifier._public_row_errors([row])
    assert "public_rows_count" in errors
    assert rows[0]["row_digest"] == row["row_digest"]

    row["coordinates"] = {"x": 1, "y": 2}
    errors, _ = verifier._public_row_errors([row])
    assert any(item.startswith("privacy_private_key:") for item in errors)


def test_verifier_does_not_trust_report_status(verifier: Any) -> None:
    report = {"status": "PASS", "execution_valid": True}
    errors = verifier.verify_report(report, require_external_bindings=False)
    assert errors
    assert "private_rows_missing" in errors
