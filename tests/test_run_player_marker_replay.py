from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from maple_automation_core.capture.pixel_store import canonical_json

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_player_marker_replay", ROOT / "tools" / "run_player_marker_replay.py"
)
assert SPEC is not None and SPEC.loader is not None
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def _args_without_commit() -> list[str]:
    return [
        "--manifest",
        "manifest.json",
        "--truth-root",
        "truth",
        "--private-cas-root",
        "cas",
        "--event-tape",
        "events.jsonl",
        "--accepted-ledger",
        "ledger.jsonl",
        "--calibration",
        "calibration.json",
        "--zero-input-audit",
        "audit.json",
        "--marker-config",
        "marker.json",
    ]


def test_replay_commit_is_required_and_zero_timing_is_fixed() -> None:
    with pytest.raises(SystemExit):
        cli._parse_args(_args_without_commit())

    args = cli._parse_args(
        [*_args_without_commit(), "--replay-source-commit", "a" * 40]
    )
    assert args.as_of_offset_ns == 0
    assert args.generation == 0

    with pytest.raises(SystemExit):
        cli._parse_args(
            [
                *_args_without_commit(),
                "--replay-source-commit",
                "a" * 40,
                "--generation",
                "1",
            ]
        )


def test_event_tapes_are_sorted_and_must_be_unique(tmp_path) -> None:
    second = tmp_path / "z.jsonl"
    first = tmp_path / "a.jsonl"
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")

    assert cli._event_paths([second, first], None) == (first.resolve(), second.resolve())
    with pytest.raises(ValueError, match="unique"):
        cli._event_paths([first, first], None)


def test_report_writer_is_canonical_single_lf_and_atomic(tmp_path) -> None:
    output = tmp_path / "nested" / "report.json"
    payload = {"z": [2, 1], "a": "value"}

    cli._write_atomic_json(output, payload)

    assert output.read_bytes() == canonical_json(payload) + b"\n"
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


def test_read_only_pixel_store_has_no_mutation_surface(tmp_path) -> None:
    store = cli.ReadOnlyPixelStore(tmp_path / "cas")

    assert callable(store.read)
    assert not hasattr(store, "write")
    assert not hasattr(store, "put")
