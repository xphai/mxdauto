"""Fast contract and negative tests for the offline capture pressure report."""

from __future__ import annotations

import copy
import json
import math
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import maple_automation_core.capture.stress as stress
from maple_automation_core.capture.stress import (
    CapturePressureConfig,
    CapturePressureError,
    CapturePressureReport,
    run_capture_pressure,
    verify_capture_pressure_report,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "capture-pressure-report.schema.json"


def _small_report() -> dict[str, object]:
    return run_capture_pressure(
        publish_take_operations=64,
        lifecycle_races=8,
        repetitions=3,
        timeout_s=0.2,
        generated_at="2026-08-29T00:00:00Z",
    ).to_dict()


def test_small_pressure_report_is_deterministic_and_schema_valid() -> None:
    payload = _small_report()
    assert payload["status"] == "PASS"
    assert payload["deterministic"] is True
    assert payload["requirements"]["coverage"] == "SMOKE"  # type: ignore[index]
    assert payload["summary"]["total_lightweight_operations"] >= 64  # type: ignore[index]
    assert payload["concurrency"]["observed_overlap_operations"] >= 1  # type: ignore[index]
    lifecycle = payload["runs"][0]["lifecycle"]  # type: ignore[index]
    assert lifecycle["backend_status"] == "PASS"  # type: ignore[index]
    assert lifecycle["backend_start_checks"] == 16  # type: ignore[index]
    assert lifecycle["backend_blocked_read_checks"] == 16  # type: ignore[index]
    assert lifecycle["backend_reset_checks"] == 15  # type: ignore[index]
    assert lifecycle["backend_stop_checks"] == 16  # type: ignore[index]
    verify_capture_pressure_report(payload)

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload))
    assert errors == []


def test_same_seed_has_same_summary_digest() -> None:
    first = _small_report()
    second = _small_report()
    assert [run["summary_digest"] for run in first["runs"]] == [  # type: ignore[index]
        run["summary_digest"]
        for run in second["runs"]  # type: ignore[index]
    ]
    assert first["canonical_report_sha256"] == second["canonical_report_sha256"]


def test_invalid_scale_and_repetition_are_rejected() -> None:
    with pytest.raises(CapturePressureError):
        CapturePressureConfig(publish_take_operations=0)
    with pytest.raises(CapturePressureError):
        CapturePressureConfig(repetitions=2)
    with pytest.raises(CapturePressureError):
        CapturePressureConfig(
            publish_take_operations=64,
            lifecycle_races=8,
            enforce_minimums=True,
        )
    for timeout_s in (
        math.nan,
        math.inf,
        -math.inf,
        10**1000,
        stress.threading.TIMEOUT_MAX * 2.0,
    ):
        with pytest.raises(CapturePressureError, match="timeout_s"):
            CapturePressureConfig(timeout_s=timeout_s)
    assert CapturePressureConfig(timeout_s=stress.threading.TIMEOUT_MAX).to_dict()[
        "timeout_s"
    ] == float(stress.threading.TIMEOUT_MAX)


def test_derived_pressure_deadline_is_bounded_before_threading_waits() -> None:
    maximum = float(stress.threading.TIMEOUT_MAX)
    assert stress._bounded_overall_timeout(1, maximum) == maximum
    assert stress._bounded_overall_timeout(10**1000, 0.1) == maximum


def test_report_digest_and_counter_tampering_are_rejected() -> None:
    payload = _small_report()
    tampered_digest = copy.deepcopy(payload)
    tampered_digest["canonical_report_sha256"] = "0" * 64
    with pytest.raises(CapturePressureError):
        verify_capture_pressure_report(tampered_digest)

    tampered_counter = copy.deepcopy(payload)
    tampered_counter["runs"][0]["lightweight"]["pending"] = 1  # type: ignore[index]
    with pytest.raises(CapturePressureError):
        verify_capture_pressure_report(tampered_counter)


def test_unknown_schema_property_is_rejected() -> None:
    payload = _small_report()
    payload["unexpected"] = True
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(payload))
    assert any(error.validator == "additionalProperties" for error in errors)


@pytest.mark.parametrize(
    ("function", "value", "name"),
    [
        (stress._ensure_non_negative_int, -1, "seed"),
        (stress._ensure_non_negative_int, True, "seed"),
        (stress._ensure_positive_int, 0, "count"),
        (stress._ensure_positive_int, 1.5, "count"),
        (stress._ensure_sha256, "0" * 63, "digest"),
        (stress._ensure_sha256, "g" * 64, "digest"),
        (stress._ensure_commit, "0" * 39, "commit"),
        (stress._ensure_commit, "g" * 40, "commit"),
    ],
)
def test_strict_scalar_helpers_reject_bad_evidence(
    function: object, value: object, name: str
) -> None:
    with pytest.raises(CapturePressureError):
        function(value, name)  # type: ignore[operator]


def test_strict_scalar_helpers_canonicalize_and_reject_non_json_values() -> None:
    assert stress._ensure_sha256("A" * 64, "digest") == "a" * 64
    assert stress._ensure_commit("B" * 40, "commit") == "b" * 40
    assert stress._utc_timestamp().endswith("Z")
    with pytest.raises(CapturePressureError, match="strict JSON"):
        stress._canonical_json({"not_json": math.nan})


def test_memory_helpers_cover_missing_samples_and_linear_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert stress._quantize_memory(None) is None
    assert stress._quantize_memory(1_048_577) == 1_048_576
    assert stress._memory_delta(None, 1) is None
    assert stress._memory_delta(4, 2) == 0
    assert stress._memory_delta(2, 4) == 2

    points: dict[str, int | None] = {
        "before_tracemalloc_current": 100,
        "mid_tracemalloc_current": 100,
        "after_tracemalloc_current": 10 * stress.MEMORY_BUCKET_BYTES + 100,
        "after_tracemalloc_peak": 11 * stress.MEMORY_BUCKET_BYTES + 100,
        "before_rss": None,
        "mid_rss": None,
        "after_rss": None,
        "after_rss_peak": None,
    }
    report = stress._memory_report(points)
    assert report["tracemalloc"]["linear_growth_observed"] is True
    assert report["tracemalloc"]["within_threshold"] is True
    assert report["rss"]["available"] is False
    assert report["evidence_complete"] is False
    assert report["passed"] is False

    linear_points = {
        "before_tracemalloc_current": 100,
        "mid_tracemalloc_current": 10 * stress.MEMORY_BUCKET_BYTES + 100,
        "after_tracemalloc_current": 20 * stress.MEMORY_BUCKET_BYTES + 100,
        "after_tracemalloc_peak": 20 * stress.MEMORY_BUCKET_BYTES + 100,
        "before_rss": None,
        "mid_rss": None,
        "after_rss": None,
        "after_rss_peak": None,
    }
    linear_report = stress._memory_report(linear_points)
    assert linear_report["tracemalloc"]["linear_growth_observed"] is True

    monkeypatch.setattr(stress, "_rss_bytes", lambda: None)
    points.clear()
    stress._capture_memory_point(points, "before")
    assert points == {
        "before_tracemalloc_current": None,
        "before_tracemalloc_peak": None,
        "before_rss": None,
    }


def test_full_size_pixel_cas_reports_each_integrity_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def identity_validate(_spec: object, value: bytearray) -> bytearray:
        # Returning the mutable decode view intentionally models a broken copy.
        return value

    class BrokenStore:
        def __init__(self, _directory: object) -> None:
            pass

        def put_artifact(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(pixel_digest="0" * 64, ref="cas://sha256/bad")

        def read(self, *_args: object, **_kwargs: object) -> bytes:
            return b"bad"

        def exists(self, *_args: object, **_kwargs: object) -> bool:
            return False

    monkeypatch.setattr(stress, "validate_pixels", identity_validate)

    def broken_digest(_spec: object, data: object) -> str:
        return "f" * 64 if isinstance(data, bytearray) else "e" * 64

    monkeypatch.setattr(stress, "pixel_digest", broken_digest)
    monkeypatch.setattr(stress, "PixelStore", BrokenStore)
    evidence = stress._run_full_size_pixel_cas()
    assert evidence["status"] == "FAIL"
    assert evidence["failures"] == [
        "pixel_cas_artifact_digest",
        "pixel_cas_bytes_mismatch",
        "pixel_cas_missing",
        "pixel_cas_ref_mismatch",
        "pixel_cas_rehash_mismatch",
        "pixel_copy_not_owned",
        "pixel_known_answer_mismatch",
    ]

    monkeypatch.setattr(
        stress,
        "validate_pixels",
        lambda *_args: (_ for _ in ()).throw(ValueError()),
    )
    failed = stress._run_full_size_pixel_cas()
    assert failed["status"] == "FAIL"
    assert failed["failures"] == ["pixel_cas_exception"]


def test_lightweight_tail_and_compatibility_lifecycle_helper() -> None:
    memory_points: dict[str, int | None] = {}
    result = stress._run_lightweight_pressure(
        operation_count=70,
        seed=stress.DEFAULT_SEED,
        memory_points=memory_points,
        timeout_s=0.2,
    )
    assert result["status"] == "PASS"
    assert result["publish_operations"] == 70
    assert result["take_operations"] == 70
    assert result["total_operations"] == 140
    assert result["final_drain_sequence"] == 70
    assert "mid_tracemalloc_current" in memory_points
    assert "after_tracemalloc_current" in memory_points
    assert (
        stress._run_one_lifecycle_race(
            0,
            stress.DEFAULT_SEED,
            0.2,
        )
        == stress._PersistentLifecycleRunner._new_failures()
    )


def test_lightweight_interleaving_is_seed_bound_and_counts_real_overlap() -> None:
    first = stress._run_lightweight_pressure(128, stress.DEFAULT_SEED, timeout_s=0.2)
    second = stress._run_lightweight_pressure(128, stress.DEFAULT_SEED + 1, timeout_s=0.2)
    assert first["status"] == second["status"] == "PASS"
    assert first["event_ledger_digest"] != second["event_ledger_digest"]
    assert first["overlap_operation_count"] == stress._expected_lightweight_overlap_count(
        128,
        stress.DEFAULT_SEED,
    )
    assert second["overlap_operation_count"] == stress._expected_lightweight_overlap_count(
        128,
        stress.DEFAULT_SEED + 1,
    )
    assert 0 < first["concurrent_publish_operations"] < first["publish_operations"]
    assert first["concurrent_publish_take_operations"] < first["total_operations"]


def test_fake_lifecycle_backend_stop_deadline_path() -> None:
    evidence = stress._run_vc003_fake_lifecycle(1.0)
    assert evidence["status"] == "PASS"
    assert evidence["non_exit_mode"] == "source_backend_stop_deadline"
    assert evidence["non_exit_residual_observed"] is True
    assert evidence["non_exit_cleanup_cleared"] is True
    assert evidence["threads_left_alive"] == 0


def test_process_rss_probe_covers_proc_and_resource_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stress.os, "name", "posix")

    class Statm:
        def is_file(self) -> bool:
            return True

        def read_text(self, **_kwargs: object) -> str:
            return "100 2"

    monkeypatch.setattr(stress.os, "sysconf", lambda _name: 4096, raising=False)
    monkeypatch.setattr(stress, "Path", lambda _path: Statm())
    assert stress._rss_bytes() == 2 * stress.os.sysconf("SC_PAGE_SIZE")

    class NoStatm:
        def is_file(self) -> bool:
            return False

    resource = types.ModuleType("resource")
    resource.RUSAGE_SELF = object()  # type: ignore[attr-defined]
    resource.getrusage = lambda _who: SimpleNamespace(ru_maxrss=7)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(stress, "Path", lambda _path: NoStatm())
    assert stress._rss_bytes() == 7 * 1024

    class BrokenStatm:
        def is_file(self) -> bool:
            raise OSError("statm unavailable")

    monkeypatch.setattr(stress, "Path", lambda _path: BrokenStatm())
    assert stress._rss_bytes() is None


def test_session_slot_classifies_authoritative_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = stress._StressSample.make("session-a", 1, stress.DEFAULT_SEED)
    stopped = stress._SessionSlot("session-a")
    stopped.start()
    assert stopped.session_id == "session-a"
    assert (
        stopped.publish(stress._StressSample.make("other-session", 1, stress.DEFAULT_SEED)).status
        == "old_session"
    )
    stopped.slot.stop()
    assert stopped.publish(sample).status == "stopped"

    old = stress._SessionSlot("session-a")
    old.start()
    old.slot.reset("session-b")
    assert old.publish(sample).status == "old_session"

    unexpected = stress._SessionSlot("session-a")
    unexpected.start()

    def raise_unexpected(_sample: object) -> object:
        raise RuntimeError("unexpected slot failure")

    monkeypatch.setattr(unexpected.slot, "publish", raise_unexpected)
    with pytest.raises(RuntimeError, match="unexpected slot failure"):
        unexpected.publish(sample)


def test_status_checker_reports_each_counter_invariant() -> None:
    status = SimpleNamespace(
        pending=2,
        in_flight=3,
        discarded_on_error=-1,
        max_depth=2,
        delivered=0,
        superseded=0,
        discarded_on_reset=0,
        produced=9,
        accounted=0,
    )
    failures = stress._check_status(status)
    assert failures == [
        "pending_not_binary",
        "in_flight_not_binary",
        "discarded_on_error_negative",
        "max_depth_not_one",
        "counter_equation",
    ]
    assert stress._check_status(status, require_max_depth=False) == [
        "pending_not_binary",
        "in_flight_not_binary",
        "discarded_on_error_negative",
        "counter_equation",
    ]


def test_git_and_artifact_helpers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def raise_os_error(*_args: object, **_kwargs: object) -> object:
        raise OSError("git unavailable")

    monkeypatch.setattr(stress.subprocess, "run", raise_os_error)
    assert stress._git_head(tmp_path) == "0" * 40

    monkeypatch.setattr(
        stress.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="g" * 40),
    )
    assert stress._git_head(tmp_path) == "0" * 40
    assert stress._artifact_hash(tmp_path / "missing.bin", b"fallback") == stress._sha256_bytes(
        b"fallback"
    )
    repo_root = stress._default_repo_root()
    assert repo_root == ROOT
    assert (repo_root / "pyproject.toml").is_file()
    assert (
        repo_root / "src" / "maple_automation_core" / "capture" / "stress.py"
    ).resolve() == Path(stress.__file__).resolve()


def test_default_repo_root_fails_closed_for_installed_wheel_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installed_source = (
        tmp_path
        / "venv"
        / "Lib"
        / "site-packages"
        / "maple_automation_core"
        / "capture"
        / "stress.py"
    )
    installed_source.parent.mkdir(parents=True)
    installed_source.write_text("# installed-wheel fixture\n", encoding="utf-8")
    monkeypatch.setattr(stress, "__file__", str(installed_source))

    with pytest.raises(CapturePressureError, match="pass repo_root explicitly"):
        run_capture_pressure(
            publish_take_operations=64,
            lifecycle_races=8,
            repetitions=3,
            timeout_s=0.2,
            generated_at="2026-08-29T00:00:00Z",
        )

    report = run_capture_pressure(
        publish_take_operations=64,
        lifecycle_races=8,
        repetitions=3,
        timeout_s=0.2,
        repo_root=ROOT,
        generated_at="2026-08-29T00:00:00Z",
    )
    assert report.status == "PASS"
    with pytest.raises(CapturePressureError, match="pass repo_root explicitly"):
        report.assert_valid()


def test_report_wrapper_serializes_and_delegates_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _small_report()
    report = CapturePressureReport(source)
    assert report.status == "PASS"
    assert report.deterministic is True
    assert report.report_digest == source["canonical_report_sha256"]
    assert report.canonical_report_sha256 == report.report_digest
    assert report.to_dict() == source
    assert json.loads(report.to_json()) == source
    destination = report.write_json(tmp_path / "nested" / "report.json")
    assert destination.read_text(encoding="utf-8").endswith("\n")
    called: list[object] = []
    monkeypatch.setattr(
        stress,
        "verify_capture_pressure_report",
        lambda payload: called.append(payload),
    )
    report.assert_valid()
    assert called == [source]


def _valid_memory_evidence() -> dict[str, object]:
    metric = {
        "relative_to_before": True,
        "available": True,
        "before_bytes": 0,
        "mid_bytes": stress.MEMORY_BUCKET_BYTES,
        "after_bytes": 2 * stress.MEMORY_BUCKET_BYTES,
        "peak_bytes": 3 * stress.MEMORY_BUCKET_BYTES,
        "growth_bytes": 2 * stress.MEMORY_BUCKET_BYTES,
        "threshold_bytes": stress.MEMORY_GROWTH_THRESHOLD_BYTES,
        "linear_growth_observed": False,
        "within_threshold": True,
    }
    return {
        "scope": "core_owned_raw_latest",
        "measurement": "tracemalloc_and_process_rss",
        "bucket_bytes": stress.MEMORY_BUCKET_BYTES,
        "threshold_bytes": stress.MEMORY_GROWTH_THRESHOLD_BYTES,
        "tracemalloc": copy.deepcopy(metric),
        "rss": copy.deepcopy(metric),
        "evidence_complete": True,
        "linear_growth_observed": False,
        "passed": True,
    }


def test_memory_evidence_verifier_rejects_tampered_semantics() -> None:
    valid = _valid_memory_evidence()
    stress._verify_memory_evidence(valid)
    cases: list[tuple[str, object]] = []

    missing = copy.deepcopy(valid)
    del missing["scope"]
    cases.append(("keys", missing))

    identity = copy.deepcopy(valid)
    identity["scope"] = "other"
    cases.append(("identity", identity))

    not_mapping = copy.deepcopy(valid)
    not_mapping["tracemalloc"] = []
    cases.append(("metric mapping", not_mapping))

    metric_keys = copy.deepcopy(valid)
    del metric_keys["tracemalloc"]["peak_bytes"]  # type: ignore[index]
    cases.append(("metric keys", metric_keys))

    threshold = copy.deepcopy(valid)
    threshold["tracemalloc"]["threshold_bytes"] = 1  # type: ignore[index]
    cases.append(("metric threshold", threshold))

    relative = copy.deepcopy(valid)
    relative["tracemalloc"]["relative_to_before"] = False  # type: ignore[index]
    cases.append(("baseline mode", relative))

    sample_type = copy.deepcopy(valid)
    sample_type["tracemalloc"]["mid_bytes"] = "1"  # type: ignore[index]
    cases.append(("sample type", sample_type))

    peak_type = copy.deepcopy(valid)
    peak_type["tracemalloc"]["peak_bytes"] = False  # type: ignore[index]
    cases.append(("peak type", peak_type))

    peak_order = copy.deepcopy(valid)
    peak_order["tracemalloc"]["peak_bytes"] = 0  # type: ignore[index]
    cases.append(("peak order", peak_order))

    peak_threshold = copy.deepcopy(valid)
    peak_threshold["tracemalloc"]["peak_bytes"] = (  # type: ignore[index]
        stress.MEMORY_GROWTH_THRESHOLD_BYTES + stress.MEMORY_BUCKET_BYTES
    )
    cases.append(("peak threshold", peak_threshold))

    negative_sample = copy.deepcopy(valid)
    negative_sample["tracemalloc"]["mid_bytes"] = -1  # type: ignore[index]
    cases.append(("negative sample", negative_sample))

    baseline = copy.deepcopy(valid)
    baseline["tracemalloc"]["before_bytes"] = 1  # type: ignore[index]
    cases.append(("baseline", baseline))

    availability = copy.deepcopy(valid)
    availability["tracemalloc"]["available"] = False  # type: ignore[index]
    cases.append(("availability", availability))

    growth = copy.deepcopy(valid)
    growth["tracemalloc"]["growth_bytes"] = 1  # type: ignore[index]
    cases.append(("growth", growth))

    linear = copy.deepcopy(valid)
    linear["tracemalloc"]["linear_growth_observed"] = True  # type: ignore[index]
    cases.append(("linear", linear))

    within = copy.deepcopy(valid)
    within["tracemalloc"]["within_threshold"] = False  # type: ignore[index]
    cases.append(("within", within))

    complete = copy.deepcopy(valid)
    complete["evidence_complete"] = False
    cases.append(("complete", complete))

    global_linear = copy.deepcopy(valid)
    global_linear["linear_growth_observed"] = True
    cases.append(("global linear", global_linear))

    global_passed = copy.deepcopy(valid)
    global_passed["passed"] = False
    cases.append(("global passed", global_passed))

    for _name, candidate in cases:
        with pytest.raises(CapturePressureError, match="memory|baseline|linear|growth|peak"):
            stress._verify_memory_evidence(candidate)  # type: ignore[arg-type]


def test_pixel_and_fake_lifecycle_verifiers_reject_tampered_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_pixel = {
        "fixture": "full_size_zero_bgr8_v1",
        "width": 1920,
        "height": 1080,
        "channels": 3,
        "byte_length": stress.FULL_SIZE_PIXEL_BYTES,
        "pixel_digest": stress.FULL_SIZE_ZERO_DIGEST,
        "expected_zero_digest": stress.FULL_SIZE_ZERO_DIGEST,
        "copy_verified": True,
        "hash_verified": True,
        "cas_put_get_verified": True,
        "cas_ref": f"cas://sha256/{stress.FULL_SIZE_ZERO_DIGEST}",
        "failures": [],
        "status": "PASS",
    }
    stress._verify_pixel_cas_evidence(valid_pixel)
    for candidate in (
        {**valid_pixel, "fixture": "other"},
        {key: value for key, value in valid_pixel.items() if key != "status"},
        {**valid_pixel, "status": "FAIL"},
        {**valid_pixel, "copy_verified": False},
    ):
        with pytest.raises(CapturePressureError):
            stress._verify_pixel_cas_evidence(candidate)

    valid_fake = stress._run_vc003_fake_lifecycle(0.2)
    monkeypatch.setattr(stress, "_run_vc003_fake_lifecycle", lambda _timeout: dict(valid_fake))
    stress._verify_vc003_fake_lifecycle(valid_fake, 0.2)
    for candidate in (
        {key: value for key, value in valid_fake.items() if key != "status"},
        {**valid_fake, "status": "FAIL"},
        {**valid_fake, "normal_start": False},
        {**valid_fake, "non_exit_mode": "unknown"},
        {**valid_fake, "threads_left_alive": 1},
    ):
        with pytest.raises(CapturePressureError):
            stress._verify_vc003_fake_lifecycle(candidate, 0.2)


def test_phase_summary_verifier_rejects_counter_and_ledger_tampering() -> None:
    payload = _small_report()
    run = payload["runs"][0]  # type: ignore[index]
    configuration = payload["configuration"]  # type: ignore[index]
    config = CapturePressureConfig(
        publish_take_operations=configuration["publish_take_operations"],  # type: ignore[index]
        lifecycle_races=configuration["lifecycle_races"],  # type: ignore[index]
        repetitions=configuration["repetitions"],  # type: ignore[index]
        seed=configuration["seed"],  # type: ignore[index]
        timeout_s=configuration["timeout_s"],  # type: ignore[index]
        enforce_minimums=configuration["enforce_minimums"],  # type: ignore[index]
    )
    lightweight = run["lightweight"]  # type: ignore[index]
    lifecycle = run["lifecycle"]  # type: ignore[index]
    stress._verify_phase_summaries(lightweight, lifecycle, config)

    cases: list[tuple[str, dict[str, object], dict[str, object]]] = []

    missing_light = copy.deepcopy(lightweight)
    del missing_light["status"]
    cases.append(("lightweight keys", missing_light, copy.deepcopy(lifecycle)))

    publish = copy.deepcopy(lightweight)
    publish["publish_operations"] += 1
    cases.append(("publish count", publish, copy.deepcopy(lifecycle)))

    take = copy.deepcopy(lightweight)
    take["take_operations"] -= 1
    cases.append(("take count", take, copy.deepcopy(lifecycle)))

    total = copy.deepcopy(lightweight)
    total["total_operations"] -= 1
    cases.append(("total count", total, copy.deepcopy(lifecycle)))

    concurrent_publish = copy.deepcopy(lightweight)
    concurrent_publish["concurrent_publish_operations"] -= 1
    cases.append(("concurrent publish", concurrent_publish, copy.deepcopy(lifecycle)))

    concurrent_take = copy.deepcopy(lightweight)
    concurrent_take["concurrent_take_operations"] -= 1
    cases.append(("concurrent take", concurrent_take, copy.deepcopy(lifecycle)))

    concurrent_total = copy.deepcopy(lightweight)
    concurrent_total["concurrent_publish_take_operations"] -= 1
    cases.append(("concurrent total", concurrent_total, copy.deepcopy(lifecycle)))

    below_minimum = copy.deepcopy(lightweight)
    below_minimum["total_operations"] = config.publish_take_operations - 1
    below_minimum["concurrent_publish_take_operations"] = config.publish_take_operations - 1
    cases.append(("operation minimum", below_minimum, copy.deepcopy(lifecycle)))

    overlap = copy.deepcopy(lightweight)
    overlap["overlap_operation_count"] = 0
    cases.append(("overlap", overlap, copy.deepcopy(lifecycle)))

    roles = copy.deepcopy(lightweight)
    roles["concurrent_roles"] = 3
    cases.append(("roles", roles, copy.deepcopy(lifecycle)))

    ledger = copy.deepcopy(lightweight)
    ledger["event_ledger_digest"] = "bad"
    cases.append(("ledger", ledger, copy.deepcopy(lifecycle)))

    produced = copy.deepcopy(lightweight)
    produced["produced"] -= 1
    cases.append(("produced", produced, copy.deepcopy(lifecycle)))

    depth = copy.deepcopy(lightweight)
    depth["max_depth"] = 2
    cases.append(("depth", depth, copy.deepcopy(lifecycle)))

    equation = copy.deepcopy(lightweight)
    equation["superseded"] += 1
    cases.append(("equation", equation, copy.deepcopy(lifecycle)))

    final_drain = copy.deepcopy(lightweight)
    final_drain["final_drain_sequence"] -= 1
    cases.append(("final drain", final_drain, copy.deepcopy(lifecycle)))

    light_failures = copy.deepcopy(lightweight)
    light_failures["failures"] = ["synthetic"]
    cases.append(("lightweight failures", light_failures, copy.deepcopy(lifecycle)))

    missing_lifecycle = copy.deepcopy(lifecycle)
    del missing_lifecycle["status"]
    cases.append(("lifecycle keys", copy.deepcopy(lightweight), missing_lifecycle))

    lifecycle_counts = copy.deepcopy(lifecycle)
    lifecycle_counts["races"] += 1
    cases.append(("lifecycle counts", copy.deepcopy(lightweight), lifecycle_counts))

    lifecycle_invariant = copy.deepcopy(lifecycle)
    lifecycle_invariant["deadlocks"] = 1
    cases.append(("lifecycle invariant", copy.deepcopy(lightweight), lifecycle_invariant))

    lifecycle_status = copy.deepcopy(lifecycle)
    lifecycle_status["status"] = "FAIL"
    cases.append(("lifecycle status", copy.deepcopy(lightweight), lifecycle_status))

    for _name, candidate_lightweight, candidate_lifecycle in cases:
        with pytest.raises(CapturePressureError, match=".*"):
            stress._verify_phase_summaries(candidate_lightweight, candidate_lifecycle, config)


def _resign_report(payload: dict[str, object]) -> dict[str, object]:
    candidate = copy.deepcopy(payload)
    candidate.pop("canonical_report_sha256", None)
    candidate.pop("report_digest", None)
    digest = stress._digest(candidate)
    candidate["canonical_report_sha256"] = digest
    candidate["report_digest"] = digest
    return candidate


def test_report_verifier_rejects_top_level_semantic_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _small_report()

    # These checks are exercised independently below.  Stubbing them here
    # keeps this matrix focused on the report-level binding and status rules.
    monkeypatch.setattr(stress, "_verify_phase_summaries", lambda *_args: None)
    monkeypatch.setattr(stress, "_verify_memory_evidence", lambda *_args: None)
    monkeypatch.setattr(stress, "_verify_pixel_cas_evidence", lambda *_args: None)
    monkeypatch.setattr(stress, "_verify_vc003_fake_lifecycle", lambda *_args: None)
    monkeypatch.setattr(
        stress,
        "_run_full_size_pixel_cas",
        lambda: copy.deepcopy(base["pixel_cas"]),  # type: ignore[arg-type]
    )

    def candidate(mutator: object) -> dict[str, object]:
        value = copy.deepcopy(base)
        mutator(value)  # type: ignore[operator]
        return _resign_report(value)

    cases: list[tuple[str, object]] = [
        ("unknown key", lambda p: p.update({"unexpected": True})),
        ("missing key", lambda p: p.pop("status")),
        ("schema identity", lambda p: p.update({"schema_version": "0.0.0"})),
        ("execution mode", lambda p: p.update({"execution_mode": "live"})),
        ("deterministic type", lambda p: p.update({"deterministic": 1})),
        ("repeat count", lambda p: p.update({"repeat_count": 2})),
        ("configuration type", lambda p: p.update({"configuration": []})),
        (
            "configuration keys",
            lambda p: p["configuration"].pop("seed"),  # type: ignore[index]
        ),
        (
            "configuration hash",
            lambda p: p["configuration"].update({"seed": 123}),  # type: ignore[index]
        ),
        (
            "configuration repetitions",
            lambda p: (
                p["configuration"].update({"repetitions": 4}),  # type: ignore[index]
                p.update(
                    {
                        "config_sha256": stress._digest(p["configuration"]),  # type: ignore[arg-type,index]
                    }
                ),
            ),
        ),
        ("environment type", lambda p: p.update({"environment": []})),
        (
            "environment binding",
            lambda p: p["environment"].update({"network_used": True}),  # type: ignore[index]
        ),
        (
            "schedule binding",
            lambda p: p["schedules"].update({"barrier_controlled": False}),  # type: ignore[index]
        ),
        ("requirements type", lambda p: p.update({"requirements": []})),
        (
            "minimum publish requirement",
            lambda p: p["requirements"].update({"minimum_publish_take_operations": 1}),  # type: ignore[index]
        ),
        (
            "minimum lifecycle requirement",
            lambda p: p["requirements"].update({"minimum_lifecycle_races": 1}),  # type: ignore[index]
        ),
        (
            "publish requirement",
            lambda p: p["requirements"].update({"publish_take_operations": 1}),  # type: ignore[index]
        ),
        (
            "lifecycle requirement",
            lambda p: p["requirements"].update({"lifecycle_races": 1}),  # type: ignore[index]
        ),
        (
            "minimums flag",
            lambda p: p["requirements"].update({"minimums_met": True}),  # type: ignore[index]
        ),
        (
            "evidence flag type",
            lambda p: p["requirements"].update({"evidence_complete": 1}),  # type: ignore[index]
        ),
        ("run count", lambda p: p.update({"runs": []})),
        ("run type", lambda p: p["runs"].__setitem__(0, [])),  # type: ignore[index]
        (
            "run missing key",
            lambda p: p["runs"][0].pop("status"),  # type: ignore[index]
        ),
        (
            "run index",
            lambda p: p["runs"][0].update({"run_index": 2}),  # type: ignore[index]
        ),
        (
            "phase mapping",
            lambda p: p["runs"][0].update({"lightweight": []}),  # type: ignore[index]
        ),
        (
            "evidence mapping",
            lambda p: p["runs"][0].update({"memory": []}),  # type: ignore[index]
        ),
        (
            "run failures type",
            lambda p: p["runs"][0].update({"failures": [1]}),  # type: ignore[index]
        ),
        (
            "summary digest",
            lambda p: p["runs"][0].update({"summary_digest": "0" * 64}),  # type: ignore[index]
        ),
        (
            "run digest",
            lambda p: p["runs"][0].update({"run_digest": "0" * 64}),  # type: ignore[index]
        ),
        (
            "event digest type",
            lambda p: p["runs"][0].update({"event_digest": "bad"}),  # type: ignore[index]
        ),
        (
            "event digest value",
            lambda p: p["runs"][0].update({"event_digest": "0" * 64}),  # type: ignore[index]
        ),
        (
            "run status",
            lambda p: p["runs"][0].update({"status": "FAIL"}),  # type: ignore[index]
        ),
        (
            "evidence complete",
            lambda p: p["requirements"].update({"evidence_complete": False}),  # type: ignore[index]
        ),
        (
            "coverage",
            lambda p: p["requirements"].update({"coverage": "FULL"}),  # type: ignore[index]
        ),
        ("deterministic flag", lambda p: p.update({"deterministic": False})),
        ("report failures", lambda p: p.update({"failures": ["synthetic"]})),
        ("invariants type", lambda p: p.update({"invariants": []})),
        (
            "invariants value",
            lambda p: p["invariants"].update({"memory_bounded": False}),  # type: ignore[index]
        ),
        ("summary type", lambda p: p.update({"summary": []})),
        (
            "summary publish",
            lambda p: p["summary"].update({"publish_operations": 1}),  # type: ignore[index]
        ),
        (
            "summary take",
            lambda p: p["summary"].update({"take_operations": 1}),  # type: ignore[index]
        ),
        (
            "summary total",
            lambda p: p["summary"].update({"total_lightweight_operations": 1}),  # type: ignore[index]
        ),
        (
            "summary races",
            lambda p: p["summary"].update({"lifecycle_races": 1}),  # type: ignore[index]
        ),
        (
            "summary epochs",
            lambda p: p["summary"].update({"epochs_checked": 1}),  # type: ignore[index]
        ),
        (
            "summary counter",
            lambda p: p["summary"].update({"produced": 1}),  # type: ignore[index]
        ),
        (
            "summary invariant",
            lambda p: p["summary"].update({"memory_bounded": False}),  # type: ignore[index]
        ),
        (
            "summary failure count",
            lambda p: p["summary"].update({"invariant_failures": 1}),  # type: ignore[index]
        ),
        ("counter epochs", lambda p: p.update({"counter_epochs": {}})),
        ("timeouts", lambda p: p.update({"timeouts": {}})),
        ("memory summary", lambda p: p.update({"memory": []})),
        ("pixel summary", lambda p: p.update({"pixel_cas": []})),
        (
            "pixel recomputation",
            lambda p: p["pixel_cas"].update({"status": "FAIL"}),  # type: ignore[index]
        ),
        ("fake lifecycle summary", lambda p: p.update({"vc003_fake_lifecycle": []})),
        (
            "concurrency",
            lambda p: p["concurrency"].update({"role_count": 3}),  # type: ignore[index]
        ),
        ("input audit type", lambda p: p.update({"input_audit": []})),
        (
            "input audit policy",
            lambda p: p["input_audit"].update({"input_owner": "core"}),  # type: ignore[index]
        ),
        (
            "input audit activity",
            lambda p: p["input_audit"].update({"network_call_count": 1}),  # type: ignore[index]
        ),
        ("status value", lambda p: p.update({"status": "BROKEN"})),
        ("artifacts type", lambda p: p.update({"artifacts": []})),
        (
            "artifact ids",
            lambda p: p["artifacts"].__setitem__(0, {"artifact_id": "other"}),  # type: ignore[index]
        ),
        (
            "artifact binding",
            lambda p: p["artifacts"][0].update({"kind": "schema"}),  # type: ignore[index]
        ),
        ("artifact digest", lambda p: p.update({"artifact_list_sha256": "0" * 64})),
        (
            "deterministic evidence digest",
            lambda p: p.update({"deterministic_evidence_sha256": "0" * 64}),
        ),
        ("tool artifact digest", lambda p: p.update({"tool_artifact_sha256": "0" * 64})),
        ("report status", lambda p: p.update({"status": "FAIL"})),
    ]

    for _name, mutator in cases:
        with pytest.raises(CapturePressureError, match=".*"):
            verify_capture_pressure_report(candidate(mutator))

    monkeypatch.setattr(stress, "_run_full_size_pixel_cas", lambda: {})
    with pytest.raises(CapturePressureError, match="could not be recomputed"):
        verify_capture_pressure_report(_resign_report(base))


def test_report_verifier_checks_input_type_and_alias_digest_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(CapturePressureError, match="mapping"):
        verify_capture_pressure_report([])  # type: ignore[arg-type]

    base = _small_report()
    candidate = copy.deepcopy(base)
    candidate["report_digest"] = "0" * 64
    with pytest.raises(CapturePressureError, match="differ"):
        verify_capture_pressure_report(candidate)

    valid_fake = base["vc003_fake_lifecycle"]

    def unexpected_recomputation(_timeout: float) -> dict[str, object]:
        raise AssertionError("fake lifecycle verification must not rerun worker threads")

    monkeypatch.setattr(stress, "_run_vc003_fake_lifecycle", unexpected_recomputation)
    stress._verify_vc003_fake_lifecycle(valid_fake, 0.2)  # type: ignore[arg-type]
