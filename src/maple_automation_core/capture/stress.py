"""Deterministic pressure verification for the Core-owned raw latest slot.

The verifier in this module deliberately targets :class:`RawLatestSlot`, not a
camera, operating-system queue, or vendor driver.  A slot sample is a small,
immutable tuple carrying a session, sequence, marker, and digest.  The marker
and digest make it possible to detect a torn tuple if a future slot
implementation starts publishing fields independently.

Two phases are run for each deterministic repetition:

* a high-volume four-role publish/take schedule with an observed blocked-read
  hand-off, and
* lifecycle/reset/stop/session races using persistent producer, consumer,
  metrics, and controller workers around the public slot object.  The race
  schedule intentionally does not expose scheduler-dependent counters in the
  report.  It records invariant outcomes
  instead, so three repetitions have the same canonical summary on every
  run.

This is an offline synthetic test.  It does not open VC-003, import OpenCV,
make network calls, or write keyboard/mouse/window input.
"""

from __future__ import annotations

import gc
import json
import math
import os
import platform
import queue
import subprocess
import tempfile
import threading
import tracemalloc
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, cast

from maple_automation_core.capture.pixel_store import (
    UNKNOWN_DEVICE_FINGERPRINT_SHA256,
    PixelSpec,
    PixelStore,
    pixel_digest,
    validate_pixels,
)
from maple_automation_core.capture.vc003_source import (
    BackendFrame,
    NegotiatedCaptureFacts,
    RawLatestSlot,
    RawLatestStatus,
    VC003RawFrame,
    VC003Source,
    VC003SourceConfig,
)
from maple_automation_core.domain.frame import FrameSize

SCHEMA_VERSION = "1.0.0"
REPORT_TYPE = "capture-pressure"
SCHEDULE_VERSION = "capture-pressure-v1"
DEFAULT_SEED = 0xC0FFEE
MIN_PUBLISH_TAKE_OPERATIONS = 100_000
MIN_LIFECYCLE_RACES = 1_000
DEFAULT_TIMEOUT_S = 2.0
FULL_SIZE_PIXEL_BYTES = 6_220_800
FULL_SIZE_ZERO_DIGEST = "c23a85d7fe7002f426293d40fb9a02a8795c41f7ef7ea801b082a969793ab4bc"
MEMORY_GROWTH_THRESHOLD_BYTES = 64 * 1024 * 1024
MEMORY_BUCKET_BYTES = 1024 * 1024

_ZERO_SHA256 = "0" * 64
_ZERO_COMMIT = "0" * 40
_LIMITATIONS = (
    "Core-owned raw latest slot only; upstream backend/driver/vendor queue depth is unknown.",
    "Offline synthetic stress; no VC-003 device, network, keyboard, mouse, receiver, "
    "or window access.",
    "The report does not measure sensor exposure time or glass-to-glass latency.",
)


class CapturePressureError(ValueError):
    """Raised for invalid configuration or semantically invalid evidence."""


def _canonical_json(value: Any) -> bytes:
    """Encode a strict JSON value in the report's canonical form."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapturePressureError("report value is not strict JSON") from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _ensure_non_negative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise CapturePressureError(f"{name} must be an integer >= 0")
    return value


def _ensure_positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise CapturePressureError(f"{name} must be an integer > 0")
    return value


def _ensure_positive_wait_timeout(value: object, name: str) -> float:
    """Return a finite timeout accepted by the platform threading API."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CapturePressureError(f"{name} must be a positive number")
    try:
        timeout = float(value)
    except OverflowError as exc:
        raise CapturePressureError(f"{name} must be a positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > threading.TIMEOUT_MAX:
        raise CapturePressureError(
            f"{name} must be a positive number no greater than threading.TIMEOUT_MAX"
        )
    return timeout


def _ensure_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise CapturePressureError(f"{name} must be a SHA-256 hex string")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise CapturePressureError(f"{name} must be a SHA-256 hex string") from exc
    return value.lower()


def _ensure_commit(value: object, name: str = "source_commit") -> str:
    if not isinstance(value, str) or len(value) != 40:
        raise CapturePressureError(f"{name} must be a 40-character git SHA-1")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise CapturePressureError(f"{name} must be a 40-character git SHA-1") from exc
    return value.lower()


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rss_bytes() -> int | None:
    """Return process resident bytes without adding a runtime dependency."""

    try:
        if os.name == "nt":
            import ctypes

            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_: ClassVar[list[tuple[str, Any]]] = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            get_current_process = ctypes.windll.kernel32.GetCurrentProcess
            get_current_process.restype = ctypes.c_void_p
            process = get_current_process()
            get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessMemoryCounters),
                ctypes.c_ulong,
            ]
            get_process_memory_info.restype = ctypes.c_bool
            success = get_process_memory_info(process, ctypes.byref(counters), counters.cb)
            return int(counters.WorkingSetSize) if success else None

        statm = Path("/proc/self/statm")
        if statm.is_file():
            fields = statm.read_text(encoding="ascii").split()
            if len(fields) >= 2:
                sysconf = getattr(os, "sysconf", None)
                if callable(sysconf):
                    page_size = int(sysconf("SC_PAGE_SIZE"))
                    return int(fields[1]) * page_size
        import importlib

        resource = importlib.import_module("resource")
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if os.name == "darwin" else value * 1024
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _capture_memory_point(points: dict[str, int | None], name: str) -> None:
    if tracemalloc.is_tracing():
        current, peak = tracemalloc.get_traced_memory()
        points[f"{name}_tracemalloc_current"] = int(current)
        points[f"{name}_tracemalloc_peak"] = int(peak)
    else:
        points[f"{name}_tracemalloc_current"] = None
        points[f"{name}_tracemalloc_peak"] = None
    points[f"{name}_rss"] = _rss_bytes()


def _quantize_memory(value: int | None) -> int | None:
    if value is None:
        return None
    return (value // MEMORY_BUCKET_BYTES) * MEMORY_BUCKET_BYTES


def _memory_delta(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return max(0, after - before)


def _memory_report(points: Mapping[str, int | None]) -> dict[str, Any]:
    """Reduce real memory samples to stable, coarse-grained report values."""

    def metric(prefix: str, peak_prefix: str | None = None) -> dict[str, Any]:
        peak_prefix = prefix if peak_prefix is None else peak_prefix
        raw_before = points.get(f"before_{prefix}")
        raw_mid = points.get(f"mid_{prefix}")
        raw_after = points.get(f"after_{prefix}")
        raw_peak = points.get(f"after_{peak_prefix}_peak")
        before = 0 if raw_before is not None else None
        mid = (
            _quantize_memory(max(0, raw_mid - raw_before))
            if raw_mid is not None and raw_before is not None
            else None
        )
        after = (
            _quantize_memory(max(0, raw_after - raw_before))
            if raw_after is not None and raw_before is not None
            else None
        )
        peak = (
            _quantize_memory(max(0, raw_peak - raw_before))
            if raw_peak is not None and raw_before is not None
            else None
        )
        growth = _memory_delta(before, after)
        first_growth = _memory_delta(before, mid)
        tail_growth = _memory_delta(mid, after)
        linear = (
            growth is None
            or tail_growth is None
            or (
                tail_growth > 8 * MEMORY_BUCKET_BYTES
                and tail_growth >= max(MEMORY_BUCKET_BYTES, (first_growth or 0) // 2)
            )
        )
        return {
            "relative_to_before": True,
            "available": before is not None and mid is not None and after is not None,
            "before_bytes": before,
            "mid_bytes": mid,
            "after_bytes": after,
            "peak_bytes": peak,
            "growth_bytes": growth,
            "threshold_bytes": MEMORY_GROWTH_THRESHOLD_BYTES,
            "linear_growth_observed": linear,
            "within_threshold": (
                growth is not None
                and growth <= MEMORY_GROWTH_THRESHOLD_BYTES
                and (peak is None or peak <= MEMORY_GROWTH_THRESHOLD_BYTES)
            ),
        }

    tracemalloc_metric = metric("tracemalloc_current", "tracemalloc")
    rss_metric = metric("rss")
    complete = bool(tracemalloc_metric["available"] and rss_metric["available"])
    linear = bool(
        tracemalloc_metric["linear_growth_observed"] or rss_metric["linear_growth_observed"]
    )
    passed = bool(
        complete
        and not linear
        and tracemalloc_metric["within_threshold"]
        and rss_metric["within_threshold"]
    )
    return {
        "scope": "core_owned_raw_latest",
        "measurement": "tracemalloc_and_process_rss",
        "bucket_bytes": MEMORY_BUCKET_BYTES,
        "threshold_bytes": MEMORY_GROWTH_THRESHOLD_BYTES,
        "tracemalloc": tracemalloc_metric,
        "rss": rss_metric,
        "evidence_complete": complete,
        "linear_growth_observed": linear,
        "passed": passed,
    }


def _run_full_size_pixel_cas() -> dict[str, Any]:
    """Run an independent full-size Pixel V1 copy/hash/CAS known-answer test."""

    spec = PixelSpec(width=1920, height=1080, channels=3, pixel_format="BGR8", dtype="uint8")
    failures: list[str] = []
    expected_digest: str | None = None
    cas_ref: str | None = None
    copied: bytes | None = None
    readback: bytes | None = None
    try:
        mutable_decode_view = bytearray(spec.length)
        copied = validate_pixels(spec, mutable_decode_view)
        mutable_decode_view[0] = 0xA5
        mutable_decode_view[-1] = 0x5A
        copy_verified = copied[0] == 0 and copied[-1] == 0
        if not copy_verified:
            failures.append("pixel_copy_not_owned")
        expected_digest = pixel_digest(spec, copied)
        if expected_digest != FULL_SIZE_ZERO_DIGEST:
            failures.append("pixel_known_answer_mismatch")
        with tempfile.TemporaryDirectory(prefix="capture-pressure-cas-") as directory:
            store = PixelStore(directory)
            artifact = store.put_artifact(
                spec,
                copied,
                privacy_class="private",
                retention_class="ephemeral",
                source_provenance_id="capture-pressure-full-size-kat",
                session_id="capture-pressure-kat",
                source_sequence=1,
            )
            cas_ref = artifact.ref
            readback = store.read(expected_digest, spec)
            if not store.exists(expected_digest, spec):
                failures.append("pixel_cas_missing")
            if artifact.pixel_digest != expected_digest:
                failures.append("pixel_cas_artifact_digest")
            if cas_ref != f"cas://sha256/{expected_digest}":
                failures.append("pixel_cas_ref_mismatch")
            if readback != copied:
                failures.append("pixel_cas_bytes_mismatch")
            if pixel_digest(spec, readback) != expected_digest:
                failures.append("pixel_cas_rehash_mismatch")
    except (OSError, TypeError, ValueError):
        failures.append("pixel_cas_exception")
    copy_ok = copied is not None and copied[0] == 0 and copied[-1] == 0
    read_ok = readback is not None and expected_digest is not None and readback == copied
    passed = not failures and expected_digest == FULL_SIZE_ZERO_DIGEST and copy_ok and read_ok
    return {
        "fixture": "full_size_zero_bgr8_v1",
        "width": spec.width,
        "height": spec.height,
        "channels": spec.channels,
        "byte_length": spec.length,
        "pixel_digest": expected_digest,
        "expected_zero_digest": FULL_SIZE_ZERO_DIGEST,
        "copy_verified": copy_ok,
        "hash_verified": expected_digest == FULL_SIZE_ZERO_DIGEST,
        "cas_put_get_verified": read_ok,
        "cas_ref": cas_ref,
        "failures": sorted(set(failures)),
        "status": "PASS" if passed else "FAIL",
    }


class _StressBackend:
    """Small deterministic backend script used only by the offline fixture."""

    def __init__(
        self,
        frames: list[bytes | BackendFrame | BaseException | None] | None = None,
        *,
        block_start: bool = False,
        block_read: bool = False,
        start_error: BaseException | None = None,
        stop_blocked: bool = False,
        device_name: str = "VC-003 Video",
    ) -> None:
        self.frames: list[bytes | BackendFrame | BaseException | None] = list(
            frames or [b"\x00" * 6]
        )
        self.block_start = block_start
        self.block_read = block_read
        self.start_error = start_error
        self.stop_blocked = stop_blocked
        self.device_name = device_name
        self.device_fingerprint_sha256 = UNKNOWN_DEVICE_FINGERPRINT_SHA256
        self.negotiated_facts = NegotiatedCaptureFacts(
            width=2,
            height=1,
            fps=60.0,
            fourcc="MJPG",
            backend="dshow",
            backend_api="dshow",
            backend_version="fake-v1",
        )
        self.started = threading.Event()
        self.start_entered = threading.Event()
        self.stopped = threading.Event()
        self.stop_entered = threading.Event()
        self.stop_returned = threading.Event()
        self.release_start = threading.Event()
        self.release_read = threading.Event()
        self.release_stop = threading.Event()
        self.read_entered = threading.Event()
        self.read_count = 0
        self.stop_count = 0

    def start(self) -> None:
        self.start_entered.set()
        if self.block_start:
            self.release_start.wait()
        if self.start_error is not None:
            raise self.start_error
        self.started.set()

    def read(self) -> bytes | BackendFrame | None:
        self.read_entered.set()
        if self.block_read:
            self.release_read.wait()
            return None
        if self.read_count < len(self.frames):
            frame = self.frames[self.read_count]
            self.read_count += 1
            if isinstance(frame, BaseException):
                raise frame
            return frame
        self.stopped.wait(0.0005)
        return None

    def stop(self) -> None:
        self.stop_count += 1
        self.stop_entered.set()
        self.release_read.set()
        if self.stop_blocked:
            self.release_stop.wait()
        self.stopped.set()
        self.stop_returned.set()


def _wait_for(predicate: Callable[[], bool], timeout_s: float) -> bool:
    deadline = monotonic() + timeout_s
    while not predicate():
        if monotonic() >= deadline:
            return False
        threading.Event().wait(0.0005)
    return True


def _run_vc003_fake_lifecycle(timeout_s: float) -> dict[str, Any]:
    """Exercise real ``VC003Source`` states with a deterministic fake matrix."""

    failures: list[str] = []

    def make_config(session_id: str) -> VC003SourceConfig:
        return VC003SourceConfig(
            source_id="capture-card-primary",
            session_id=session_id,
            device_name="VC-003 Video",
            width=2,
            height=1,
            fps=60.0,
            poll_interval_s=0.0001,
        )

    def source_for(
        session_id: str,
        backend: _StressBackend,
        *,
        clock: Callable[[], int] | None = None,
    ) -> VC003Source:
        if clock is None:
            return VC003Source(make_config(session_id), backend_factory=lambda _config: backend)
        return VC003Source(
            make_config(session_id),
            backend_factory=lambda _config: backend,
            clock=clock,
        )

    first_backend = _StressBackend([b"\x00" * 6])
    second_backend = _StressBackend([b"\x00" * 6])
    normal_backends = [first_backend, second_backend]

    def normal_factory(_config: VC003SourceConfig) -> _StressBackend:
        return normal_backends.pop(0)

    normal_source = VC003Source(
        make_config("capture-pressure-vc003-a"), backend_factory=normal_factory
    )
    drain_backend = _StressBackend([b"\x01" * 6])
    drain_source = source_for("capture-pressure-vc003-drain", drain_backend)
    normal_start = normal_read = normal_stop = reset_new_session = normal_final_drain = False
    try:
        normal_source.start()
        normal_start = first_backend.started.wait(timeout_s)
        normal_read = _wait_for(lambda: normal_source.status().produced >= 1, timeout_s)
        sample = normal_source.read(timeout=timeout_s)
        normal_read = (
            normal_read and sample is not None and sample.session_id == "capture-pressure-vc003-a"
        )
        normal_source.stop()
        normal_source.stop()
        normal_status = normal_source.status()
        normal_stop = (
            normal_status.lifecycle == "stopped"
            and not normal_status.thread_alive
            and first_backend.stopped.is_set()
        )
        drain_source.start()
        normal_final_drain = (
            _wait_for(lambda: drain_source.status().produced >= 1, timeout_s)
            and (drained := drain_source.stop()) is not None
            and drained.sequence == 1
            and drain_source.status().final_drain_performed
            and drain_source.status().final_drain_sequence == 1
            and drain_source.status().pending == 0
            and drain_source.status().in_flight == 0
            and drain_source.status().produced == drain_source.status().accounted
        )
        normal_source.reset("capture-pressure-vc003-b")
        normal_source.start()
        reset_new_session = _wait_for(
            lambda: normal_source.status().session_id == "capture-pressure-vc003-b"
            and normal_source.status().produced >= 1,
            timeout_s,
        )
        reset_sample = normal_source.read(timeout=timeout_s)
        reset_new_session = (
            reset_new_session
            and reset_sample is not None
            and reset_sample.session_id == "capture-pressure-vc003-b"
            and normal_source.status().epoch == 1
            and second_backend.started.is_set()
        )
        normal_source.stop()
    except Exception:
        failures.append("normal_lifecycle_exception")
        with suppress(Exception):
            normal_source.stop()
        with suppress(Exception):
            drain_source.stop()

    def run_error_case(
        name: str,
        backend: _StressBackend,
        *,
        wait_for_error: bool = True,
        expected_start_error: bool = False,
    ) -> tuple[bool, bool, bool]:
        source = source_for(f"capture-pressure-vc003-{name}", backend)
        observed = started = cleaned = False
        root_error: str | None = None
        try:
            source.start()
            started = backend.started.wait(timeout_s)
            if wait_for_error:
                observed = _wait_for(
                    lambda: source.status().lifecycle == "error"
                    and source.status().error is not None,
                    timeout_s,
                )
                root_error = source.status().error
            else:
                observed = backend.read_entered.wait(timeout_s)
            source.stop()
            status = source.status()
            cleaned = not status.thread_alive and backend.stopped.is_set()
            if wait_for_error and root_error is not None:
                observed = observed and status.error == root_error
        except Exception:
            if expected_start_error:
                started = backend.start_entered.is_set()
                status = source.status()
                observed = status.lifecycle == "error" and status.error is not None
                with suppress(Exception):
                    source.stop()
                status = source.status()
                cleaned = not status.thread_alive and backend.stopped.is_set()
            else:
                failures.append(f"{name}_exception")
                with suppress(Exception):
                    source.stop()
        return started, observed, cleaned

    blocked_start_backend = _StressBackend([b"\x00" * 6], block_start=True)
    blocked_start_source = source_for("blocked-start", blocked_start_backend)
    blocked_start = blocked_start_cleanup = False
    start_errors: list[BaseException] = []

    def blocked_start_call() -> None:
        try:
            blocked_start_source.start()
        except BaseException as exc:
            start_errors.append(exc)

    start_thread = threading.Thread(target=blocked_start_call, name="capture-pressure-fake-start")
    start_thread.start()
    blocked_start = blocked_start_backend.start_entered.wait(timeout_s)
    blocked_start_backend.release_start.set()
    start_thread.join(timeout_s)
    with suppress(Exception):
        blocked_start_source.stop()
    blocked_start_cleanup = (
        blocked_start
        and not start_thread.is_alive()
        and blocked_start_backend.stopped.wait(timeout_s)
    )

    blocked_read_backend = _StressBackend([b"\x00" * 6], block_read=True)
    blocked_read_started, blocked_read_observed, blocked_read_cleanup = run_error_case(
        "blocked-read", blocked_read_backend, wait_for_error=False
    )
    blocked_read = blocked_read_started and blocked_read_observed and blocked_read_cleanup

    eof_backend = _StressBackend([EOFError("synthetic EOF")])
    eof_started, eof_latched, eof_cleanup = run_error_case("eof", eof_backend)
    read_backend = _StressBackend([RuntimeError("synthetic read failure")])
    read_started, read_latched, read_cleanup = run_error_case("read-error", read_backend)

    drift_spec = {"width": 3, "height": 1, "channels": 3, "dtype": "uint8"}
    drift_backend = _StressBackend([b"\x00" * 6, BackendFrame(data=b"\x00" * 9, spec=drift_spec)])
    drift_started, drift_latched, drift_cleanup = run_error_case("format-drift", drift_backend)

    rollback_backend = _StressBackend(
        [
            BackendFrame(data=b"\x00" * 6, captured_at_ns=100),
            BackendFrame(data=b"\x00" * 6, captured_at_ns=99),
        ]
    )
    rollback_clock_values = iter((100, 99))
    rollback_source = source_for(
        "timestamp-rollback",
        rollback_backend,
        clock=lambda: next(rollback_clock_values),
    )
    rollback_started = rollback_latched = rollback_cleanup = False
    try:
        rollback_source.start()
        rollback_started = rollback_backend.started.wait(timeout_s)
        rollback_latched = _wait_for(
            lambda: rollback_source.status().lifecycle == "error"
            and rollback_source.status().error is not None,
            timeout_s,
        )
        rollback_source.stop()
        rollback_status = rollback_source.status()
        rollback_cleanup = not rollback_status.thread_alive and rollback_backend.stopped.is_set()
    except Exception:
        failures.append("timestamp-rollback_exception")
        with suppress(Exception):
            rollback_source.stop()

    start_error_backend = _StressBackend(start_error=RuntimeError("synthetic open failure"))
    start_error_started, start_error_latched, start_error_cleanup = run_error_case(
        "start-error", start_error_backend, expected_start_error=True
    )
    identity_backend = _StressBackend(device_name="unexpected-device")
    identity_started, identity_latched, identity_cleanup = run_error_case(
        "identity-drift", identity_backend, expected_start_error=True
    )

    # Exercise the source's two-second backend-stop deadline at full scale.
    # Smoke runs use an equivalent bounded child fixture so their fast CI path
    # does not spend two seconds per repetition; both paths record residual
    # observation and explicit cleanup.
    non_exit_mode = "source_backend_stop_deadline"
    non_exit_fatal_cleanup = non_exit_residual_observed = non_exit_cleanup_cleared = False
    non_exit_threads_left_alive = 0
    if timeout_s >= 1.0:
        slow_backend = _StressBackend([None], stop_blocked=True)
        slow_source = source_for("non-exit-child", slow_backend)
        try:
            slow_source.start()
            slow_backend.read_entered.wait(timeout_s)
            slow_source.stop()
            slow_status = slow_source.status()
            non_exit_residual_observed = (
                slow_backend.stop_entered.is_set() and not slow_backend.stop_returned.is_set()
            )
            non_exit_fatal_cleanup = (
                non_exit_residual_observed
                and slow_status.lifecycle == "error"
                and slow_status.error is not None
            )
            slow_backend.release_stop.set()
            non_exit_cleanup_cleared = slow_backend.stop_returned.wait(timeout_s)
            _wait_for(
                lambda: not (
                    slow_source.status().thread_alive
                    or slow_source.status().start_thread_alive
                    or slow_source.status().backend_stop_thread_alive
                    or slow_source.status().drain_thread_alive
                ),
                timeout_s,
            )
            slow_status = slow_source.status()
            non_exit_threads_left_alive = slow_status.residual_worker_count
        except Exception:
            failures.append("non_exit_child_exception")
            with suppress(Exception):
                slow_backend.release_stop.set()
    else:
        non_exit_mode = "bounded_fake_child"
        child_release = threading.Event()
        child_done = threading.Event()

        def child() -> None:
            child_release.wait()
            child_done.set()

        child_thread = threading.Thread(
            target=child, name="capture-pressure-fake-child", daemon=True
        )
        child_thread.start()
        non_exit_residual_observed = child_thread.is_alive()
        child_release.set()
        child_thread.join(timeout_s)
        non_exit_cleanup_cleared = child_done.is_set() and not child_thread.is_alive()
        non_exit_fatal_cleanup = non_exit_residual_observed and non_exit_cleanup_cleared
        non_exit_threads_left_alive = int(child_thread.is_alive())

    checks = {
        "normal_start": normal_start,
        "normal_read": normal_read,
        "normal_stop": normal_stop,
        "normal_final_drain": normal_final_drain,
        "reset_new_session": reset_new_session,
        "blocked_start": blocked_start,
        "blocked_start_cleanup": blocked_start_cleanup,
        "blocked_read": blocked_read,
        "eof_fatal_latch": eof_started and eof_latched and eof_cleanup,
        "read_fatal_latch": read_started and read_latched and read_cleanup,
        "format_drift_fatal_latch": drift_started and drift_latched and drift_cleanup,
        "timestamp_rollback_fatal_latch": rollback_started
        and rollback_latched
        and rollback_cleanup,
        "start_failure_latch": start_error_started and start_error_latched and start_error_cleanup,
        "identity_drift_latch": identity_started and identity_latched and identity_cleanup,
        "non_exit_fatal_cleanup": non_exit_fatal_cleanup,
        "non_exit_residual_observed": non_exit_residual_observed,
        "non_exit_cleanup_cleared": non_exit_cleanup_cleared,
    }
    for name, passed in checks.items():
        if not passed:
            failures.append(f"fake_{name}")
    threads_left_alive = int(start_thread.is_alive()) + non_exit_threads_left_alive
    return {
        "normal_start": normal_start,
        "normal_read": normal_read,
        "normal_stop": normal_stop,
        "normal_final_drain": normal_final_drain,
        "reset_new_session": reset_new_session,
        "blocked_start": blocked_start,
        "blocked_start_cleanup": blocked_start_cleanup,
        "blocked_read": blocked_read,
        "eof_fatal_latch": checks["eof_fatal_latch"],
        "read_fatal_latch": checks["read_fatal_latch"],
        "format_drift_fatal_latch": checks["format_drift_fatal_latch"],
        "timestamp_rollback_fatal_latch": checks["timestamp_rollback_fatal_latch"],
        "start_failure_latch": checks["start_failure_latch"],
        "identity_drift_latch": checks["identity_drift_latch"],
        "non_exit_mode": non_exit_mode,
        "non_exit_fatal_cleanup": non_exit_fatal_cleanup,
        "non_exit_residual_observed": non_exit_residual_observed,
        "non_exit_cleanup_cleared": non_exit_cleanup_cleared,
        "threads_left_alive": threads_left_alive,
        "failures": sorted(set(failures)),
        "status": "PASS" if not failures else "FAIL",
    }


def _git_head(repo_root: Path | None = None) -> str:
    root = Path.cwd() if repo_root is None else repo_root
    try:
        value = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return _ZERO_COMMIT
    try:
        return _ensure_commit(value)
    except CapturePressureError:
        return _ZERO_COMMIT


@dataclass(frozen=True, slots=True)
class CapturePressureConfig:
    """Configuration for one deterministic pressure report.

    ``enforce_minimums`` is opt-in so small CI smoke runs can use the same
    implementation.  Full evidence uses the defaults and therefore has at
    least 100,000 publish operations and 1,000 lifecycle races.
    """

    publish_take_operations: int = MIN_PUBLISH_TAKE_OPERATIONS
    lifecycle_races: int = MIN_LIFECYCLE_RACES
    repetitions: int = 3
    seed: int = DEFAULT_SEED
    timeout_s: float = DEFAULT_TIMEOUT_S
    enforce_minimums: bool = False

    def __post_init__(self) -> None:
        _ensure_positive_int(self.publish_take_operations, "publish_take_operations")
        _ensure_positive_int(self.lifecycle_races, "lifecycle_races")
        if type(self.repetitions) is not int or self.repetitions < 3:
            raise CapturePressureError("repetitions must be at least 3")
        _ensure_non_negative_int(self.seed, "seed")
        _ensure_positive_wait_timeout(self.timeout_s, "timeout_s")
        if type(self.enforce_minimums) is not bool:
            raise CapturePressureError("enforce_minimums must be a boolean")
        if self.enforce_minimums and not self.minimums_met:
            raise CapturePressureError(
                "full pressure evidence requires at least 100000 publish operations "
                "and 1000 lifecycle races"
            )

    @property
    def minimums_met(self) -> bool:
        return (
            self.publish_take_operations >= MIN_PUBLISH_TAKE_OPERATIONS
            and self.lifecycle_races >= MIN_LIFECYCLE_RACES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_version": SCHEDULE_VERSION,
            "seed": self.seed,
            "publish_take_operations": self.publish_take_operations,
            "lifecycle_races": self.lifecycle_races,
            "repetitions": self.repetitions,
            "timeout_s": float(self.timeout_s),
            "enforce_minimums": self.enforce_minimums,
        }


@dataclass(frozen=True, slots=True)
class _StressSample:
    """Immutable synthetic raw sample used to detect torn metadata/bytes."""

    session_id: str
    sequence: int
    marker: tuple[str, int, int, int]
    payload: bytes
    content_hash: str

    @classmethod
    def make(cls, session_id: str, sequence: int, seed: int) -> _StressSample:
        marker = (session_id, sequence, seed, sequence ^ seed)
        payload = f"{session_id}|{sequence}|{seed}|{sequence ^ seed}".encode("ascii")
        content_hash = _sha256_bytes(payload)
        return cls(
            session_id=session_id,
            sequence=sequence,
            marker=marker,
            payload=payload,
            content_hash=content_hash,
        )

    def is_intact(self) -> bool:
        expected_marker = (
            self.session_id,
            self.sequence,
            self.marker[2],
            self.sequence ^ self.marker[2],
        )
        expected_payload = (
            f"{self.session_id}|{self.sequence}|{self.marker[2]}|{self.sequence ^ self.marker[2]}"
        ).encode("ascii")
        return (
            self.marker == expected_marker
            and self.payload == expected_payload
            and self.content_hash == _sha256_bytes(self.payload)
        )


@dataclass(frozen=True, slots=True)
class _PublishOutcome:
    status: str
    sample: _StressSample | None = None


class _SessionSlot:
    """Result-code adapter around a session-bound production raw slot.

    The production slot owns synchronization, lifecycle transitions, session
    binding, and stale-sample rejection.  This class only turns expected
    ``RuntimeError`` outcomes into stable stress result codes; it never holds
    a lock while calling the slot, so producer/consumer/controller operations
    genuinely overlap.
    """

    def __init__(self, session_id: str) -> None:
        self.slot: RawLatestSlot[_StressSample] = RawLatestSlot(session_id=session_id)
        self._state_lock = threading.Lock()
        self._session_id = session_id
        self._stopped = False

    def start(self) -> None:
        with self._state_lock:
            session_id = self._session_id
        self.slot.start(session_id=session_id)
        with self._state_lock:
            self._stopped = False

    @property
    def session_id(self) -> str:
        with self._state_lock:
            return self._session_id

    def publish(self, sample: _StressSample) -> _PublishOutcome:
        with self._state_lock:
            session_id = self._session_id
            stopped = self._stopped
        if sample.session_id != session_id:
            return _PublishOutcome("old_session")
        if stopped:
            return _PublishOutcome("stopped")
        try:
            self.slot.publish(sample)
        except RuntimeError:
            # The production slot is authoritative when reset/stop wins the
            # race between the state check and publish.  Re-read both sources
            # only to classify the expected stable result code.
            with self._state_lock:
                current_session = self._session_id
                currently_stopped = self._stopped
            slot_status = self.slot.status()
            if currently_stopped or self.slot.closed:
                return _PublishOutcome("stopped")
            if sample.session_id != current_session or slot_status.session_id != sample.session_id:
                return _PublishOutcome("old_session")
            raise
        return _PublishOutcome("published", sample)

    def take(self, **kwargs: Any) -> _StressSample | None:
        return self.slot.take(**kwargs)

    def reset(self, session_id: str) -> Any:
        sealed = self.slot.reset(new_session_id=session_id)
        with self._state_lock:
            self._session_id = session_id
            self._stopped = False
        return sealed

    def stop(self) -> str:
        with self._state_lock:
            if self._stopped:
                return "already_stopped"
            self._stopped = True
        self.slot.stop()
        return "stopped"

    def status(self) -> Any:
        return self.slot.status()


def _check_status(status: Any, *, require_max_depth: bool = True) -> list[str]:
    failures: list[str] = []
    if status.pending not in (0, 1):
        failures.append("pending_not_binary")
    if getattr(status, "in_flight", 0) not in (0, 1):
        failures.append("in_flight_not_binary")
    if getattr(status, "discarded_on_error", 0) < 0:
        failures.append("discarded_on_error_negative")
    if require_max_depth and status.max_depth != 1:
        failures.append("max_depth_not_one")
    accounted = (
        status.delivered
        + status.superseded
        + status.pending
        + getattr(status, "in_flight", 0)
        + status.discarded_on_reset
        + getattr(status, "discarded_on_error", 0)
    )
    if status.produced != accounted or status.produced != status.accounted:
        failures.append("counter_equation")
    return failures


def _lightweight_take_first(seed: int, sequence: int) -> bool:
    """Return the replayable interleaving decision for one publish sequence."""

    # A small integer mixer gives every seed a stable, inexpensive schedule
    # without materialising a 100k-entry command list.  Sequence one is
    # always take-first so every non-trivial run has a real blocked hand-off.
    if sequence == 1:
        return True
    mixed = (seed + sequence * 0x9E3779B9) & 0xFFFFFFFF
    mixed = ((mixed ^ (mixed >> 16)) * 0x85EBCA6B) & 0xFFFFFFFF
    mixed = ((mixed ^ (mixed >> 13)) * 0xC2B2AE35) & 0xFFFFFFFF
    mixed ^= mixed >> 16
    return bool(mixed & 1)


def _expected_lightweight_overlap_count(operation_count: int, seed: int) -> int:
    return sum(
        _lightweight_take_first(seed, sequence)
        for sequence in range(1, max(0, operation_count) + 1)
    )


def _bounded_overall_timeout(operation_count: int, operation_timeout: float) -> float:
    """Derive the schedule deadline without exceeding platform wait limits."""

    maximum = float(threading.TIMEOUT_MAX)
    budget_limit = int(maximum * 1_000.0)
    operation_budget = maximum if operation_count >= budget_limit else operation_count / 1_000.0
    return min(maximum, max(5.0, operation_timeout * 4.0 + operation_budget))


def _run_lightweight_pressure(
    operation_count: int,
    seed: int,
    memory_points: dict[str, int | None] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Run a deterministic four-role high-volume latest-slot schedule."""

    session_id = "stress-lightweight"
    slot: RawLatestSlot[_StressSample] = RawLatestSlot(session_id=session_id)
    slot.start(session_id=session_id)
    abort = threading.Event()
    metrics_stop = threading.Event()
    ready = threading.Barrier(4)
    controller_done = threading.Event()
    worker_errors: list[BaseException] = []
    error_lock = threading.Lock()
    # One reusable condition coordinates a deterministic operation schedule.
    # Each sequence is either take-first (a blocked consumer hand-off) or
    # publish-first.  No per-sample events, command objects, or tail list are
    # allocated, so the memory probe measures the live slot rather than the
    # harness retaining 100k synthetic samples.
    schedule = threading.Condition()
    active_sequence = 0
    published_sequence = 0
    completed_sequence = 0
    consumer_active_sequence = 0
    producer_done = threading.Event()
    consumer_done = threading.Event()
    event_hash = sha256()
    event_lock = threading.Lock()
    overlap_operation_count = 0
    operation_timeout = max(0.05, float(timeout_s))
    # The deterministic handshake performs one condition round-trip per
    # operation.  Budget proportional time for the full 100k evidence run;
    # using a fixed short deadline would turn scheduler pressure into a false
    # worker failure.
    overall_timeout = _bounded_overall_timeout(operation_count, operation_timeout)
    publish_count = 0
    take_count = 0
    sequence_monotonic = True
    last_delivered_sequence: int | None = None
    torn_samples = 0

    def record_error(error: BaseException) -> None:
        with error_lock:
            worker_errors.append(error)
        abort.set()
        with schedule:
            schedule.notify_all()

    def wait_ready() -> None:
        try:
            ready.wait(timeout=overall_timeout)
        except BaseException as exc:
            record_error(exc)

    def metrics_worker() -> None:
        wait_ready()
        while not metrics_stop.is_set() and not abort.is_set():
            try:
                snapshot = slot.status()
                if (
                    snapshot.pending not in (0, 1)
                    or getattr(snapshot, "in_flight", 0) not in (0, 1)
                    or snapshot.max_depth > 1
                ):
                    record_error(RuntimeError("raw latest metrics invariant failed"))
            except BaseException as exc:
                record_error(exc)
            metrics_stop.wait(0.0001)

    def producer_worker() -> None:
        nonlocal active_sequence, published_sequence
        nonlocal publish_count, overlap_operation_count
        try:
            wait_ready()
            for sequence in range(1, operation_count + 1):
                if abort.is_set():
                    return
                take_first = _lightweight_take_first(seed, sequence)
                with schedule:
                    active_sequence = sequence
                    schedule.notify_all()
                    deadline = monotonic() + overall_timeout
                    while (
                        take_first and consumer_active_sequence != sequence and not abort.is_set()
                    ):
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            record_error(TimeoutError("lightweight consumer did not enter"))
                            return
                        schedule.wait(min(remaining, 0.050))
                mode = "take-first" if take_first else "publish-first"
                with event_lock:
                    event_hash.update(f"publish:{sequence}:{mode}\n".encode("ascii"))
                sample = _StressSample.make(session_id, sequence, seed)
                status = slot.publish(sample)
                if isinstance(status, RawLatestStatus):
                    publish_count += 1
                else:
                    record_error(RuntimeError("lightweight producer returned no status"))
                    return
                with schedule:
                    published_sequence = sequence
                    if take_first and consumer_active_sequence == sequence:
                        overlap_operation_count += 1
                    schedule.notify_all()
                    deadline = monotonic() + overall_timeout
                    while completed_sequence != sequence and not abort.is_set():
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            record_error(TimeoutError("lightweight consumer did not complete"))
                            return
                        schedule.wait(min(remaining, 0.050))
                if memory_points is not None and sequence == operation_count // 2:
                    _capture_memory_point(memory_points, "mid")
        except BaseException as exc:
            record_error(exc)
        finally:
            producer_done.set()

    def consumer_worker() -> None:
        nonlocal completed_sequence, consumer_active_sequence, sequence_monotonic
        nonlocal last_delivered_sequence, take_count, torn_samples
        try:
            wait_ready()
            for sequence in range(1, operation_count + 1):
                take_first = _lightweight_take_first(seed, sequence)
                with schedule:
                    deadline = monotonic() + overall_timeout
                    while active_sequence != sequence and not abort.is_set():
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            record_error(TimeoutError("lightweight schedule did not advance"))
                            return
                        schedule.wait(min(remaining, 0.050))
                    if abort.is_set():
                        return
                    if take_first:
                        consumer_active_sequence = sequence
                        schedule.notify_all()
                    else:
                        while published_sequence != sequence and not abort.is_set():
                            remaining = deadline - monotonic()
                            if remaining <= 0:
                                record_error(TimeoutError("lightweight publish did not arrive"))
                                return
                            schedule.wait(min(remaining, 0.050))
                        if abort.is_set():
                            return
                        consumer_active_sequence = sequence
                observed = slot.take(timeout=operation_timeout)
                take_count += 1
                mode = "take-first" if take_first else "publish-first"
                if isinstance(observed, _StressSample):
                    if (
                        not observed.is_intact()
                        or observed.session_id != session_id
                        or (
                            last_delivered_sequence is not None
                            and observed.sequence <= last_delivered_sequence
                        )
                    ):
                        torn_samples += 1
                        sequence_monotonic = False
                    last_delivered_sequence = observed.sequence
                    with event_lock:
                        event_hash.update(
                            f"take:{observed.session_id}:{observed.sequence}:{mode}\n".encode(
                                "ascii"
                            )
                        )
                else:
                    sequence_monotonic = False
                    with event_lock:
                        event_hash.update(f"take:none:{mode}\n".encode("ascii"))
                with schedule:
                    if consumer_active_sequence == sequence:
                        consumer_active_sequence = 0
                    completed_sequence = sequence
                    schedule.notify_all()
                if memory_points is not None and sequence == operation_count // 2:
                    _capture_memory_point(memory_points, "mid")
        except BaseException as exc:
            record_error(exc)
        finally:
            consumer_done.set()

    def controller_worker() -> None:
        try:
            wait_ready()
            if not producer_done.wait(overall_timeout):
                record_error(TimeoutError("lightweight producer schedule deadline expired"))
                return
            if not consumer_done.wait(overall_timeout):
                record_error(TimeoutError("lightweight consumer schedule deadline expired"))
                return
            controller_done.set()
        except BaseException as exc:
            record_error(exc)
            controller_done.set()

    threads = [
        threading.Thread(
            target=producer_worker,
            name="capture-pressure-lightweight-producer",
            daemon=True,
        ),
        threading.Thread(
            target=consumer_worker,
            name="capture-pressure-lightweight-consumer",
            daemon=True,
        ),
        threading.Thread(
            target=metrics_worker,
            name="capture-pressure-lightweight-metrics",
            daemon=True,
        ),
        threading.Thread(
            target=controller_worker,
            name="capture-pressure-lightweight-controller",
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    deadlocked = not controller_done.wait(overall_timeout)
    if deadlocked:
        record_error(TimeoutError("lightweight controller deadline expired"))
    abort.set()
    metrics_stop.set()
    with schedule:
        schedule.notify_all()
    for thread in threads:
        thread.join(timeout=operation_timeout)
    threads_left_alive = sum(thread.is_alive() for thread in threads)
    if threads_left_alive:
        record_error(RuntimeError("lightweight worker thread did not exit"))
    # Capture the end/peak point before stopping the live slot and before this
    # function releases its harness objects.  The report therefore compares
    # one live slot's before/mid/end evidence instead of an after-GC snapshot.
    if memory_points is not None and "after_tracemalloc_current" not in memory_points:
        _capture_memory_point(memory_points, "after")
    try:
        slot.stop()
    except BaseException as exc:
        record_error(exc)

    with event_lock:
        event_digest = event_hash.hexdigest()
    status = slot.status()
    final_sequence = last_delivered_sequence
    failures = _check_status(status)
    if len(worker_errors) or deadlocked or threads_left_alive:
        failures.append("concurrent_worker_error")
    if publish_count != operation_count:
        failures.append("concurrent_publish_count")
    if take_count != operation_count:
        failures.append("concurrent_take_count")
    if publish_count + take_count != operation_count * 2:
        failures.append("concurrent_total_count")
    expected_overlap = _expected_lightweight_overlap_count(operation_count, seed)
    if overlap_operation_count != expected_overlap:
        failures.append("schedule_overlap_count")
    if not sequence_monotonic:
        failures.append("delivered_sequence_not_monotonic")
    if final_sequence != operation_count:
        failures.append("final_drain_not_last_produced")
    if torn_samples:
        failures.append("torn_tuple")
    if status.last_produced_sequence != operation_count:
        failures.append("slot_last_produced_sequence")
    if status.last_delivered_sequence != operation_count:
        failures.append("slot_last_delivered_sequence")
    if memory_points is not None and "mid_tracemalloc_current" not in memory_points:
        _capture_memory_point(memory_points, "mid")
    return {
        "publish_operations": publish_count,
        "take_operations": take_count,
        "total_operations": publish_count + take_count,
        "concurrent_publish_operations": overlap_operation_count,
        "concurrent_take_operations": overlap_operation_count,
        "concurrent_publish_take_operations": overlap_operation_count * 2,
        "overlap_operation_count": overlap_operation_count,
        "concurrent_roles": 4,
        "event_ledger_algorithm": "sha256-lines-v1",
        "event_ledger_count": publish_count + take_count,
        "event_ledger_digest": event_digest,
        "produced": status.produced,
        "delivered": status.delivered,
        "superseded": status.superseded,
        "pending": status.pending,
        "in_flight": getattr(status, "in_flight", 0),
        "discarded_on_reset": status.discarded_on_reset,
        "discarded_on_error": getattr(status, "discarded_on_error", 0),
        "max_depth": status.max_depth,
        "accounting_holds": status.accounting_holds,
        "pending_binary": status.pending in (0, 1),
        "sequence_monotonic": sequence_monotonic,
        "final_drain_sequence": final_sequence,
        "last_produced_sequence": operation_count,
        "final_drain_is_last_produced": final_sequence == operation_count,
        "no_torn_tuple": torn_samples == 0,
        "threads_left_alive": threads_left_alive,
        "failures": sorted(set(failures)),
        "status": "PASS" if not failures else "FAIL",
    }


def _run_one_lifecycle_race(race_index: int, seed: int, timeout_s: float) -> dict[str, int]:
    """Compatibility helper that runs one race with the real four-role runner."""

    runner = _PersistentLifecycleRunner(seed, timeout_s)
    try:
        return runner.run(race_index)
    finally:
        # Cleanup belongs to the production slot exercise, not to a wrapper
        # thread that can hide a leaked worker.
        runner.close()


@dataclass(slots=True)
class _CommandState:
    result: list[Any]
    errors: list[BaseException]
    done: threading.Event


class _PersistentLifecycleRunner:
    """Reuse fixed producer/consumer/controller threads across all races."""

    def __init__(self, seed: int, timeout_s: float) -> None:
        self.seed = seed
        self.timeout_s = timeout_s
        self._producer_commands: queue.Queue[Any] = queue.Queue()
        self._consumer_commands: queue.Queue[Any] = queue.Queue()
        self._controller_commands: queue.Queue[Any] = queue.Queue()
        self._observer_stop = threading.Event()
        self._observer_active = threading.Event()
        self._observer_lock = threading.Lock()
        self._observer_harness: _SessionSlot | None = None
        self._observer_race_index = -1
        self._observer_failures: dict[int, int] = {}
        self._observer_errors: dict[int, int] = {}
        self._threads = [
            threading.Thread(
                target=self._worker,
                args=(self._producer_commands,),
                name="capture-pressure-persistent-producer",
                daemon=True,
            ),
            threading.Thread(
                target=self._worker,
                args=(self._consumer_commands,),
                name="capture-pressure-persistent-consumer",
                daemon=True,
            ),
            threading.Thread(
                target=self._worker,
                args=(self._controller_commands,),
                name="capture-pressure-persistent-controller",
                daemon=True,
            ),
            threading.Thread(
                target=self._metrics_worker,
                name="capture-pressure-persistent-metrics",
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def _worker(self, commands: queue.Queue[Any]) -> None:
        while True:
            command = commands.get()
            if command is None:
                return
            function, barrier, state = command
            try:
                if barrier is not None:
                    barrier.wait(timeout=self.timeout_s)
                state.result.append(function())
            except BaseException as exc:
                state.errors.append(exc)
            finally:
                state.done.set()

    def _metrics_worker(self) -> None:
        while not self._observer_stop.is_set():
            if self._observer_active.is_set():
                with self._observer_lock:
                    harness = self._observer_harness
                    race_index = self._observer_race_index
                if harness is not None:
                    try:
                        snapshot = harness.status()
                        if snapshot.pending not in (0, 1) or not snapshot.accounting_holds:
                            self._observer_failures[race_index] = (
                                self._observer_failures.get(race_index, 0) + 1
                            )
                    except BaseException:
                        self._observer_errors[race_index] = (
                            self._observer_errors.get(race_index, 0) + 1
                        )
            self._observer_stop.wait(0.0001)

    def _submit(
        self,
        commands: queue.Queue[Any],
        function: Callable[[], Any],
        barrier: threading.Barrier | None = None,
    ) -> _CommandState:
        state = _CommandState(result=[], errors=[], done=threading.Event())
        commands.put((function, barrier, state))
        return state

    def _wait(self, state: _CommandState) -> bool:
        return state.done.wait(timeout=self.timeout_s)

    def _barrier_start(self, barrier: threading.Barrier, errors: list[BaseException]) -> bool:
        try:
            barrier.wait(timeout=self.timeout_s)
        except BaseException as exc:
            errors.append(exc)
            return False
        return True

    @staticmethod
    def _new_failures() -> dict[str, int]:
        return {
            "accounting_failures": 0,
            "pending_violations": 0,
            "in_flight_violations": 0,
            "discarded_on_error_violations": 0,
            "max_depth_violations": 0,
            "sequence_violations": 0,
            "torn_tuple_violations": 0,
            "cross_session_leaks": 0,
            "deadlocks": 0,
            "unhandled_exceptions": 0,
            "final_drain_failures": 0,
            "stop_idempotence_failures": 0,
            "reset_accounting_failures": 0,
            "metrics_observer_failures": 0,
            "publish_after_stop_failures": 0,
            "restart_after_stop_failures": 0,
            "underlying_session_rejection_failures": 0,
            "blocked_read_failures": 0,
        }

    def _record_state(
        self, state: _CommandState, failures: dict[str, int], *, required_result: bool = True
    ) -> None:
        if not self._wait(state):
            failures["deadlocks"] += 1
        if state.errors:
            failures["unhandled_exceptions"] += len(state.errors)
        if required_result and not state.result:
            failures["unhandled_exceptions"] += 1

    def run(self, race_index: int) -> dict[str, int]:
        failures = self._new_failures()
        old_session = f"stress-session-{race_index}-a"
        new_session = f"stress-session-{race_index}-b"
        harness = _SessionSlot(old_session)
        harness.start()
        old_first = _StressSample.make(old_session, 1, self.seed)
        old_second = _StressSample.make(old_session, 2, self.seed)
        new_first = _StressSample.make(new_session, 1, self.seed)
        new_second = _StressSample.make(new_session, 2, self.seed)
        delivered_by_session: dict[str, list[int]] = {old_session: [], new_session: []}
        accepted_new: list[_StressSample] = []
        main_errors: list[BaseException] = []
        with self._observer_lock:
            self._observer_harness = harness
            self._observer_race_index = race_index
        self._observer_active.set()

        # First establish a genuinely blocked read on the persistent consumer
        # thread, then wake it through the real slot publish.  The consumer
        # remains the sole logical consumer for the final drain.
        blocked_started = threading.Event()

        def blocked_take() -> _StressSample | None:
            blocked_started.set()
            return harness.take(timeout=self.timeout_s)

        consumer_state = self._submit(self._consumer_commands, blocked_take)
        if not blocked_started.wait(timeout=self.timeout_s):
            failures["blocked_read_failures"] += 1
            failures["deadlocks"] += 1
        producer_state = self._submit(self._producer_commands, lambda: harness.publish(old_first))
        self._record_state(producer_state, failures)
        self._record_state(consumer_state, failures)
        initial_sample = consumer_state.result[0] if consumer_state.result else None
        if isinstance(initial_sample, _StressSample):
            delivered_by_session[old_session].append(initial_sample.sequence)
            if not initial_sample.is_intact():
                failures["torn_tuple_violations"] += 1
        elif initial_sample is None:
            failures["blocked_read_failures"] += 1
        else:
            failures["torn_tuple_violations"] += 1

        # Reset races the same persistent producer.  Either ordering is
        # valid: an old sample is rejected after reset, or it is accounted for
        # in the sealed epoch before reset clears the pending sample.
        reset_barrier = threading.Barrier(3)
        reset_producer = self._submit(
            self._producer_commands, lambda: harness.publish(old_second), reset_barrier
        )
        reset_controller = self._submit(
            self._controller_commands, lambda: harness.reset(new_session), reset_barrier
        )
        self._barrier_start(reset_barrier, main_errors)
        self._record_state(reset_producer, failures)
        self._record_state(reset_controller, failures)
        sealed = reset_controller.result[0] if reset_controller.result else None
        if sealed is None:
            failures["reset_accounting_failures"] += 1
        else:
            failures["accounting_failures"] += len(_check_status(sealed, require_max_depth=False))
            if sealed.pending not in (0, 1):
                failures["pending_violations"] += 1

        old_after_state = self._submit(self._producer_commands, lambda: harness.publish(old_second))
        self._record_state(old_after_state, failures)
        old_after = old_after_state.result[0] if old_after_state.result else None
        if not isinstance(old_after, _PublishOutcome) or old_after.status != "old_session":
            failures["cross_session_leaks"] += 1

        def direct_old_publish_rejected() -> bool:
            try:
                harness.slot.publish(old_second)
            except RuntimeError:
                return True
            return False

        direct_old_state = self._submit(self._producer_commands, direct_old_publish_rejected)
        self._record_state(direct_old_state, failures)
        if not direct_old_state.result or direct_old_state.result[0] is not True:
            failures["underlying_session_rejection_failures"] += 1

        current_state = self._submit(self._producer_commands, lambda: harness.publish(new_first))
        self._record_state(current_state, failures)
        current = current_state.result[0] if current_state.result else None
        if not isinstance(current, _PublishOutcome) or current.status != "published":
            failures["final_drain_failures"] += 1
        elif current.sample is not None:
            accepted_new.append(current.sample)

        stop_barrier = threading.Barrier(3)
        stop_producer = self._submit(
            self._producer_commands, lambda: harness.publish(new_second), stop_barrier
        )
        stop_controller = self._submit(self._controller_commands, harness.stop, stop_barrier)
        self._barrier_start(stop_barrier, main_errors)
        self._record_state(stop_producer, failures)
        self._record_state(stop_controller, failures)
        race_publish = stop_producer.result[0] if stop_producer.result else None
        if (
            isinstance(race_publish, _PublishOutcome)
            and race_publish.status == "published"
            and race_publish.sample is not None
        ):
            accepted_new.append(race_publish.sample)
        if harness.stop() != "already_stopped":
            failures["stop_idempotence_failures"] += 1

        def start_after_stop_rejected() -> str:
            try:
                harness.start()
            except RuntimeError as exc:
                return "requires_reset" if "requires reset" in str(exc) else "other_error"
            return "reopened"

        restart_state = self._submit(self._controller_commands, start_after_stop_rejected)
        self._record_state(restart_state, failures)
        if not restart_state.result or restart_state.result[0] != "requires_reset":
            failures["restart_after_stop_failures"] += 1

        final_state = self._submit(self._consumer_commands, harness.take)
        self._record_state(final_state, failures)
        final_sample = final_state.result[0] if final_state.result else None
        if isinstance(final_sample, _StressSample):
            delivered_by_session[new_session].append(final_sample.sequence)
            if not final_sample.is_intact() or final_sample.session_id != new_session:
                failures["torn_tuple_violations"] += 1
                failures["cross_session_leaks"] += 1
            if not accepted_new or final_sample.sequence != max(
                sample.sequence for sample in accepted_new
            ):
                failures["final_drain_failures"] += 1
        else:
            failures["final_drain_failures"] += 1

        stopped_state = self._submit(self._producer_commands, lambda: harness.publish(new_second))
        self._record_state(stopped_state, failures)
        stopped = stopped_state.result[0] if stopped_state.result else None
        if not isinstance(stopped, _PublishOutcome) or stopped.status != "stopped":
            failures["publish_after_stop_failures"] += 1

        def direct_stopped_publish_rejected() -> bool:
            try:
                harness.slot.publish(new_second)
            except RuntimeError:
                return True
            return False

        direct_stopped_state = self._submit(
            self._producer_commands, direct_stopped_publish_rejected
        )
        self._record_state(direct_stopped_state, failures)
        if not direct_stopped_state.result or direct_stopped_state.result[0] is not True:
            failures["publish_after_stop_failures"] += 1

        for _session_id, sequences in delivered_by_session.items():
            if any(left >= right for left, right in pairwise(sequences)):
                failures["sequence_violations"] += 1
            if any(not isinstance(value, int) or value <= 0 for value in sequences):
                failures["sequence_violations"] += 1
        if main_errors:
            failures["unhandled_exceptions"] += len(main_errors)
        status = harness.status()
        failures["accounting_failures"] += len(_check_status(status))
        if status.pending not in (0, 1):
            failures["pending_violations"] += 1
        if getattr(status, "in_flight", 0) not in (0, 1):
            failures["in_flight_violations"] += 1
        if getattr(status, "discarded_on_error", 0) < 0:
            failures["discarded_on_error_violations"] += 1
        if status.max_depth != 1:
            failures["max_depth_violations"] += 1
        self._observer_active.clear()
        with self._observer_lock:
            failures["metrics_observer_failures"] += self._observer_failures.pop(race_index, 0)
            failures["metrics_observer_failures"] += self._observer_errors.pop(race_index, 0)
        return failures

    def close(self) -> int:
        self._observer_active.clear()
        self._observer_stop.set()
        self._threads[3].join(timeout=self.timeout_s)
        deadlocks = int(self._threads[3].is_alive())
        for commands in (
            self._producer_commands,
            self._consumer_commands,
            self._controller_commands,
        ):
            commands.put(None)
        for thread in self._threads[:3]:
            thread.join(timeout=self.timeout_s)
            deadlocks += int(thread.is_alive())
        return deadlocks


def _run_vc003_backend_races(race_count: int, timeout_s: float) -> dict[str, Any]:
    """Run the backend-facing start/read/reset/stop matrix without hardware."""

    backends: list[_StressBackend] = []

    def factory(_config: VC003SourceConfig) -> _StressBackend:
        backend = _StressBackend([None], block_start=True, block_read=True)
        backends.append(backend)
        return backend

    source = VC003Source(
        VC003SourceConfig(
            source_id="capture-card-primary",
            session_id="capture-pressure-backend-0-a",
            device_name="VC-003 Video",
            width=2,
            height=1,
            fps=60.0,
            poll_interval_s=0.0001,
        ),
        backend_factory=factory,
    )
    failures = 0
    event_lines: list[bytes] = []
    start_checks = read_checks = reset_checks = stop_checks = session_checks = restart_checks = 0
    threads_left_alive = 0
    read_commands: queue.Queue[Any] = queue.Queue()

    def read_worker() -> None:
        while True:
            command = read_commands.get()
            if command is None:
                return
            done, results, errors = command
            try:
                results.append(source.read(timeout=timeout_s))
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

    reader_thread = threading.Thread(
        target=read_worker, name="capture-pressure-vc003-read", daemon=True
    )
    reader_thread.start()

    def start_blocked_source() -> tuple[bool, _StressBackend | None]:
        errors: list[BaseException] = []

        def call_start() -> None:
            try:
                source.start()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(
            target=call_start, name="capture-pressure-vc003-start", daemon=True
        )
        backend_count_before = len(backends)
        thread.start()
        if not _wait_for(lambda: len(backends) > backend_count_before, timeout_s):
            thread.join(timeout_s)
            return False, None
        backend = backends[-1]
        entered = backend.start_entered.wait(timeout_s)
        backend.release_start.set()
        thread.join(timeout_s)
        nonlocal threads_left_alive
        threads_left_alive += int(thread.is_alive())
        if errors:
            return False, backend
        return entered and not thread.is_alive() and backend.started.is_set(), backend

    def blocked_read_then_stop(backend: _StressBackend) -> bool:
        results: list[Any] = []
        errors: list[BaseException] = []
        done = threading.Event()
        read_commands.put((done, results, errors))
        entered = backend.read_entered.wait(timeout_s)
        source.stop()
        done.wait(timeout_s)
        return (
            entered
            and not errors
            and done.is_set()
            and results == [None]
            and backend.stopped.is_set()
        )

    try:
        for race_index in range(race_count):
            if race_index:
                next_session = f"capture-pressure-backend-{race_index}-a"
                source.reset(next_session)
                reset_checks += 1
            first_start_ok, backend = start_blocked_source()
            if backend is None or not first_start_ok:
                failures += 1
                event_lines.append(_canonical_json({"race": race_index, "start": False}))
                continue
            start_checks += 1
            first_read_ok = blocked_read_then_stop(backend)
            read_checks += int(first_read_ok)
            stop_checks += int(first_read_ok)
            old_session = source.session_id
            try:
                source.start()
            except RuntimeError as exc:
                restart_ok = "reset" in str(exc).lower() and "required" in str(exc).lower()
            else:
                restart_ok = False
                source.stop()
            restart_checks += int(restart_ok)
            new_session = f"capture-pressure-backend-{race_index}-b"
            try:
                source.reset(new_session)
                reset_checks += 1
                stale_spec = PixelSpec(width=2, height=1)
                stale_bytes = b"\x00" * stale_spec.length
                stale_digest = pixel_digest(stale_spec, stale_bytes)
                stale = VC003RawFrame(
                    source_id=source.config.source_id,
                    session_id=old_session,
                    frame_id=1,
                    captured_at_ns=0,
                    clock_domain=source.config.clock_domain,
                    transform_version=source.config.transform_version,
                    content_hash=stale_digest,
                    image_ref=f"cas://sha256/{stale_digest}",
                    source_size=FrameSize(width=2, height=1),
                    received_at_ns=0,
                    raw_bytes=stale_bytes,
                    spec=stale_spec,
                )
                try:
                    source.raw_latest_slot.publish(stale)
                except RuntimeError as exc:
                    stale_ok = "session" in str(exc).lower()
                else:
                    stale_ok = False
                session_checks += int(stale_ok)
            except Exception:
                stale_ok = False
                source.stop()
            second_start_ok, second_backend = start_blocked_source()
            start_checks += int(second_start_ok)
            second_read_ok = (
                blocked_read_then_stop(second_backend)
                if second_start_ok and second_backend is not None
                else False
            )
            read_checks += int(second_read_ok)
            stop_checks += int(second_read_ok)
            if not (
                first_read_ok and restart_ok and stale_ok and second_start_ok and second_read_ok
            ):
                failures += 1
            event_lines.append(
                _canonical_json(
                    {
                        "race": race_index,
                        "start": first_start_ok and second_start_ok,
                        "blocked_read": first_read_ok and second_read_ok,
                        "restart": restart_ok,
                        "session": stale_ok,
                        "reset": True,
                        "stop": first_read_ok and second_read_ok,
                    }
                )
            )
    finally:
        with suppress(Exception):
            source.stop()
        read_commands.put(None)
        reader_thread.join(timeout_s)
        threads_left_alive += int(reader_thread.is_alive())
        threads_left_alive += int(
            source.status().thread_alive
            or source.status().start_thread_alive
            or source.status().backend_stop_thread_alive
        )
    return {
        "races": race_count,
        "start_checks": start_checks,
        "blocked_read_checks": read_checks,
        "reset_checks": reset_checks,
        "stop_checks": stop_checks,
        "session_checks": session_checks,
        "restart_checks": restart_checks,
        "event_ledger_algorithm": "sha256-lines-v1",
        "event_ledger_count": len(event_lines),
        "event_ledger_digest": _sha256_bytes(b"\n".join(event_lines)),
        "threads_left_alive": threads_left_alive,
        "failures": failures,
        "status": "PASS" if failures == 0 and threads_left_alive == 0 else "FAIL",
    }


def _run_lifecycle_pressure(race_count: int, seed: int, timeout_s: float) -> dict[str, Any]:
    totals = _PersistentLifecycleRunner._new_failures()
    runner = _PersistentLifecycleRunner(seed, timeout_s)
    event_lines: list[bytes] = []
    backend_matrix: dict[str, Any] | None = None
    try:
        for race_index in range(race_count):
            observed = runner.run(race_index)
            event_lines.append(_canonical_json({"race_index": race_index, "results": observed}))
            for key in totals:
                totals[key] += observed[key]
    finally:
        close_deadlocks = runner.close()
        totals["deadlocks"] += close_deadlocks
        event_lines.append(_canonical_json({"cleanup_deadlocks": close_deadlocks}))
    backend_matrix = _run_vc003_backend_races(race_count, timeout_s)
    totals["backend_lifecycle_failures"] = backend_matrix["failures"]
    if backend_matrix["threads_left_alive"]:
        totals["backend_lifecycle_failures"] += backend_matrix["threads_left_alive"]
    failures = [key for key, value in totals.items() if value]
    return {
        "races": race_count,
        "epochs_checked": race_count * 2,
        "reset_count": race_count,
        "stop_races": race_count,
        "session_checks": race_count,
        "blocked_read_checks": race_count,
        "restart_checks": race_count,
        "underlying_session_checks": race_count,
        "underlying_stop_checks": race_count,
        "final_drain_checks": race_count,
        "final_drain_performed": totals["final_drain_failures"] == 0,
        "final_drain_sequence": "last_produced",
        "concurrent_roles": 4,
        "event_ledger_algorithm": "sha256-lines-v1",
        "event_ledger_count": race_count + 1,
        "event_ledger_digest": _sha256_bytes(b"\n".join(event_lines)),
        "backend_races": backend_matrix["races"],
        "backend_start_checks": backend_matrix["start_checks"],
        "backend_blocked_read_checks": backend_matrix["blocked_read_checks"],
        "backend_reset_checks": backend_matrix["reset_checks"],
        "backend_stop_checks": backend_matrix["stop_checks"],
        "backend_session_checks": backend_matrix["session_checks"],
        "backend_restart_checks": backend_matrix["restart_checks"],
        "backend_event_ledger_algorithm": backend_matrix["event_ledger_algorithm"],
        "backend_event_ledger_count": backend_matrix["event_ledger_count"],
        "backend_event_ledger_digest": backend_matrix["event_ledger_digest"],
        "backend_threads_left_alive": backend_matrix["threads_left_alive"],
        "backend_status": backend_matrix["status"],
        **totals,
        "status": "PASS" if not failures else "FAIL",
    }


def _expected_lightweight_event_ledger_digest(
    operation_count: int,
    seed: int = DEFAULT_SEED,
) -> str:
    """Return the digest for the replayable per-operation interleaving schedule."""

    digest = sha256()
    for sequence in range(1, operation_count + 1):
        mode = "take-first" if _lightweight_take_first(seed, sequence) else "publish-first"
        digest.update(f"publish:{sequence}:{mode}\n".encode("ascii"))
        digest.update(f"take:stress-lightweight:{sequence}:{mode}\n".encode("ascii"))
    return digest.hexdigest()


def _expected_lifecycle_event_ledger_digest(race_count: int) -> str:
    """Return the digest for a zero-failure raw-slot lifecycle schedule."""

    empty_results = _PersistentLifecycleRunner._new_failures()
    event_lines = [
        _canonical_json({"race_index": race_index, "results": empty_results})
        for race_index in range(race_count)
    ]
    event_lines.append(_canonical_json({"cleanup_deadlocks": 0}))
    return _sha256_bytes(b"\n".join(event_lines))


def _expected_backend_event_ledger_digest(race_count: int) -> str:
    """Return the digest for a passing backend lifecycle matrix."""

    event_lines = [
        _canonical_json(
            {
                "race": race_index,
                "start": True,
                "blocked_read": True,
                "restart": True,
                "session": True,
                "reset": True,
                "stop": True,
            }
        )
        for race_index in range(race_count)
    ]
    return _sha256_bytes(b"\n".join(event_lines))


def _run_summary_payload(
    lightweight: Mapping[str, Any], lifecycle: Mapping[str, Any], failures: list[str]
) -> dict[str, Any]:
    return {
        "lightweight": dict(lightweight),
        "lifecycle": dict(lifecycle),
        "failures": list(failures),
        "status": "PASS" if not failures else "FAIL",
    }


def _run_once(config: CapturePressureConfig, run_index: int) -> dict[str, Any]:
    # Every repetition starts from the same seed and schedule.  ``run_index``
    # is metadata only and is not used to perturb the fixture.
    if tracemalloc.is_tracing():
        tracemalloc.stop()
    gc.collect()
    tracemalloc.start()
    memory_points: dict[str, int | None] = {}
    _capture_memory_point(memory_points, "before")
    try:
        lightweight = _run_lightweight_pressure(
            config.publish_take_operations,
            config.seed,
            memory_points,
            float(config.timeout_s),
        )
        if "mid_tracemalloc_current" not in memory_points:
            _capture_memory_point(memory_points, "mid")
        # The lightweight runner captures ``after`` while its slot and worker
        # harness are still live.  Keep this fallback for an exceptional or
        # zero-operation fixture, but do not replace the live end sample with
        # an after-GC measurement here.
        if "after_tracemalloc_current" not in memory_points:
            _capture_memory_point(memory_points, "after")
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
    memory = _memory_report(memory_points)
    pixel_cas = _run_full_size_pixel_cas()
    lifecycle = _run_lifecycle_pressure(
        config.lifecycle_races, config.seed, float(config.timeout_s)
    )
    vc003_fake_lifecycle = _run_vc003_fake_lifecycle(float(config.timeout_s))
    failures = sorted(
        set(cast(list[str], lightweight["failures"]))
        | {
            key
            for key, value in lifecycle.items()
            if key.endswith("failures")
            or key
            in {
                "deadlocks",
                "unhandled_exceptions",
                "cross_session_leaks",
                "in_flight_violations",
                "discarded_on_error_violations",
            }
            if isinstance(value, int) and value
        }
    )
    if not memory["passed"]:
        failures.append("memory_evidence")
    if pixel_cas["status"] != "PASS":
        failures.append("full_size_pixel_cas")
    if vc003_fake_lifecycle["status"] != "PASS":
        failures.append("vc003_fake_lifecycle")
    failures = sorted(set(failures))
    # The nested reports carry their own failure details.  Keep the canonical
    # summary independent of timing and thread scheduling.
    summary_payload = _run_summary_payload(lightweight, lifecycle, failures)
    summary_digest = _digest(summary_payload)
    run_payload = {
        "run_index": run_index,
        **summary_payload,
        "memory": memory,
        "pixel_cas": pixel_cas,
        "vc003_fake_lifecycle": vc003_fake_lifecycle,
    }
    return {
        **run_payload,
        "summary_digest": summary_digest,
        "event_digest": _digest(
            {
                "schedule_version": SCHEDULE_VERSION,
                "seed": config.seed,
                "publish_take_operations": config.publish_take_operations,
                "lifecycle_races": config.lifecycle_races,
                "concurrent_roles": (
                    "producer",
                    "consumer",
                    "metrics_observer",
                    "reset_stop_controller",
                ),
                "lightweight_event_ledger": lightweight["event_ledger_digest"],
                "lifecycle_event_ledger": lifecycle["event_ledger_digest"],
                "backend_event_ledger": lifecycle["backend_event_ledger_digest"],
                "vc003_fake_lifecycle": _digest(vc003_fake_lifecycle),
                "full_size_fixture": "full_size_zero_bgr8_v1",
            }
        ),
        # ``run_index`` identifies the repetition but is not evidence.  The
        # memory probe is reported independently because process allocators
        # may quantize its baseline differently between repetitions.
        "run_digest": _digest(
            {key: value for key, value in run_payload.items() if key not in {"run_index", "memory"}}
        ),
    }


def _artifact_hash(path: Path, fallback: bytes) -> str:
    try:
        return _sha256_bytes(path.resolve().read_bytes())
    except OSError:
        return _sha256_bytes(fallback)


def _default_repo_root() -> Path:
    """Locate a source checkout without treating an installed package as one."""

    source_path = Path(__file__).resolve()
    source_locator = Path("src/maple_automation_core/capture/stress.py")
    required_markers = (
        Path("pyproject.toml"),
        Path("schemas/capture-pressure-report.schema.json"),
        Path("configs/requirements.lock"),
    )
    for candidate in source_path.parents:
        if (candidate / source_locator).resolve() != source_path:
            continue
        if all((candidate / marker).is_file() for marker in required_markers):
            return candidate
    raise CapturePressureError(
        "capture pressure default repo_root requires a source checkout; "
        "pass repo_root explicitly for installed packages"
    )


@dataclass(frozen=True, slots=True)
class CapturePressureReport:
    """Immutable convenience wrapper around a strict report mapping."""

    payload: Mapping[str, Any]

    @property
    def status(self) -> str:
        return cast(str, self.payload["status"])

    @property
    def deterministic(self) -> bool:
        return bool(self.payload["deterministic"])

    @property
    def report_digest(self) -> str:
        return cast(str, self.payload["canonical_report_sha256"])

    @property
    def canonical_report_sha256(self) -> str:
        return self.report_digest

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(json.dumps(self.payload, ensure_ascii=False)))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def write_json(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.to_json() + "\n", encoding="utf-8", newline="\n")
        return destination

    def assert_valid(self) -> None:
        verify_capture_pressure_report(self.payload)


def run_capture_pressure(
    config: CapturePressureConfig | None = None,
    *,
    publish_take_operations: int | None = None,
    lifecycle_races: int | None = None,
    repetitions: int | None = None,
    seed: int | None = None,
    timeout_s: float | None = None,
    enforce_minimums: bool | None = None,
    source_commit: str | None = None,
    repo_root: str | Path | None = None,
    generated_at: str | None = None,
) -> CapturePressureReport:
    """Run deterministic latest-slot pressure and build a strict report.

    Keyword overrides are provided for small CI runs.  At least three
    repetitions are always required for a deterministic result.
    """

    if config is not None and any(
        value is not None
        for value in (
            publish_take_operations,
            lifecycle_races,
            repetitions,
            seed,
            timeout_s,
            enforce_minimums,
        )
    ):
        raise CapturePressureError("config cannot be combined with keyword overrides")
    if config is None:
        config = CapturePressureConfig(
            publish_take_operations=(
                MIN_PUBLISH_TAKE_OPERATIONS
                if publish_take_operations is None
                else publish_take_operations
            ),
            lifecycle_races=MIN_LIFECYCLE_RACES if lifecycle_races is None else lifecycle_races,
            repetitions=3 if repetitions is None else repetitions,
            seed=DEFAULT_SEED if seed is None else seed,
            timeout_s=DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s,
            enforce_minimums=False if enforce_minimums is None else enforce_minimums,
        )
    root = Path(repo_root).resolve() if repo_root is not None else _default_repo_root()
    commit = _ensure_commit(source_commit) if source_commit is not None else _git_head(root)
    source_path = Path(__file__).resolve()
    schema_path = root / "schemas" / "capture-pressure-report.schema.json"
    tool_hash = _artifact_hash(source_path, b"maple-capture-pressure-source")
    schema_hash = _artifact_hash(schema_path, b"maple-capture-pressure-schema")
    artifacts = [
        {
            "artifact_id": "capture-pressure-source",
            "kind": "source",
            "sha256": tool_hash,
            "locator": "src/maple_automation_core/capture/stress.py",
        },
        {
            "artifact_id": "capture-pressure-schema",
            "kind": "schema",
            "sha256": schema_hash,
            "locator": "schemas/capture-pressure-report.schema.json",
        },
    ]
    artifact_list_sha256 = _digest(artifacts)
    runs = [_run_once(config, index) for index in range(1, config.repetitions + 1)]
    summary_digests = {run["summary_digest"] for run in runs}
    event_digests = {run["event_digest"] for run in runs}
    lightweight_event_ledgers = {run["lightweight"]["event_ledger_digest"] for run in runs}
    lifecycle_event_ledgers = {run["lifecycle"]["event_ledger_digest"] for run in runs}
    backend_event_ledgers = {run["lifecycle"]["backend_event_ledger_digest"] for run in runs}
    run_digests = {run["run_digest"] for run in runs}
    deterministic = (
        len(summary_digests) == 1
        and len(event_digests) == 1
        and len(lightweight_event_ledgers) == 1
        and len(lifecycle_event_ledgers) == 1
        and len(backend_event_ledgers) == 1
        and len(run_digests) == 1
    )
    run_failures = [failure for run in runs for failure in run["failures"]]
    failures = sorted(set(run_failures))
    if not deterministic:
        failures.append("repeated_summary_mismatch")
    failures = sorted(set(failures))
    deterministic_evidence_sha256 = _digest(
        {
            "run_digest": next(iter(run_digests), _ZERO_SHA256),
            "artifact_list_sha256": artifact_list_sha256,
        }
    )
    config_dict = config.to_dict()
    config_hash = _digest(config_dict)
    first_lightweight = runs[0]["lightweight"]
    first_lifecycle = runs[0]["lifecycle"]
    first_memory = runs[0]["memory"]
    first_pixel_cas = runs[0]["pixel_cas"]
    first_vc003_fake_lifecycle = runs[0]["vc003_fake_lifecycle"]
    all_invariants = {
        "accounting_holds": bool(first_lightweight["accounting_holds"])
        and first_lifecycle["accounting_failures"] == 0,
        "pending_binary": bool(first_lightweight["pending_binary"])
        and first_lifecycle["pending_violations"] == 0,
        "max_depth_one": first_lightweight["max_depth"] == 1
        and first_lifecycle["max_depth_violations"] == 0,
        "sequence_monotonic": bool(first_lightweight["sequence_monotonic"])
        and first_lifecycle["sequence_violations"] == 0,
        "final_drain_is_last_produced": bool(first_lightweight["final_drain_is_last_produced"])
        and first_lifecycle["final_drain_failures"] == 0,
        "no_torn_tuple": bool(first_lightweight["no_torn_tuple"])
        and first_lifecycle["torn_tuple_violations"] == 0,
        "no_cross_session_leak": first_lifecycle["cross_session_leaks"] == 0,
        "no_deadlock": (
            first_lightweight["threads_left_alive"] == 0
            and first_lifecycle["deadlocks"] == 0
            and first_lifecycle["backend_threads_left_alive"] == 0
            and first_vc003_fake_lifecycle["threads_left_alive"] == 0
        ),
        "stop_idempotent": first_lifecycle["stop_idempotence_failures"] == 0,
        "four_role_concurrency": (
            first_lightweight["concurrent_roles"] == 4
            and first_lightweight["overlap_operation_count"]
            == _expected_lightweight_overlap_count(
                config.publish_take_operations,
                config.seed,
            )
            and first_lifecycle["concurrent_roles"] == 4
            and first_lifecycle["backend_status"] == "PASS"
            and first_lifecycle["backend_races"] >= config.lifecycle_races
        ),
        "memory_bounded": bool(first_memory["passed"]),
        "full_size_pixel_cas": first_pixel_cas["status"] == "PASS",
        "vc003_fake_lifecycle": first_vc003_fake_lifecycle["status"] == "PASS",
    }
    evidence_complete = bool(
        all(run["memory"]["evidence_complete"] for run in runs)
        and all(run["pixel_cas"]["status"] == "PASS" for run in runs)
        and all(
            run["lightweight"]["concurrent_roles"] == 4
            and run["lightweight"]["overlap_operation_count"]
            == _expected_lightweight_overlap_count(
                config.publish_take_operations,
                config.seed,
            )
            and run["lifecycle"]["concurrent_roles"] == 4
            and run["lifecycle"]["backend_status"] == "PASS"
            and run["lifecycle"]["backend_races"] >= config.lifecycle_races
            and run["lightweight"]["threads_left_alive"] == 0
            and run["lifecycle"]["deadlocks"] == 0
            and run["lifecycle"]["backend_threads_left_alive"] == 0
            and run["vc003_fake_lifecycle"]["threads_left_alive"] == 0
            for run in runs
        )
        and all(run["vc003_fake_lifecycle"]["status"] == "PASS" for run in runs)
    )
    requirements_met = config.minimums_met
    status = "PASS" if not failures and all(all_invariants.values()) else "FAIL"
    if config.enforce_minimums and not requirements_met:
        status = "FAIL"
        failures = sorted(set([*failures, "minimum_scale_not_met"]))
    coverage = "FULL" if requirements_met and evidence_complete else "SMOKE"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "report_id": f"capture-pressure-{commit[:12]}-{config.seed:x}",
        "generated_at": generated_at or _utc_timestamp(),
        "source_commit": commit,
        "tool_artifact_sha256": tool_hash,
        "config_sha256": config_hash,
        "execution_mode": "offline",
        "status": status,
        "deterministic": deterministic,
        "repeat_count": config.repetitions,
        "configuration": config_dict,
        "environment": {
            "runtime": "python",
            "execution_mode": "offline",
            "hardware_used": False,
            "network_used": False,
            "python_version": platform.python_version(),
            "platform": platform.platform(aliased=True),
            "dependency_lock_sha256": _artifact_hash(
                root / "configs" / "requirements.lock", b"maple-capture-pressure-lock"
            ),
            # A local stress run is not a wheel build; keep the binding explicit
            # rather than implying that checkout code is a distributable wheel.
            "wheel_sha256": _ZERO_SHA256,
        },
        "schedules": {
            "schedule_version": SCHEDULE_VERSION,
            "seed": config.seed,
            "repeat_count": config.repetitions,
            "barrier_controlled": True,
        },
        "requirements": {
            "minimum_publish_take_operations": MIN_PUBLISH_TAKE_OPERATIONS,
            "minimum_lifecycle_races": MIN_LIFECYCLE_RACES,
            "publish_take_operations": config.publish_take_operations,
            "lifecycle_races": config.lifecycle_races,
            "minimums_met": requirements_met,
            "evidence_complete": evidence_complete,
            "coverage": coverage,
        },
        "summary": {
            "publish_operations": first_lightweight["publish_operations"],
            "take_operations": first_lightweight["take_operations"],
            "total_lightweight_operations": first_lightweight["total_operations"],
            "lifecycle_races": first_lifecycle["races"],
            "epochs_checked": first_lifecycle["epochs_checked"],
            "produced": first_lightweight["produced"],
            "delivered": first_lightweight["delivered"],
            "superseded": first_lightweight["superseded"],
            "max_depth": first_lightweight["max_depth"],
            "pending": first_lightweight["pending"],
            "in_flight": first_lightweight["in_flight"],
            "discarded_on_reset": first_lightweight["discarded_on_reset"],
            "discarded_on_error": first_lightweight["discarded_on_error"],
            "accounting_holds": all_invariants["accounting_holds"],
            "sequence_monotonic": all_invariants["sequence_monotonic"],
            "final_drain_is_last_produced": all_invariants["final_drain_is_last_produced"],
            "no_torn_tuple": all_invariants["no_torn_tuple"],
            "no_cross_session_leak": all_invariants["no_cross_session_leak"],
            "no_deadlock": all_invariants["no_deadlock"],
            "four_role_concurrency": all_invariants["four_role_concurrency"],
            "memory_bounded": all_invariants["memory_bounded"],
            "full_size_pixel_cas": all_invariants["full_size_pixel_cas"],
            "vc003_fake_lifecycle": all_invariants["vc003_fake_lifecycle"],
            "invariant_failures": len(failures),
        },
        "counter_epochs": {
            "lightweight_epoch_count": 1,
            "lifecycle_epoch_count": first_lifecycle["epochs_checked"],
            "equation_holds": all_invariants["accounting_holds"],
            "pending_binary": all_invariants["pending_binary"],
            "in_flight_binary": first_lightweight["in_flight"] in (0, 1),
            "max_depth": first_lightweight["max_depth"],
            "scope": "core_owned_raw_latest",
        },
        "timeouts": {
            "race_timeout_s": float(config.timeout_s),
            "deadlocks": first_lifecycle["deadlocks"],
            "threads_left_alive": (
                first_lightweight["threads_left_alive"]
                + first_lifecycle["deadlocks"]
                + first_lifecycle["backend_threads_left_alive"]
                + first_vc003_fake_lifecycle["threads_left_alive"]
            ),
            "all_threads_exit": all_invariants["no_deadlock"],
        },
        "memory": first_memory,
        "pixel_cas": first_pixel_cas,
        "vc003_fake_lifecycle": first_vc003_fake_lifecycle,
        "concurrency": {
            "role_count": 4,
            "producer": True,
            "consumer": True,
            "metrics_observer": True,
            "reset_stop_controller": True,
            "barrier_controlled": True,
            "slot_calls_serialized_by_harness": False,
            "observed_overlap_operations": first_lightweight["overlap_operation_count"],
        },
        "invariants": all_invariants,
        "runs": runs,
        "input_audit": {
            "input_owner": "legacy",
            "real_input_enabled": False,
            "real_input_call_count": 0,
            "core_v2_real_input_call_count": 0,
            "receiver_connect_count": 0,
            "window_write_count": 0,
            "double_write_event_count": 0,
            "network_call_count": 0,
        },
        "failures": failures,
        "limitations": list(_LIMITATIONS),
        "artifacts": artifacts,
        "artifact_list_sha256": artifact_list_sha256,
        "deterministic_evidence_sha256": deterministic_evidence_sha256,
    }
    report_hash = _digest(payload)
    payload["canonical_report_sha256"] = report_hash
    # ``report_digest`` is a compatibility alias used by existing report
    # tooling.  Both values intentionally bind the same digest preimage.
    payload["report_digest"] = report_hash
    report = CapturePressureReport(payload=payload)
    verify_capture_pressure_report(report.payload, repo_root=root)
    return report


# Friendly aliases for callers that use the tactical package vocabulary.
run_stress = run_capture_pressure
run_pressure = run_capture_pressure
run_capture_pressure_stress = run_capture_pressure


def _verify_memory_evidence(memory: Mapping[str, Any]) -> None:
    required = {
        "scope",
        "measurement",
        "bucket_bytes",
        "threshold_bytes",
        "tracemalloc",
        "rss",
        "evidence_complete",
        "linear_growth_observed",
        "passed",
    }
    if set(memory) != required:
        raise CapturePressureError("memory evidence keys mismatch")
    if (
        memory["scope"] != "core_owned_raw_latest"
        or memory["measurement"] != "tracemalloc_and_process_rss"
        or memory["bucket_bytes"] != MEMORY_BUCKET_BYTES
        or memory["threshold_bytes"] != MEMORY_GROWTH_THRESHOLD_BYTES
    ):
        raise CapturePressureError("memory evidence identity mismatch")

    def verify_metric(metric: Any, name: str) -> bool:
        if not isinstance(metric, Mapping):
            raise CapturePressureError(f"{name} memory evidence must be a mapping")
        metric_required = {
            "relative_to_before",
            "available",
            "before_bytes",
            "mid_bytes",
            "after_bytes",
            "peak_bytes",
            "growth_bytes",
            "threshold_bytes",
            "linear_growth_observed",
            "within_threshold",
        }
        if set(metric) != metric_required:
            raise CapturePressureError(f"{name} memory evidence keys mismatch")
        if metric["threshold_bytes"] != MEMORY_GROWTH_THRESHOLD_BYTES:
            raise CapturePressureError(f"{name} memory threshold mismatch")
        if metric["relative_to_before"] is not True:
            raise CapturePressureError(f"{name} memory baseline mode mismatch")
        values = [metric[field] for field in ("before_bytes", "mid_bytes", "after_bytes")]
        if any(value is not None and type(value) is not int for value in values):
            raise CapturePressureError(f"{name} memory samples must be integer or null")
        peak = metric["peak_bytes"]
        if peak is not None and type(peak) is not int:
            raise CapturePressureError(f"{name} memory peak must be integer or null")
        if any(value is not None and value < 0 for value in values) or (
            peak is not None and peak < 0
        ):
            raise CapturePressureError(f"{name} memory samples must be non-negative")
        available = all(value is not None for value in values)
        if values[0] not in (0, None):
            raise CapturePressureError(f"{name} memory baseline mismatch")
        if peak is not None and values[2] is not None and peak < values[2]:
            raise CapturePressureError(f"{name} memory peak cannot be below the end sample")
        if metric["available"] is not available:
            raise CapturePressureError(f"{name} memory availability mismatch")
        expected_growth = _memory_delta(values[0], values[2])
        expected_first = _memory_delta(values[0], values[1])
        expected_tail = _memory_delta(values[1], values[2])
        if metric["growth_bytes"] != expected_growth:
            raise CapturePressureError(f"{name} memory growth mismatch")
        expected_linear = (
            expected_growth is None
            or expected_tail is None
            or (
                expected_tail > 8 * MEMORY_BUCKET_BYTES
                and expected_tail >= max(MEMORY_BUCKET_BYTES, (expected_first or 0) // 2)
            )
        )
        if metric["linear_growth_observed"] is not expected_linear:
            raise CapturePressureError(f"{name} linear-growth result mismatch")
        expected_within = (
            expected_growth is not None
            and expected_growth <= MEMORY_GROWTH_THRESHOLD_BYTES
            and (peak is None or peak <= MEMORY_GROWTH_THRESHOLD_BYTES)
        )
        if metric["within_threshold"] is not expected_within:
            raise CapturePressureError(f"{name} memory threshold result mismatch")
        return bool(available)

    tracemalloc_available = verify_metric(memory["tracemalloc"], "tracemalloc")
    rss_available = verify_metric(memory["rss"], "rss")
    complete = tracemalloc_available and rss_available
    linear = bool(
        memory["tracemalloc"]["linear_growth_observed"] or memory["rss"]["linear_growth_observed"]
    )
    expected_passed = bool(
        complete
        and not linear
        and memory["tracemalloc"]["within_threshold"]
        and memory["rss"]["within_threshold"]
    )
    if memory["evidence_complete"] is not complete:
        raise CapturePressureError("memory completeness mismatch")
    if memory["linear_growth_observed"] is not linear:
        raise CapturePressureError("memory linear-growth mismatch")
    if memory["passed"] is not expected_passed:
        raise CapturePressureError("memory pass result mismatch")


def _verify_pixel_cas_evidence(pixel_cas: Mapping[str, Any]) -> None:
    required = {
        "fixture",
        "width",
        "height",
        "channels",
        "byte_length",
        "pixel_digest",
        "expected_zero_digest",
        "copy_verified",
        "hash_verified",
        "cas_put_get_verified",
        "cas_ref",
        "failures",
        "status",
    }
    if set(pixel_cas) != required:
        raise CapturePressureError("full-size Pixel CAS evidence keys mismatch")
    if (
        pixel_cas["fixture"] != "full_size_zero_bgr8_v1"
        or pixel_cas["width"] != 1920
        or pixel_cas["height"] != 1080
        or pixel_cas["channels"] != 3
        or pixel_cas["byte_length"] != FULL_SIZE_PIXEL_BYTES
        or pixel_cas["expected_zero_digest"] != FULL_SIZE_ZERO_DIGEST
    ):
        raise CapturePressureError("full-size Pixel CAS fixture mismatch")
    if pixel_cas["status"] != "PASS":
        raise CapturePressureError("full-size Pixel CAS evidence is not PASS")
    if (
        pixel_cas["pixel_digest"] != FULL_SIZE_ZERO_DIGEST
        or pixel_cas["cas_ref"] != f"cas://sha256/{FULL_SIZE_ZERO_DIGEST}"
        or pixel_cas["copy_verified"] is not True
        or pixel_cas["hash_verified"] is not True
        or pixel_cas["cas_put_get_verified"] is not True
        or pixel_cas["failures"] != []
    ):
        raise CapturePressureError("full-size Pixel CAS result mismatch")


def _verify_vc003_fake_lifecycle(evidence: Mapping[str, Any], timeout_s: float) -> None:
    required = {
        "normal_start",
        "normal_read",
        "normal_stop",
        "normal_final_drain",
        "reset_new_session",
        "blocked_start",
        "blocked_start_cleanup",
        "blocked_read",
        "eof_fatal_latch",
        "read_fatal_latch",
        "format_drift_fatal_latch",
        "timestamp_rollback_fatal_latch",
        "start_failure_latch",
        "identity_drift_latch",
        "non_exit_mode",
        "non_exit_fatal_cleanup",
        "non_exit_residual_observed",
        "non_exit_cleanup_cleared",
        "threads_left_alive",
        "failures",
        "status",
    }
    if set(evidence) != required:
        raise CapturePressureError("VC003 fake lifecycle evidence keys mismatch")
    if evidence["status"] != "PASS" or evidence["failures"] != []:
        raise CapturePressureError("VC003 fake lifecycle evidence is not PASS")
    for field in (
        "normal_start",
        "normal_read",
        "normal_stop",
        "reset_new_session",
        "normal_final_drain",
        "blocked_start",
        "blocked_start_cleanup",
        "blocked_read",
        "eof_fatal_latch",
        "read_fatal_latch",
        "format_drift_fatal_latch",
        "timestamp_rollback_fatal_latch",
        "start_failure_latch",
        "identity_drift_latch",
        "non_exit_fatal_cleanup",
        "non_exit_residual_observed",
        "non_exit_cleanup_cleared",
    ):
        if evidence[field] is not True:
            raise CapturePressureError(f"VC003 fake lifecycle result mismatch: {field}")
    if evidence["non_exit_mode"] not in {"bounded_fake_child", "source_backend_stop_deadline"}:
        raise CapturePressureError("VC003 fake lifecycle cleanup mode mismatch")
    if evidence["threads_left_alive"] != 0:
        raise CapturePressureError("VC003 fake lifecycle left a thread alive")
    expected = _run_vc003_fake_lifecycle(timeout_s)
    if dict(evidence) != expected:
        raise CapturePressureError("VC003 fake lifecycle could not be recomputed")


def verify_capture_pressure_report(
    payload: Mapping[str, Any], *, repo_root: str | Path | None = None
) -> None:
    """Recompute report digests and semantic invariants; reject tampering."""

    if not isinstance(payload, Mapping):
        raise CapturePressureError("capture pressure report must be a mapping")
    validation_root = Path(repo_root).resolve() if repo_root is not None else _default_repo_root()
    required = {
        "schema_version",
        "report_type",
        "report_id",
        "generated_at",
        "source_commit",
        "tool_artifact_sha256",
        "config_sha256",
        "execution_mode",
        "status",
        "deterministic",
        "repeat_count",
        "configuration",
        "environment",
        "schedules",
        "requirements",
        "summary",
        "counter_epochs",
        "timeouts",
        "memory",
        "pixel_cas",
        "concurrency",
        "vc003_fake_lifecycle",
        "invariants",
        "runs",
        "input_audit",
        "failures",
        "limitations",
        "artifacts",
        "artifact_list_sha256",
        "deterministic_evidence_sha256",
        "canonical_report_sha256",
        "report_digest",
    }
    unknown = set(payload) - required
    missing = required - set(payload)
    if unknown:
        raise CapturePressureError(f"report has unknown keys: {sorted(unknown)!r}")
    if missing:
        raise CapturePressureError(f"report is missing keys: {sorted(missing)!r}")
    if payload["schema_version"] != SCHEMA_VERSION or payload["report_type"] != REPORT_TYPE:
        raise CapturePressureError("report schema identity mismatch")
    if payload["execution_mode"] != "offline":
        raise CapturePressureError("execution_mode must be offline")
    _ensure_commit(payload["source_commit"])
    _ensure_sha256(payload["tool_artifact_sha256"], "tool_artifact_sha256")
    _ensure_sha256(payload["config_sha256"], "config_sha256")
    declared_hash = _ensure_sha256(payload["canonical_report_sha256"], "canonical_report_sha256")
    if declared_hash != payload["report_digest"]:
        raise CapturePressureError("canonical_report_sha256 and report_digest differ")
    digest_payload = dict(payload)
    digest_payload.pop("canonical_report_sha256", None)
    digest_payload.pop("report_digest", None)
    if _digest(digest_payload) != declared_hash:
        raise CapturePressureError("report digest mismatch")
    if type(payload["deterministic"]) is not bool:
        raise CapturePressureError("deterministic must be boolean")
    repeat_count = payload["repeat_count"]
    _ensure_positive_int(repeat_count, "repeat_count")
    if repeat_count < 3:
        raise CapturePressureError("repeat_count must be at least 3")
    configuration = payload["configuration"]
    if not isinstance(configuration, Mapping):
        raise CapturePressureError("configuration must be a mapping")
    config_required = set(CapturePressureConfig().to_dict())
    if set(configuration) != config_required:
        raise CapturePressureError("configuration keys mismatch")
    config_obj = CapturePressureConfig(
        publish_take_operations=configuration["publish_take_operations"],
        lifecycle_races=configuration["lifecycle_races"],
        repetitions=configuration["repetitions"],
        seed=configuration["seed"],
        timeout_s=configuration["timeout_s"],
        enforce_minimums=configuration["enforce_minimums"],
    )
    if _digest(dict(configuration)) != payload["config_sha256"]:
        raise CapturePressureError("config_sha256 mismatch")
    if config_obj.repetitions != repeat_count:
        raise CapturePressureError("repeat_count/configuration mismatch")
    environment = payload["environment"]
    if not isinstance(environment, Mapping):
        raise CapturePressureError("environment must be a mapping")
    expected_environment = {
        "runtime": "python",
        "execution_mode": "offline",
        "hardware_used": False,
        "network_used": False,
        "python_version": platform.python_version(),
        "platform": platform.platform(aliased=True),
        "dependency_lock_sha256": _artifact_hash(
            validation_root / "configs" / "requirements.lock",
            b"maple-capture-pressure-lock",
        ),
        "wheel_sha256": _ZERO_SHA256,
    }
    if dict(environment) != expected_environment:
        raise CapturePressureError("environment must describe offline synthetic execution")
    schedules = payload["schedules"]
    if schedules != {
        "schedule_version": SCHEDULE_VERSION,
        "seed": config_obj.seed,
        "repeat_count": config_obj.repetitions,
        "barrier_controlled": True,
    }:
        raise CapturePressureError("schedule binding mismatch")
    requirements = payload["requirements"]
    if not isinstance(requirements, Mapping):
        raise CapturePressureError("requirements must be a mapping")
    if requirements.get("minimum_publish_take_operations") != MIN_PUBLISH_TAKE_OPERATIONS:
        raise CapturePressureError("minimum publish requirement mismatch")
    if requirements.get("minimum_lifecycle_races") != MIN_LIFECYCLE_RACES:
        raise CapturePressureError("minimum lifecycle requirement mismatch")
    if requirements.get("publish_take_operations") != config_obj.publish_take_operations:
        raise CapturePressureError("publish operation requirement mismatch")
    if requirements.get("lifecycle_races") != config_obj.lifecycle_races:
        raise CapturePressureError("lifecycle requirement mismatch")
    minimums_met = config_obj.minimums_met
    if requirements.get("minimums_met") is not minimums_met:
        raise CapturePressureError("minimums_met mismatch")
    declared_evidence_complete = requirements.get("evidence_complete")
    if type(declared_evidence_complete) is not bool:
        raise CapturePressureError("evidence_complete must be boolean")
    runs = payload["runs"]
    if not isinstance(runs, list) or len(runs) != repeat_count:
        raise CapturePressureError("run count mismatch")
    run_summaries: list[str] = []
    event_summaries: list[str] = []
    lightweight_event_ledgers: list[str] = []
    lifecycle_event_ledgers: list[str] = []
    backend_event_ledgers: list[str] = []
    run_digests: list[str] = []
    run_statuses: list[str] = []
    all_failures: list[str] = []
    for expected_index, run in enumerate(runs, start=1):
        if not isinstance(run, Mapping):
            raise CapturePressureError("run must be a mapping")
        for key in (
            "run_index",
            "lightweight",
            "lifecycle",
            "failures",
            "status",
            "summary_digest",
            "event_digest",
            "run_digest",
            "memory",
            "pixel_cas",
            "vc003_fake_lifecycle",
        ):
            if key not in run:
                raise CapturePressureError(f"run missing key: {key}")
        if run["run_index"] != expected_index:
            raise CapturePressureError("run indexes must be contiguous")
        lightweight = run["lightweight"]
        lifecycle = run["lifecycle"]
        memory = run["memory"]
        pixel_cas = run["pixel_cas"]
        vc003_fake_lifecycle = run["vc003_fake_lifecycle"]
        run_failures = run["failures"]
        if not isinstance(lightweight, Mapping) or not isinstance(lifecycle, Mapping):
            raise CapturePressureError("run phase summaries must be mappings")
        if (
            not isinstance(memory, Mapping)
            or not isinstance(pixel_cas, Mapping)
            or not isinstance(vc003_fake_lifecycle, Mapping)
        ):
            raise CapturePressureError("run evidence summaries must be mappings")
        if not isinstance(run_failures, list) or any(
            not isinstance(item, str) for item in run_failures
        ):
            raise CapturePressureError("run failures must be string arrays")
        summary_payload = _run_summary_payload(lightweight, lifecycle, run_failures)
        if run["summary_digest"] != _digest(summary_payload):
            raise CapturePressureError("run summary_digest mismatch")
        run_payload = {
            "run_index": expected_index,
            **summary_payload,
            "memory": dict(memory),
            "pixel_cas": dict(pixel_cas),
            "vc003_fake_lifecycle": dict(vc003_fake_lifecycle),
        }
        if run["run_digest"] != _digest(
            {key: value for key, value in run_payload.items() if key not in {"run_index", "memory"}}
        ):
            raise CapturePressureError("run_digest mismatch")
        if not isinstance(run["event_digest"], str) or len(run["event_digest"]) != 64:
            raise CapturePressureError("event_digest malformed")
        expected_event = _digest(
            {
                "schedule_version": SCHEDULE_VERSION,
                "seed": config_obj.seed,
                "publish_take_operations": config_obj.publish_take_operations,
                "lifecycle_races": config_obj.lifecycle_races,
                "concurrent_roles": (
                    "producer",
                    "consumer",
                    "metrics_observer",
                    "reset_stop_controller",
                ),
                "lightweight_event_ledger": lightweight["event_ledger_digest"],
                "lifecycle_event_ledger": lifecycle["event_ledger_digest"],
                "backend_event_ledger": lifecycle["backend_event_ledger_digest"],
                "vc003_fake_lifecycle": _digest(vc003_fake_lifecycle),
                "full_size_fixture": "full_size_zero_bgr8_v1",
            }
        )
        if run["event_digest"] != expected_event:
            raise CapturePressureError("event_digest mismatch")
        run_summaries.append(cast(str, run["summary_digest"]))
        event_summaries.append(run["event_digest"])
        lightweight_event_ledgers.append(cast(str, lightweight["event_ledger_digest"]))
        lifecycle_event_ledgers.append(cast(str, lifecycle["event_ledger_digest"]))
        backend_event_ledgers.append(cast(str, lifecycle["backend_event_ledger_digest"]))
        run_digests.append(cast(str, run["run_digest"]))
        run_statuses.append(cast(str, run["status"]))
        all_failures.extend(run_failures)
        if run["status"] != ("PASS" if not run_failures else "FAIL"):
            raise CapturePressureError("run status contradicts failures")
        _verify_phase_summaries(lightweight, lifecycle, config_obj)
        _verify_memory_evidence(memory)
        _verify_pixel_cas_evidence(pixel_cas)
        _verify_vc003_fake_lifecycle(vc003_fake_lifecycle, float(config_obj.timeout_s))
    expected_evidence_complete = bool(
        all(run["memory"]["evidence_complete"] for run in runs)
        and all(run["pixel_cas"]["status"] == "PASS" for run in runs)
        and all(
            run["lightweight"]["concurrent_roles"] == 4
            and run["lightweight"]["overlap_operation_count"]
            == _expected_lightweight_overlap_count(
                config_obj.publish_take_operations,
                config_obj.seed,
            )
            and run["lifecycle"]["concurrent_roles"] == 4
            and run["lifecycle"]["backend_status"] == "PASS"
            and run["lifecycle"]["backend_races"] >= config_obj.lifecycle_races
            for run in runs
        )
        and all(run["vc003_fake_lifecycle"]["status"] == "PASS" for run in runs)
    )
    if declared_evidence_complete is not expected_evidence_complete:
        raise CapturePressureError("evidence_complete mismatch")
    expected_coverage = "FULL" if minimums_met and expected_evidence_complete else "SMOKE"
    if requirements.get("coverage") != expected_coverage:
        raise CapturePressureError("coverage mismatch")
    deterministic = (
        len(set(run_summaries)) == 1
        and len(set(event_summaries)) == 1
        and len(set(lightweight_event_ledgers)) == 1
        and len(set(lifecycle_event_ledgers)) == 1
        and len(set(backend_event_ledgers)) == 1
        and len(set(run_digests)) == 1
    )
    if payload["deterministic"] is not deterministic:
        raise CapturePressureError("deterministic flag mismatch")
    if (
        run_statuses
        and any(status != "PASS" for status in run_statuses)
        and payload["status"] == "PASS"
    ):
        raise CapturePressureError("report PASS contradicts run status")
    if sorted(set(payload["failures"])) != sorted(set(all_failures)):
        raise CapturePressureError("report failures do not match runs")
    invariants = payload["invariants"]
    if not isinstance(invariants, Mapping):
        raise CapturePressureError("invariants must be a mapping")
    expected_invariants = _derive_invariants(payload["runs"][0])
    if dict(invariants) != expected_invariants:
        raise CapturePressureError("invariants mismatch")
    summary = payload["summary"]
    if not isinstance(summary, Mapping):
        raise CapturePressureError("summary must be a mapping")
    first_lightweight = payload["runs"][0]["lightweight"]
    first_lifecycle = payload["runs"][0]["lifecycle"]
    first_memory = payload["runs"][0]["memory"]
    first_pixel_cas = payload["runs"][0]["pixel_cas"]
    first_vc003_fake_lifecycle = payload["runs"][0]["vc003_fake_lifecycle"]
    if summary.get("publish_operations") != first_lightweight.get("publish_operations"):
        raise CapturePressureError("summary publish count mismatch")
    if summary.get("take_operations") != first_lightweight.get("take_operations"):
        raise CapturePressureError("summary take count mismatch")
    if summary.get("total_lightweight_operations") != first_lightweight.get("total_operations"):
        raise CapturePressureError("summary total operation count mismatch")
    if summary.get("lifecycle_races") != first_lifecycle.get("races"):
        raise CapturePressureError("summary race count mismatch")
    if summary.get("epochs_checked") != first_lifecycle.get("epochs_checked"):
        raise CapturePressureError("summary epoch count mismatch")
    for field in (
        "produced",
        "delivered",
        "superseded",
        "pending",
        "in_flight",
        "discarded_on_reset",
        "discarded_on_error",
        "max_depth",
    ):
        if summary.get(field) != first_lightweight.get(field):
            raise CapturePressureError(f"summary {field} count mismatch")
    expected_invariant_summary = {
        "accounting_holds": expected_invariants["accounting_holds"],
        "sequence_monotonic": expected_invariants["sequence_monotonic"],
        "final_drain_is_last_produced": expected_invariants["final_drain_is_last_produced"],
        "no_torn_tuple": expected_invariants["no_torn_tuple"],
        "no_cross_session_leak": expected_invariants["no_cross_session_leak"],
        "no_deadlock": expected_invariants["no_deadlock"],
        "four_role_concurrency": expected_invariants["four_role_concurrency"],
        "memory_bounded": expected_invariants["memory_bounded"],
        "full_size_pixel_cas": expected_invariants["full_size_pixel_cas"],
        "vc003_fake_lifecycle": expected_invariants["vc003_fake_lifecycle"],
    }
    for field, expected in expected_invariant_summary.items():
        if summary.get(field) is not expected:
            raise CapturePressureError(f"summary {field} invariant mismatch")
    if summary.get("invariant_failures") != len(payload["failures"]):
        raise CapturePressureError("summary invariant failure count mismatch")
    counter_epochs = payload["counter_epochs"]
    if counter_epochs != {
        "lightweight_epoch_count": 1,
        "lifecycle_epoch_count": first_lifecycle["epochs_checked"],
        "equation_holds": expected_invariants["accounting_holds"],
        "pending_binary": expected_invariants["pending_binary"],
        "in_flight_binary": first_lightweight["in_flight"] in (0, 1),
        "max_depth": first_lightweight["max_depth"],
        "scope": "core_owned_raw_latest",
    }:
        raise CapturePressureError("counter epoch summary mismatch")
    timeouts = payload["timeouts"]
    if timeouts != {
        "race_timeout_s": config_obj.timeout_s,
        "deadlocks": first_lifecycle["deadlocks"],
        "threads_left_alive": (
            first_lightweight["threads_left_alive"]
            + first_lifecycle["deadlocks"]
            + first_lifecycle["backend_threads_left_alive"]
            + first_vc003_fake_lifecycle["threads_left_alive"]
        ),
        "all_threads_exit": expected_invariants["no_deadlock"],
    }:
        raise CapturePressureError("timeout summary mismatch")
    memory = payload["memory"]
    if not isinstance(memory, Mapping) or dict(memory) != dict(first_memory):
        raise CapturePressureError("memory summary mismatch")
    _verify_memory_evidence(memory)
    pixel_cas = payload["pixel_cas"]
    if not isinstance(pixel_cas, Mapping) or dict(pixel_cas) != dict(first_pixel_cas):
        raise CapturePressureError("full-size Pixel CAS summary mismatch")
    _verify_pixel_cas_evidence(pixel_cas)
    expected_pixel_cas = _run_full_size_pixel_cas()
    if dict(pixel_cas) != expected_pixel_cas:
        raise CapturePressureError("full-size Pixel CAS could not be recomputed")
    vc003_fake_lifecycle = payload["vc003_fake_lifecycle"]
    if not isinstance(vc003_fake_lifecycle, Mapping) or dict(vc003_fake_lifecycle) != dict(
        first_vc003_fake_lifecycle
    ):
        raise CapturePressureError("VC003 fake lifecycle summary mismatch")
    _verify_vc003_fake_lifecycle(vc003_fake_lifecycle, float(config_obj.timeout_s))
    concurrency = payload["concurrency"]
    expected_concurrency = {
        "role_count": 4,
        "producer": True,
        "consumer": True,
        "metrics_observer": True,
        "reset_stop_controller": True,
        "barrier_controlled": True,
        "slot_calls_serialized_by_harness": False,
        "observed_overlap_operations": first_lightweight["overlap_operation_count"],
    }
    if concurrency != expected_concurrency:
        raise CapturePressureError("concurrency evidence mismatch")
    audit = payload["input_audit"]
    if not isinstance(audit, Mapping):
        raise CapturePressureError("input_audit must be a mapping")
    expected_zero_fields = (
        "real_input_call_count",
        "core_v2_real_input_call_count",
        "receiver_connect_count",
        "window_write_count",
        "double_write_event_count",
        "network_call_count",
    )
    if audit.get("input_owner") != "legacy" or audit.get("real_input_enabled") is not False:
        raise CapturePressureError("input audit policy mismatch")
    if any(audit.get(field) != 0 for field in expected_zero_fields):
        raise CapturePressureError("input audit contains nonzero activity")
    if payload["status"] not in {"PASS", "FAIL"}:
        raise CapturePressureError("invalid report status")
    artifacts = payload["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise CapturePressureError("artifact list must contain source and schema evidence")
    artifact_by_id = {
        item.get("artifact_id"): item for item in artifacts if isinstance(item, Mapping)
    }
    expected_artifacts = {
        "capture-pressure-source": (
            "source",
            "src/maple_automation_core/capture/stress.py",
            _artifact_hash(Path(__file__), b"maple-capture-pressure-source"),
        ),
        "capture-pressure-schema": (
            "schema",
            "schemas/capture-pressure-report.schema.json",
            _artifact_hash(
                validation_root / "schemas" / "capture-pressure-report.schema.json",
                b"maple-capture-pressure-schema",
            ),
        ),
    }
    if set(artifact_by_id) != set(expected_artifacts):
        raise CapturePressureError("artifact ids mismatch")
    for artifact_id, (kind, locator, artifact_hash) in expected_artifacts.items():
        item = artifact_by_id[artifact_id]
        if (
            item.get("kind") != kind
            or item.get("locator") != locator
            or item.get("sha256") != artifact_hash
        ):
            raise CapturePressureError(f"artifact evidence mismatch: {artifact_id}")
    _ensure_sha256(payload["artifact_list_sha256"], "artifact_list_sha256")
    artifact_list_sha256 = _digest(list(artifacts))
    if payload["artifact_list_sha256"] != artifact_list_sha256:
        raise CapturePressureError("artifact list digest mismatch")
    _ensure_sha256(payload["deterministic_evidence_sha256"], "deterministic_evidence_sha256")
    expected_run_digests = {run["run_digest"] for run in runs}
    expected_deterministic_evidence = _digest(
        {
            "run_digest": next(iter(expected_run_digests), _ZERO_SHA256),
            "artifact_list_sha256": artifact_list_sha256,
        }
    )
    if payload["deterministic_evidence_sha256"] != expected_deterministic_evidence:
        raise CapturePressureError("deterministic evidence digest mismatch")
    if payload["tool_artifact_sha256"] != expected_artifacts["capture-pressure-source"][2]:
        raise CapturePressureError("tool_artifact_sha256 does not bind source artifact")
    expected_status = (
        "PASS"
        if deterministic and not all_failures and all(expected_invariants.values())
        else "FAIL"
    )
    if config_obj.enforce_minimums and not minimums_met:
        expected_status = "FAIL"
    if payload["status"] != expected_status:
        raise CapturePressureError("report status contradicts derived evidence")


def _verify_phase_summaries(
    lightweight: Mapping[str, Any], lifecycle: Mapping[str, Any], config: CapturePressureConfig
) -> None:
    required_lightweight = {
        "publish_operations",
        "take_operations",
        "total_operations",
        "produced",
        "delivered",
        "superseded",
        "pending",
        "in_flight",
        "discarded_on_reset",
        "discarded_on_error",
        "max_depth",
        "accounting_holds",
        "pending_binary",
        "sequence_monotonic",
        "final_drain_sequence",
        "last_produced_sequence",
        "final_drain_is_last_produced",
        "no_torn_tuple",
        "concurrent_publish_operations",
        "concurrent_take_operations",
        "concurrent_publish_take_operations",
        "overlap_operation_count",
        "concurrent_roles",
        "event_ledger_algorithm",
        "event_ledger_count",
        "event_ledger_digest",
        "threads_left_alive",
        "failures",
        "status",
    }
    if set(lightweight) != required_lightweight:
        raise CapturePressureError("lightweight summary keys mismatch")
    if lightweight["publish_operations"] != config.publish_take_operations:
        raise CapturePressureError("publish count does not match configuration")
    if lightweight["take_operations"] != config.publish_take_operations:
        raise CapturePressureError("take count does not match configuration")
    if lightweight["total_operations"] != (
        lightweight["publish_operations"] + lightweight["take_operations"]
    ):
        raise CapturePressureError("total operation count mismatch")
    expected_overlap = _expected_lightweight_overlap_count(
        config.publish_take_operations,
        config.seed,
    )
    if lightweight["overlap_operation_count"] != expected_overlap:
        raise CapturePressureError("lightweight schedule overlap mismatch")
    if lightweight["concurrent_publish_operations"] != lightweight["overlap_operation_count"]:
        raise CapturePressureError("concurrent publish count mismatch")
    if lightweight["concurrent_take_operations"] != lightweight["overlap_operation_count"]:
        raise CapturePressureError("concurrent take count mismatch")
    if lightweight["concurrent_publish_take_operations"] != (
        lightweight["concurrent_publish_operations"] + lightweight["concurrent_take_operations"]
    ):
        raise CapturePressureError("concurrent operation count mismatch")
    if lightweight["concurrent_publish_take_operations"] < min(2, lightweight["total_operations"]):
        raise CapturePressureError("concurrent operation minimum not met")
    if lightweight["overlap_operation_count"] < min(1, config.publish_take_operations - 1):
        raise CapturePressureError("observed producer/consumer overlap minimum not met")
    if lightweight["concurrent_roles"] != 4 or lightweight["threads_left_alive"] != 0:
        raise CapturePressureError("lightweight concurrency evidence failed")
    if (
        not isinstance(lightweight["event_ledger_digest"], str)
        or len(lightweight["event_ledger_digest"]) != 64
        or lightweight["event_ledger_algorithm"] != "sha256-lines-v1"
        or lightweight["event_ledger_count"] != lightweight["total_operations"]
        or lightweight["event_ledger_digest"]
        != _expected_lightweight_event_ledger_digest(
            config.publish_take_operations,
            config.seed,
        )
    ):
        raise CapturePressureError("lightweight event ledger digest malformed")
    if lightweight["produced"] != lightweight["publish_operations"]:
        raise CapturePressureError("produced count mismatch")
    if (
        lightweight["pending"] not in (0, 1)
        or lightweight["in_flight"] not in (0, 1)
        or lightweight["max_depth"] != 1
    ):
        raise CapturePressureError("slot depth invariant failed")
    if lightweight["produced"] != (
        lightweight["delivered"]
        + lightweight["superseded"]
        + lightweight["pending"]
        + lightweight["in_flight"]
        + lightweight["discarded_on_reset"]
        + lightweight["discarded_on_error"]
    ):
        raise CapturePressureError("lightweight counter equation failed")
    if lightweight["final_drain_sequence"] != config.publish_take_operations:
        raise CapturePressureError("lightweight final drain mismatch")
    if lightweight["failures"] != []:
        raise CapturePressureError("lightweight phase contains failures")
    required_lifecycle = {
        "races",
        "epochs_checked",
        "reset_count",
        "stop_races",
        "session_checks",
        "blocked_read_checks",
        "restart_checks",
        "underlying_session_checks",
        "underlying_stop_checks",
        "final_drain_checks",
        "final_drain_performed",
        "final_drain_sequence",
        "backend_races",
        "backend_start_checks",
        "backend_blocked_read_checks",
        "backend_reset_checks",
        "backend_stop_checks",
        "backend_session_checks",
        "backend_restart_checks",
        "backend_event_ledger_algorithm",
        "backend_event_ledger_count",
        "backend_event_ledger_digest",
        "backend_threads_left_alive",
        "backend_status",
        "event_ledger_algorithm",
        "event_ledger_count",
        "concurrent_roles",
        "event_ledger_digest",
        "accounting_failures",
        "pending_violations",
        "in_flight_violations",
        "discarded_on_error_violations",
        "max_depth_violations",
        "sequence_violations",
        "torn_tuple_violations",
        "cross_session_leaks",
        "deadlocks",
        "unhandled_exceptions",
        "final_drain_failures",
        "stop_idempotence_failures",
        "reset_accounting_failures",
        "metrics_observer_failures",
        "publish_after_stop_failures",
        "restart_after_stop_failures",
        "underlying_session_rejection_failures",
        "blocked_read_failures",
        "backend_lifecycle_failures",
        "status",
    }
    if set(lifecycle) != required_lifecycle:
        raise CapturePressureError("lifecycle summary keys mismatch")
    if (
        lifecycle["races"] != config.lifecycle_races
        or lifecycle["epochs_checked"] != config.lifecycle_races * 2
        or lifecycle["reset_count"] != config.lifecycle_races
        or lifecycle["stop_races"] != config.lifecycle_races
        or lifecycle["session_checks"] != config.lifecycle_races
        or lifecycle["blocked_read_checks"] != config.lifecycle_races
        or lifecycle["restart_checks"] != config.lifecycle_races
        or lifecycle["underlying_session_checks"] != config.lifecycle_races
        or lifecycle["underlying_stop_checks"] != config.lifecycle_races
        or lifecycle["final_drain_checks"] != config.lifecycle_races
        or lifecycle["final_drain_performed"] is not True
        or lifecycle["final_drain_sequence"] != "last_produced"
        or lifecycle["concurrent_roles"] != 4
        or lifecycle["backend_races"] != config.lifecycle_races
        or lifecycle["backend_start_checks"] != config.lifecycle_races * 2
        or lifecycle["backend_blocked_read_checks"] != config.lifecycle_races * 2
        or lifecycle["backend_reset_checks"] != max(0, config.lifecycle_races * 2 - 1)
        or lifecycle["backend_stop_checks"] != config.lifecycle_races * 2
        or lifecycle["backend_session_checks"] != config.lifecycle_races
        or lifecycle["backend_restart_checks"] != config.lifecycle_races
        or lifecycle["backend_event_ledger_algorithm"] != "sha256-lines-v1"
        or lifecycle["backend_event_ledger_count"] != config.lifecycle_races
        or not isinstance(lifecycle["backend_event_ledger_digest"], str)
        or len(lifecycle["backend_event_ledger_digest"]) != 64
        or lifecycle["backend_event_ledger_digest"]
        != _expected_backend_event_ledger_digest(config.lifecycle_races)
        or lifecycle["backend_threads_left_alive"] != 0
        or lifecycle["backend_status"] != "PASS"
        or lifecycle["backend_lifecycle_failures"] != 0
        or lifecycle["event_ledger_algorithm"] != "sha256-lines-v1"
        or lifecycle["event_ledger_count"] != config.lifecycle_races + 1
        or not isinstance(lifecycle["event_ledger_digest"], str)
        or len(lifecycle["event_ledger_digest"]) != 64
        or lifecycle["event_ledger_digest"]
        != _expected_lifecycle_event_ledger_digest(config.lifecycle_races)
    ):
        raise CapturePressureError("lifecycle count mismatch")
    if any(
        lifecycle[key] != 0
        for key in required_lifecycle
        if key.endswith("failures")
        or key
        in {
            "deadlocks",
            "unhandled_exceptions",
            "cross_session_leaks",
            "in_flight_violations",
            "discarded_on_error_violations",
        }
    ):
        raise CapturePressureError("lifecycle invariant failed")
    if lifecycle["status"] != "PASS":
        raise CapturePressureError("lifecycle status failed")


def _derive_invariants(run: Mapping[str, Any]) -> dict[str, bool]:
    lightweight = run["lightweight"]
    lifecycle = run["lifecycle"]
    return {
        "accounting_holds": bool(lightweight["accounting_holds"])
        and lifecycle["accounting_failures"] == 0,
        "pending_binary": bool(lightweight["pending_binary"])
        and lifecycle["pending_violations"] == 0,
        "max_depth_one": lightweight["max_depth"] == 1 and lifecycle["max_depth_violations"] == 0,
        "sequence_monotonic": bool(lightweight["sequence_monotonic"])
        and lifecycle["sequence_violations"] == 0,
        "final_drain_is_last_produced": bool(lightweight["final_drain_is_last_produced"])
        and lifecycle["final_drain_failures"] == 0,
        "no_torn_tuple": bool(lightweight["no_torn_tuple"])
        and lifecycle["torn_tuple_violations"] == 0,
        "no_cross_session_leak": lifecycle["cross_session_leaks"] == 0,
        "no_deadlock": (
            lightweight["threads_left_alive"] == 0
            and lifecycle["deadlocks"] == 0
            and lifecycle["backend_threads_left_alive"] == 0
            and run["vc003_fake_lifecycle"]["threads_left_alive"] == 0
        ),
        "stop_idempotent": lifecycle["stop_idempotence_failures"] == 0,
        "four_role_concurrency": (
            lightweight["concurrent_roles"] == 4
            and lightweight["overlap_operation_count"]
            >= min(1, lightweight["publish_operations"] - 1)
            and lifecycle["concurrent_roles"] == 4
            and lifecycle["backend_status"] == "PASS"
            and lifecycle["backend_races"] >= lifecycle["races"]
        ),
        "memory_bounded": bool(run["memory"]["passed"]),
        "full_size_pixel_cas": run["pixel_cas"]["status"] == "PASS",
        "vc003_fake_lifecycle": run["vc003_fake_lifecycle"]["status"] == "PASS",
    }


__all__ = [
    "DEFAULT_SEED",
    "MIN_LIFECYCLE_RACES",
    "MIN_PUBLISH_TAKE_OPERATIONS",
    "REPORT_TYPE",
    "SCHEDULE_VERSION",
    "SCHEMA_VERSION",
    "CapturePressureConfig",
    "CapturePressureError",
    "CapturePressureReport",
    "run_capture_pressure",
    "run_capture_pressure_stress",
    "run_pressure",
    "run_stress",
    "verify_capture_pressure_report",
]
