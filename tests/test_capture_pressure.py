"""Deterministic producer-pressure tests for the VC-003 latest slot."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from maple_automation_core.capture.pixel_store import UNKNOWN_DEVICE_FINGERPRINT_SHA256
from maple_automation_core.capture.vc003_source import (
    NegotiatedCaptureFacts,
    RawLatestSlot,
    VC003Source,
    VC003SourceConfig,
)


class BurstBackend:
    def __init__(self, count: int, *, width: int = 2, height: int = 1) -> None:
        self.frames = [bytes([index % 256]) * (width * height * 3) for index in range(count)]
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.device_name = "VC-003 Video"
        self.device_fingerprint_sha256 = UNKNOWN_DEVICE_FINGERPRINT_SHA256
        self.negotiated_facts = NegotiatedCaptureFacts(
            width=width,
            height=height,
            fps=60.0,
            fourcc="MJPG",
            backend="dshow",
            backend_api="dshow",
            backend_version="fake-v1",
        )

    def start(self) -> None:
        self.started.set()

    def read(self) -> bytes | None:
        if self.frames:
            return self.frames.pop(0)
        self.stopped.wait(0.001)
        return None

    def stop(self) -> None:
        self.stopped.set()


def _wait_until(predicate: Any, timeout_s: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        time.sleep(0.001)


def _config() -> VC003SourceConfig:
    return VC003SourceConfig(
        source_id="capture-card-primary",
        session_id="pressure-a",
        clock_domain="monotonic",
        transform_version="capture-v1",
        device_name="VC-003 Video",
        width=2,
        height=1,
        fps=60.0,
        poll_interval_s=0.0001,
    )


def test_burst_pressure_is_bounded_to_one_pending_sample() -> None:
    backend = BurstBackend(120)
    source = VC003Source(_config(), backend=backend)
    source.start()
    try:
        _wait_until(lambda: source.status().produced == 120)
        snapshot = source.status()
        assert snapshot.max_depth == 1
        assert snapshot.pending == 1
        assert snapshot.delivered == 0
        assert snapshot.superseded == 119
        assert snapshot.produced == (
            snapshot.delivered
            + snapshot.superseded
            + snapshot.pending
            + snapshot.discarded_on_reset
        )
        latest = source.read()
        assert latest is not None
        assert latest.sequence == 120
        assert latest.raw_bytes == bytes([119]) * 6
        snapshot = source.status()
        assert snapshot.pending == 0
        assert snapshot.delivered == 1
        assert snapshot.accounting_holds
    finally:
        source.stop()


def test_reset_accounts_for_pending_sample_without_cross_session_leak() -> None:
    backends = [BurstBackend(1), BurstBackend(0)]
    factory_calls = 0

    def factory(_config: VC003SourceConfig) -> BurstBackend:
        nonlocal factory_calls
        backend = backends[factory_calls]
        factory_calls += 1
        return backend

    source = VC003Source(_config(), backend_factory=factory)
    source.start()
    try:
        _wait_until(lambda: source.status().pending == 1)
        source.reset("pressure-b")
        snapshot = source.status()
        assert snapshot.produced == 0
        assert snapshot.pending == 0
        assert snapshot.discarded_on_reset == 0
        assert snapshot.session_id == "pressure-b"
        assert snapshot.accounting_holds
        assert source.read() is None
        sealed = source.last_reset_status
        assert sealed is not None
        assert sealed.produced == 1
        assert sealed.discarded_on_reset == 1
        assert sealed.delivered == 0
        source.start()
        assert factory_calls == 2
        assert backends[0] is not backends[1]
    finally:
        source.stop()


def test_raw_slot_handles_100k_publishes_with_bounded_depth_and_exact_accounting() -> None:
    slot: RawLatestSlot[int] = RawLatestSlot()
    for value in range(100_000):
        slot.publish(value)
    snapshot = slot.status()
    assert snapshot.max_depth == 1
    assert snapshot.pending == 1
    assert snapshot.produced == 100_000
    assert snapshot.superseded == 99_999
    assert snapshot.accounting_holds
    assert slot.take() == 99_999
    assert slot.status().delivered == 1
    assert slot.status().pending == 0
    assert slot.status().accounting_holds


def test_raw_slot_stop_is_idempotent_and_preserves_final_drain() -> None:
    slot: RawLatestSlot[int] = RawLatestSlot()
    slot.publish(1)
    slot.stop()
    slot.stop()
    assert slot.take() == 1
    assert slot.status().delivered == 1
    try:
        slot.publish(2)
    except RuntimeError as exc:
        assert "stopped" in str(exc)
    else:  # pragma: no cover - the slot must reject post-stop publication
        raise AssertionError("publish after stop unexpectedly succeeded")


def test_raw_slot_restart_requires_new_epoch_and_rejects_old_session() -> None:
    slot: RawLatestSlot[object] = RawLatestSlot(session_id="session-a")
    slot.publish(SimpleNamespace(session_id="session-a", sequence=1))
    slot.stop()
    with pytest.raises(RuntimeError, match="requires reset"):
        slot.start("session-a")
    sealed = slot.reset("session-b")
    assert sealed.session_id == "session-a"
    with pytest.raises(RuntimeError, match="old or mismatched session"):
        slot.publish(SimpleNamespace(session_id="session-a", sequence=2))
    slot.publish(SimpleNamespace(session_id="session-b", sequence=1))
    assert slot.status().session_id == "session-b"


def test_raw_slot_blocked_take_honors_cancel_and_deadline() -> None:
    slot: RawLatestSlot[int] = RawLatestSlot()
    cancel = threading.Event()
    cancel.set()
    assert slot.take(timeout=1.0, cancel_event=cancel) is None
    started = time.monotonic()
    assert slot.take(timeout=0.01) is None
    assert time.monotonic() - started < 0.5


def test_raw_slot_rejects_second_producer() -> None:
    slot: RawLatestSlot[int] = RawLatestSlot()
    slot.publish(1)
    errors: list[BaseException] = []

    def publish_from_second_thread() -> None:
        try:
            slot.publish(2)
        except BaseException as exc:  # pragma: no cover - assertion captures it
            errors.append(exc)

    thread = threading.Thread(target=publish_from_second_thread)
    thread.start()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
