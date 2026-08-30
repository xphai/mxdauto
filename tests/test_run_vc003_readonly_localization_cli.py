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
            "--accepted-ledger",
            "accepted.jsonl",
            "--private-cas-root",
            "cas",
            "--private-rows",
            "rows.jsonl",
            "--source-commit",
            "a" * 40,
        ]
    )
    assert args.report == Path("report.json")
    assert args.accepted_ledger == Path("accepted.jsonl")
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


def test_runner_real_clock_branch_passes_the_frozen_read_timeout(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_timeouts: list[float] = []

    class FakeIntegration:
        def __init__(self, *_args: Any, thresholds: Any, **_kwargs: Any) -> None:
            self.thresholds = thresholds
            self.selector = SimpleNamespace(selected=())
            self.rows: tuple[Any, ...] = ()
            self.results: tuple[Any, ...] = ()

        def poll(self, *, timeout_s: float) -> tuple[Any, None]:
            seen_timeouts.append(timeout_s)
            admission = SimpleNamespace(
                accepted=False,
                packet=None,
                status=runner.FrameAdmissionStatus.NO_FRAME,
                event=SimpleNamespace(observed_at_ns=0),
            )
            return admission, None

        def validate(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(valid=True)

    monotonic_ns_values = iter((0, 0, 0, 1_000_000_000))
    source = SimpleNamespace(
        is_running=False,
        start=lambda: None,
        stop=lambda: None,
        negotiated_facts=None,
    )
    monkeypatch.setattr(runner, "VC003LiveMarkerRunner", FakeIntegration)
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: next(monotonic_ns_values))
    monkeypatch.setattr(runner.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    runner.run_measurement(
        source=source,
        source_config=VC003SourceConfig(session_id="test-session"),
        warmup_seconds=0,
        measurement_seconds=1,
        source_commit="a" * 40,
        config=runner.load_strict_json(runner.CONFIG_PATH),
        config_sha256=runner.sha256_file(runner.CONFIG_PATH),
    )

    assert seen_timeouts == [runner.POLL_TIMEOUT_SECONDS]


def test_runner_cleans_up_after_source_start_failure(runner: Any) -> None:
    stopped: list[bool] = []

    def fail_start() -> None:
        raise RuntimeError("synthetic start failure")

    source = SimpleNamespace(
        is_running=False,
        start=fail_start,
        stop=lambda: stopped.append(True),
    )

    with pytest.raises(RuntimeError, match="synthetic start failure"):
        runner.run_measurement(
            source=source,
            source_config=VC003SourceConfig(session_id="test-session"),
            warmup_seconds=0,
            measurement_seconds=0,
            source_commit="a" * 40,
            config=runner.load_strict_json(runner.CONFIG_PATH),
            config_sha256=runner.sha256_file(runner.CONFIG_PATH),
        )

    assert stopped == [True]


def test_runner_cleans_up_if_clock_fails_after_source_start(runner: Any) -> None:
    stopped: list[bool] = []
    source = SimpleNamespace(
        is_running=False,
        start=lambda: None,
        stop=lambda: stopped.append(True),
    )

    with pytest.raises(RuntimeError, match="synthetic clock failure"):
        runner.run_measurement(
            source=source,
            source_config=VC003SourceConfig(session_id="test-session"),
            clock=lambda: (_ for _ in ()).throw(RuntimeError("synthetic clock failure")),
            warmup_seconds=0,
            measurement_seconds=0,
            source_commit="a" * 40,
            config=runner.load_strict_json(runner.CONFIG_PATH),
            config_sha256=runner.sha256_file(runner.CONFIG_PATH),
        )

    assert stopped == [True]


def test_runner_writer_is_canonical_single_lf_and_atomic(tmp_path: Path, runner: Any) -> None:
    output = tmp_path / "nested" / "report.json"
    payload = {"z": [2, 1], "a": "value"}

    runner._write_atomic_json(output, payload)

    assert output.read_bytes() == runner.canonical_json(payload) + b"\n"
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


def test_frozen_config_and_default_bindings_pass_preflight(runner: Any) -> None:
    config = runner.load_strict_json(runner.CONFIG_PATH)
    runner._production_config(config)
    extractor = Path(runner.inspect.getsourcefile(runner.MinimapMarkerExtractor))
    paths = {
        "upstream_b2_packet": runner.DEFAULT_B2_PACKET_PATH,
        "loc003b_report_raw": runner.DEFAULT_LOC003B_REPORT_PATH,
        "base_marker_config_raw": runner.DEFAULT_MARKER_CONFIG_PATH,
        "calibration": runner.DEFAULT_B2_PROVENANCE_PATH,
        "extractor": extractor,
        "wheel": runner.DEFAULT_WHEEL_PATH,
        "dependency_lock": runner.DEFAULT_LOCK_PATH,
        "device_environment": runner.DEFAULT_B2_PROVENANCE_PATH,
    }
    expected = {
        "wheel": runner.EXPECTED_B2_WHEEL_SHA256,
        "dependency_lock": runner.EXPECTED_B2_LOCK_SHA256,
        "device_environment": runner.EXPECTED_B2_DEVICE_ENV_SHA256,
    }

    assert runner.verify_external_bindings(config, paths, expected=expected) == []


def test_device_preflight_binds_the_named_physical_instance(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_id = r"USB\VID_345F&PID_2131&MI_00\6&1D3088&0&0000"
    alternative = (
        r"@device_pnp_\\?\usb#vid_345f&pid_2131&mi_00#6&1d3088&0&0000"
        r"#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
    )
    monkeypatch.setattr(
        runner,
        "_enumerate_dshow_video_devices",
        lambda: [("Other Camera", "other"), ("VC-003 Video", alternative)],
    )

    index, digest = runner._preflight_device("VC-003 Video", instance_id)

    assert index == 1
    assert digest == runner.hash_physical_device_fingerprint(instance_id)


def test_cleanup_rejects_a_residual_capture_worker(runner: Any) -> None:
    source = SimpleNamespace(
        status=lambda: SimpleNamespace(
            residual_worker_count=1,
            lifecycle="stopped",
            error=None,
            accounting_holds=True,
        )
    )

    cleanup = runner._cleanup_payload(source, stop_ok=True, private_released=True)

    assert cleanup["status"] == "FAIL"
    assert cleanup["residual_thread_count"] == 1


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


def test_verifier_malformed_nested_shape_fails_closed(verifier: Any) -> None:
    errors = verifier.verify_report(
        {"scope": "G1-LOC-003C", "lineage": "malformed"},
        require_external_bindings=False,
    )

    assert len(errors) == 1
    assert errors[0].startswith("verifier_structure:")


def test_accepted_ledger_rejects_extra_or_private_fields(tmp_path: Path, verifier: Any) -> None:
    row = {
        "status": "accepted",
        "frame_id": 1,
        "captured_at_ns": 10,
        "received_at_ns": 20,
        "session_id": "session-a",
        "source_id": "capture-card-primary",
        "pixel_digest": "a" * 64,
        "raw_bytes": "LEAK",
    }
    row["frame_digest"] = verifier._frame_identity_digest(
        session_id=row["session_id"],
        source_id=row["source_id"],
        frame_id=row["frame_id"],
        captured_at_ns=row["captured_at_ns"],
        admitted_at_ns=row["received_at_ns"],
        pixel_digest_value=row["pixel_digest"],
    )
    normalized = {key: value for key, value in row.items() if key != "status"}
    ledger = tmp_path / "accepted.jsonl"
    ledger.write_bytes(verifier.canonical_json(row) + b"\n")
    digest = verifier._canonical_digest([normalized])
    report = {
        "capture": {"source_id": "capture-card-primary"},
        "admission": {
            "accepted_count": 1,
            "accepted_packet_count": 1,
            "accepted_frame_ledger_sha256": digest,
        },
        "lineage": {"accepted_frame_ledger_sha256": digest},
        "public_selected_rows": [],
    }

    errors = verifier._accepted_ledger_errors(ledger, report, {})

    assert any(error.startswith("accepted_ledger_extra_fields:0:raw_bytes") for error in errors)


def test_runner_rejects_symlinked_binding(tmp_path: Path, runner: Any) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(runner.VC003RunError, match="symlinks/reparse"):
        runner._require_file(link, "fixture")
