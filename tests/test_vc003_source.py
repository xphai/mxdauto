"""Unit coverage for the read-only VC-003 raw source."""

from __future__ import annotations

import gc
import threading
import time
import weakref
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from maple_automation_core.capture.frame_source import (
    FrameSourceAdapter,
    FrameSourceConfig,
    canonical_calibration_sha256,
)
from maple_automation_core.capture.pixel_store import (
    UNKNOWN_DEVICE_FINGERPRINT_SHA256,
    CaptureSourceProvenance,
    PixelStore,
    canonical_json,
    pixel_digest,
)
from maple_automation_core.capture.vc003_source import (
    BackendFrame,
    NegotiatedCaptureFacts,
    OpenCVCaptureBackend,
    RawFrameSpec,
    RawLatestSlot,
    RawLatestStatus,
    VC003RawFrame,
    VC003Source,
    VC003SourceConfig,
    VC003SourceError,
    _coerce_bytes_and_spec,
    _payload_parts,
    _spec_from_value,
    make_opencv_backend,
)
from maple_automation_core.domain.frame import FrameSize, SourceGeometry, SourceRect


class FakeBackend:
    """Small deterministic backend used in place of a device."""

    def __init__(self, frames: list[Any], *, delay_s: float = 0.0) -> None:
        self.frames = list(frames)
        self.delay_s = delay_s
        self.started = 0
        self.stopped = 0
        self.reads = 0
        self.stop_event = threading.Event()
        self.device_name = "VC-003 Video"
        self.device_fingerprint_sha256 = UNKNOWN_DEVICE_FINGERPRINT_SHA256
        self.negotiated_facts = NegotiatedCaptureFacts(
            width=2,
            height=1,
            fps=30.0,
            fourcc="MJPG",
            backend="dshow",
            backend_api="dshow",
            backend_version="fake-v1",
        )

    def start(self) -> None:
        self.started += 1

    def read(self) -> Any | None:
        self.reads += 1
        if self.frames:
            value = self.frames.pop(0)
            if self.delay_s:
                time.sleep(self.delay_s)
            return value
        self.stop_event.wait(0.001)
        return None

    def stop(self) -> None:
        self.stopped += 1
        self.stop_event.set()


def _config(**overrides: Any) -> VC003SourceConfig:
    values: dict[str, Any] = {
        "source_id": "capture-card-primary",
        "session_id": "session-a",
        "clock_domain": "monotonic",
        "transform_version": "capture-v1",
        "device_name": "VC-003 Video",
        "device_index": 0,
        "backend": "dshow",
        "width": 2,
        "height": 1,
        "fps": 30.0,
        "poll_interval_s": 0.0001,
    }
    values.update(overrides)
    return VC003SourceConfig(**values)


def _wait_until(predicate: Any, timeout_s: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        time.sleep(0.001)


def _bytes_frame(value: int) -> bytes:
    return bytes([value]) * 6


def test_created_status_has_zeroed_capture_evidence_counters() -> None:
    source = VC003Source(_config(), backend=FakeBackend([]))
    status = source.status()
    assert status.lifecycle == "created"
    assert status.read_attempts == 0
    assert status.no_frame_count == 0
    assert status.read_failure_count == 0
    assert status.decode_rejection_count == 0
    assert status.eof_count == 0
    assert status.reconnect_count == 0
    assert status.backend_fallback_count == 0
    assert not status.final_drain_performed


def test_controller_reservation_is_private_and_rejected_while_slot_is_running() -> None:
    slot: RawLatestSlot[Any] = RawLatestSlot(session_id="session-a")
    sample = SimpleNamespace(session_id="session-a", sequence=1)
    slot.publish(sample)
    assert not hasattr(slot, "reserve_for_final_drain")
    with pytest.raises(RuntimeError, match="stopped raw latest slot"):
        slot._reserve_for_final_drain()
    status = slot.status()
    assert status.pending == 1
    assert status.delivered == 0
    assert not status.consumer_bound


def test_raw_sample_is_immutable_and_keeps_sequence_hash_spec_and_bytes_together() -> None:
    backend = FakeBackend([_bytes_frame(7)])
    source = VC003Source(_config(), backend=backend, clock=iter([100]).__next__)

    source.start()
    try:
        _wait_until(lambda source=source: source.status().pending == 1)
        sample = source.read()
        assert isinstance(sample, VC003RawFrame)
        assert sample is not None
        assert sample.sequence == 1
        assert sample.frame_id == sample.sequence
        assert sample.raw_bytes == _bytes_frame(7)
        assert sample.bytes is sample.raw_bytes
        assert sample.spec == RawFrameSpec(width=2, height=1)
        assert sample.content_hash == pixel_digest(sample.spec, sample.raw_bytes)
        assert sample.hash == sample.content_hash
        assert sample.image_ref == f"cas://sha256/{sample.content_hash}"
        assert sample.source_size.width == sample.spec.width
        assert sample.source_size.height == sample.spec.height
        with pytest.raises(FrozenInstanceError):
            sample.raw_bytes = b"x" * 6  # type: ignore[misc]
    finally:
        source.stop()


def test_adapter_receives_only_a_resolvable_verified_cas_reference(tmp_path: Path) -> None:
    geometry = SourceGeometry(
        source_size=FrameSize(width=2, height=1),
        content_rect=SourceRect(x=0, y=0, width=2, height=1),
        working_size=FrameSize(width=2, height=1),
    )
    store = PixelStore(tmp_path / "private-cas")
    source = VC003Source(
        _config(source_geometry=geometry),
        backend=FakeBackend([_bytes_frame(9)]),
        pixel_store=store,
        clock=iter([100]).__next__,
    )
    adapter = FrameSourceAdapter(
        source,
        FrameSourceConfig(
            session_id="session-a",
            source_id="capture-card-primary",
            clock_domain="monotonic",
            transform_version="capture-v1",
            source_geometry=geometry,
            max_age_ns=250_000_000,
        ),
        clock=iter([101]).__next__,
    )
    source.start()
    try:
        _wait_until(lambda source=source: source.status().pending == 1)
        result = adapter.poll()
        assert result.accepted
        assert result.packet is not None
        digest = result.packet.content_hash
        assert result.packet.image_ref == f"cas://sha256/{digest}"
        assert store.read(digest, RawFrameSpec(width=2, height=1)) == _bytes_frame(9)
    finally:
        source.stop()


def test_pixel_cas_failure_latches_source_error_before_adapter_delivery(
    tmp_path: Path,
) -> None:
    class FailingStore(PixelStore):
        def put_artifact(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("synthetic CAS failure")

    backend = FakeBackend([_bytes_frame(3)])
    source = VC003Source(
        _config(),
        backend=backend,
        pixel_store=FailingStore(tmp_path / "failing-cas"),
    )
    source.start()
    _wait_until(lambda: source.status().pending == 1)
    with pytest.raises(VC003SourceError, match="pixel CAS admission failed"):
        source.read()
    status = source.status()
    assert status.lifecycle == "error"
    assert status.error is not None
    assert "synthetic CAS failure" in status.error
    with pytest.raises(VC003SourceError, match="pixel CAS admission failed"):
        source.read()
    source.stop()


def test_latest_capacity_one_supersedes_old_sample_and_accounts_for_every_produced_frame() -> None:
    backend = FakeBackend([_bytes_frame(value) for value in range(1, 5)])
    source = VC003Source(_config(), backend=backend)

    source.start()
    try:
        _wait_until(lambda: source.status().produced == 4)
        status = source.status()
        assert status.pending == 1
        assert status.max_depth == 1
        assert status.superseded == 3
        assert status.delivered == 0
        assert status.accounting_holds
        latest = source.read()
        assert latest is not None
        assert latest.sequence == 4
        assert latest.raw_bytes == _bytes_frame(4)
        status = source.status()
        assert status.delivered == 1
        assert status.pending == 0
        assert status.accounting_holds
    finally:
        source.stop()


def test_reset_discards_pending_and_starts_new_sequence_and_session() -> None:
    backends = [FakeBackend([_bytes_frame(1)]), FakeBackend([_bytes_frame(2)])]
    factory_calls = 0

    def factory(_config: VC003SourceConfig) -> FakeBackend:
        nonlocal factory_calls
        backend = backends[factory_calls]
        factory_calls += 1
        return backend

    source = VC003Source(_config(), backend_factory=factory)

    source.start()
    try:
        _wait_until(lambda current=source: current.status().pending == 1)
        source.reset("session-b")
        status = source.status()
        assert status.session_id == "session-b"
        assert status.pending == 0
        assert status.produced == 0
        assert status.discarded_on_reset == 0
        assert status.next_sequence == 1
        assert not status.final_drain_performed
        assert source.read() is None
        assert status.accounting_holds
        sealed = source.last_reset_status
        assert sealed is not None
        assert sealed.discarded_on_reset == 1
        assert sealed.delivered == 0
        assert sealed.produced == 1

        source.start()
        _wait_until(lambda: source.status().pending == 1)
        sample = source.read()
        assert sample is not None
        assert sample.session_id == "session-b"
        assert sample.sequence == 1
        assert sample.raw_bytes == _bytes_frame(2)
        assert source.status().accounting_holds
        assert factory_calls == 2
        assert backends[0] is not backends[1]
        assert backends[0].stopped == 1
    finally:
        source.stop()


def test_reset_discards_an_inflight_cas_sample_without_returning_old_session(
    tmp_path: Path,
) -> None:
    cas_entered = threading.Event()
    release_cas = threading.Event()
    reset_stop_entered = threading.Event()

    class BlockingStore(PixelStore):
        def put_artifact(self, *args: Any, **kwargs: Any) -> Any:
            cas_entered.set()
            assert release_cas.wait(1.0)
            return super().put_artifact(*args, **kwargs)

    class ResetBackend(FakeBackend):
        def stop(self) -> None:
            reset_stop_entered.set()
            super().stop()

    backends = [ResetBackend([_bytes_frame(1)]), FakeBackend([])]
    source = VC003Source(
        _config(),
        backend_factory=lambda _config: backends.pop(0),
        pixel_store=BlockingStore(tmp_path / "reset-inflight-cas"),
    )
    source.start()
    reader_result: list[Any] = []
    reset_errors: list[BaseException] = []

    def read_inflight() -> None:
        reader_result.append(source.read(timeout=1.0))

    reader = threading.Thread(target=read_inflight)
    reader.start()
    try:
        assert cas_entered.wait(1.0)

        def reset_inflight() -> None:
            try:
                source.reset("session-b")
            except BaseException as exc:
                reset_errors.append(exc)

        resetter = threading.Thread(target=reset_inflight)
        resetter.start()
        assert reset_stop_entered.wait(1.0)
        assert resetter.is_alive()
        release_cas.set()
        # Coverage instrumentation on hosted Windows runners can make the
        # post-release CAS verification materially slower than local runs.
        # This join only observes test completion; the product stop deadline
        # remains independently asserted at two seconds.
        reader.join(timeout=5.0)
        resetter.join(timeout=5.0)
        assert not reader.is_alive()
        assert not resetter.is_alive()
        assert reset_errors == []
        assert reader_result == [None]
        sealed = source.last_reset_status
        assert sealed is not None
        assert sealed.produced == 1
        assert sealed.delivered == 0
        assert sealed.in_flight == 0
        assert sealed.discarded_on_reset == 1
        assert sealed.accounting_holds
        assert source.status().session_id == "session-b"
    finally:
        release_cas.set()
        reader.join(timeout=2.0)
        source.stop()


def test_reset_rejects_same_session_id() -> None:
    source = VC003Source(_config(), backend=FakeBackend([]))
    with pytest.raises(ValueError, match="differ"):
        source.reset("session-a")


def test_single_consumer_is_enforced() -> None:
    backend = FakeBackend([_bytes_frame(1)])
    source = VC003Source(_config(), backend=backend)
    source.start()
    try:
        _wait_until(lambda: source.status().pending == 1)
        assert source.read() is not None
        errors: list[BaseException] = []

        def consume_from_second_thread() -> None:
            try:
                source.read()
            except BaseException as exc:  # pragma: no cover - assertion captures it
                errors.append(exc)

        thread = threading.Thread(target=consume_from_second_thread)
        thread.start()
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
    finally:
        source.stop()


def test_backend_failure_is_terminal_without_factory_retry() -> None:
    stop_entered = threading.Event()
    release_stop = threading.Event()

    class FailingBackend(FakeBackend):
        def read(self) -> Any | None:
            self.reads += 1
            raise OSError("device read failed")

        def stop(self) -> None:
            stop_entered.set()
            release_stop.wait(1.0)
            super().stop()

    backends = [FailingBackend([])]
    factory_calls = 0

    def factory(_config: VC003SourceConfig) -> FailingBackend:
        nonlocal factory_calls
        factory_calls += 1
        return backends[0]

    source = VC003Source(_config(), backend_factory=factory)
    source.start()
    try:
        assert stop_entered.wait(1.0)
        intermediate = source.status()
        assert intermediate.lifecycle == "running"
        assert intermediate.thread_alive
        release_stop.set()

        def terminal_quiescent() -> bool:
            snapshot = source.status()
            return snapshot.lifecycle == "error" and not snapshot.thread_alive

        _wait_until(terminal_quiescent)
        status = source.status()
        assert factory_calls == 1
        assert status.error is not None
        assert "read failed" in status.error
        assert not status.thread_alive
    finally:
        source.stop()
    assert factory_calls == 1


def test_backend_format_drift_is_fatal_without_publishing_the_bad_frame() -> None:
    payload = BackendFrame(
        data=b"x" * 9,
        spec={"width": 3, "height": 1, "channels": 3, "pixel_format": "BGR8"},
    )
    source = VC003Source(_config(), backend=FakeBackend([payload]))
    source.start()
    try:
        _wait_until(lambda: source.status().lifecycle == "error")
        status = source.status()
        assert status.produced == 0
        assert status.pending == 0
        assert status.error is not None
        assert "dimensions" in status.error
    finally:
        source.stop()


def test_backend_identity_drift_is_rejected_at_start() -> None:
    class WrongDeviceBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__([])
            self.device_name = "other-device"

    backend = WrongDeviceBackend()
    source = VC003Source(_config(), backend=backend)
    with pytest.raises(RuntimeError, match="unexpected device"):
        source.start()
    assert backend.started == 1
    assert backend.stopped == 1


def test_backend_must_report_bounded_negotiated_facts_after_start() -> None:
    missing = FakeBackend([])
    del missing.negotiated_facts
    with pytest.raises(TypeError, match="NegotiatedCaptureFacts"):
        VC003Source(_config(), backend=missing).start()

    fractional = FakeBackend([])
    fractional.negotiated_facts = replace(fractional.negotiated_facts, fps=29.97)
    with pytest.raises(RuntimeError, match="contradict source config"):
        VC003Source(_config(), backend=fractional).start()

    directshow_rounding = FakeBackend([])
    directshow_rounding.negotiated_facts = replace(
        directshow_rounding.negotiated_facts,
        fps=30.00003000003,
    )
    rounded_source = VC003Source(_config(), backend=directshow_rounding)
    rounded_source.start()
    rounded_source.stop()

    substring_api = FakeBackend([])
    substring_api.negotiated_facts = replace(
        substring_api.negotiated_facts,
        backend_api="notdshow",
    )
    with pytest.raises(RuntimeError, match="contradict source config"):
        VC003Source(_config(), backend=substring_api).start()

    wrong_fingerprint = FakeBackend([])
    wrong_fingerprint.device_fingerprint_sha256 = "1" * 64
    with pytest.raises(RuntimeError, match="fingerprint contradicts provenance"):
        VC003Source(_config(), backend=wrong_fingerprint).start()


def test_runtime_negotiated_fact_drift_is_fatal_before_publish() -> None:
    class DriftingBackend(FakeBackend):
        def read(self) -> Any | None:
            value = super().read()
            if self.reads == 2:
                self.negotiated_facts = replace(self.negotiated_facts, fourcc="YUY2")
            return value

    backend = DriftingBackend([_bytes_frame(1), _bytes_frame(2)])
    source = VC003Source(_config(), backend=backend)
    source.start()
    try:
        _wait_until(lambda: source.status().lifecycle == "error")
        status = source.status()
        assert status.produced == 1
        assert status.pending == 1
        assert status.error is not None
        assert "format/identity drift" in status.error
    finally:
        source.stop()


def test_stop_never_delivers_a_final_frame_whose_contract_drifted() -> None:
    entered = threading.Event()
    release = threading.Event()

    class Backend(FakeBackend):
        def read(self) -> Any | None:
            self.reads += 1
            entered.set()
            release.wait(1.0)
            return _bytes_frame(1)

        def stop(self) -> None:
            self.negotiated_facts = replace(self.negotiated_facts, fourcc="YUY2")
            release.set()
            super().stop()

    backend = Backend([])
    source = VC003Source(_config(), backend=backend)
    source.start()
    assert entered.wait(1.0)
    assert source.stop() is None
    status = source.status()
    assert status.lifecycle == "error"
    assert status.produced == 0
    assert status.delivered == 0
    assert status.error is not None
    assert "format/identity drift" in status.error


def test_unsupported_backend_is_not_silently_fallen_back() -> None:
    with pytest.raises(ValueError, match="backend must be one"):
        _config(backend="any")


def test_read_surfaces_terminal_error_and_does_not_deliver_old_pending_sample() -> None:
    gate = threading.Event()

    class Backend(FakeBackend):
        def read(self) -> Any | None:
            self.reads += 1
            if self.frames:
                return self.frames.pop(0)
            gate.wait(1.0)
            raise OSError("fatal after pending")

    backend = Backend([_bytes_frame(3)])
    source = VC003Source(_config(), backend=backend)
    source.start()
    try:
        _wait_until(lambda: source.status().pending == 1)
        gate.set()
        _wait_until(lambda: source.status().lifecycle == "error")
        with pytest.raises(VC003SourceError, match="fatal after pending"):
            source.read()
        assert source.status().pending == 1
    finally:
        source.stop()


def test_stop_is_idempotent_and_backend_is_released_once() -> None:
    backend = FakeBackend([_bytes_frame(6)])
    source = VC003Source(_config(), backend=backend)
    source.start()
    _wait_until(lambda: source.status().pending == 1)
    final = source.stop()
    source.stop()
    assert backend.started == 1
    assert backend.stopped == 1
    assert not source.status().thread_alive
    assert source.status().lifecycle == "stopped"
    assert final is not None
    assert final.sequence == 1
    assert source.read() is None
    assert source.status().final_drain_performed
    assert source.status().final_drain_sequence == 1
    assert source.status().accounting_holds


@pytest.mark.parametrize("with_pending_final", [False, True])
def test_controller_stop_drains_without_rebinding_logical_consumer(
    with_pending_final: bool,
) -> None:
    backend = FakeBackend([_bytes_frame(1)])
    source = VC003Source(_config(), backend=backend)
    source.start()
    _wait_until(lambda: source.status().pending == 1)

    consumed: list[VC003RawFrame | None] = []
    consumer = threading.Thread(target=lambda: consumed.append(source.read()))
    consumer.start()
    # Allow slow coverage-instrumented Windows filesystems to finish the CAS
    # write/read transaction without changing the source's two-second stop
    # contract.
    consumer.join(timeout=5.0)
    assert not consumer.is_alive()
    assert consumed[0] is not None
    assert consumed[0].sequence == 1

    if with_pending_final:
        backend.frames.append(_bytes_frame(2))
        _wait_until(lambda: source.status().pending == 1)

    final = source.stop()
    status = source.status()
    assert status.lifecycle == "stopped"
    assert status.error is None
    assert status.final_drain_performed
    assert status.final_drain_sequence == (2 if with_pending_final else 1)
    assert status.delivered == (2 if with_pending_final else 1)
    assert status.accounting_holds
    if with_pending_final:
        assert final is not None
        assert final.sequence == 2
    else:
        assert final is None


def test_concurrent_double_stop_is_single_flight_and_reuses_final_result() -> None:
    for _ in range(20):
        backend = FakeBackend([_bytes_frame(1)])
        source = VC003Source(_config(), backend=backend)
        source.start()
        _wait_until(lambda current=source: current.status().pending == 1)
        barrier = threading.Barrier(3)
        results: list[VC003RawFrame | None] = []

        def invoke_stop(
            source: VC003Source = source,
            barrier: threading.Barrier = barrier,
            results: list[VC003RawFrame | None] = results,
        ) -> None:
            barrier.wait()
            results.append(source.stop())

        callers = [threading.Thread(target=invoke_stop) for _ in range(2)]
        for caller in callers:
            caller.start()
        barrier.wait()
        for caller in callers:
            caller.join(timeout=2.0)
            assert not caller.is_alive()
        status = source.status()
        assert status.lifecycle == "stopped"
        assert status.error is None
        assert status.delivered == 1
        assert status.pending == 0
        assert status.final_drain_sequence == 1
        assert status.accounting_holds
        assert len(results) == 2
        assert all(result is not None and result.sequence == 1 for result in results)


def test_host_capture_timestamp_is_authoritative_and_backend_timestamp_is_locator() -> None:
    payload = BackendFrame(
        data=bytearray(_bytes_frame(9)),
        spec={"width": 2, "height": 1, "channels": 3, "pixel_format": "bgr8"},
        captured_at_ns=1234,
    )
    source = VC003Source(
        _config(),
        backend=FakeBackend([payload]),
        clock=iter([5678]).__next__,
    )
    source.start()
    try:
        _wait_until(lambda: source.status().pending == 1)
        sample = source.read()
        assert sample is not None
        assert sample.captured_at_ns == 5678
        assert sample.received_at_ns == sample.captured_at_ns
        assert sample.image_metadata["backend_timestamp"] == {
            "value_ns": 1234,
            "timing_truth": False,
        }
        assert sample.spec == RawFrameSpec(width=2, height=1)
        assert sample.raw_bytes == _bytes_frame(9)
    finally:
        source.stop()


def test_host_timestamp_is_taken_before_post_retrieve_fact_queries() -> None:
    order: list[str] = []

    class Backend(FakeBackend):
        @property
        def negotiated_facts(self) -> NegotiatedCaptureFacts:
            if "read-returned" in order and "post-facts" not in order:
                order.append("post-facts")
                time.sleep(0.005)
            return self._facts

        @negotiated_facts.setter
        def negotiated_facts(self, value: NegotiatedCaptureFacts) -> None:
            self._facts = value

        def read(self) -> Any | None:
            value = super().read()
            if value is not None:
                order.append("read-returned")
            return value

    def clock() -> int:
        order.append("clock")
        return 123

    source = VC003Source(_config(), backend=Backend([_bytes_frame(1)]), clock=clock)
    source.start()
    try:
        _wait_until(lambda: source.status().pending == 1)
        assert order.index("read-returned") < order.index("clock")
        assert order.index("clock") < order.index("post-facts")
    finally:
        source.stop()


def test_invalid_payload_latches_error_and_does_not_count_as_produced() -> None:
    source = VC003Source(_config(), backend=FakeBackend([b"too short"]))
    source.start()
    try:
        _wait_until(lambda: source.status().lifecycle == "error")
        status = source.status()
        assert status.produced == 0
        assert status.pending == 0
        assert status.error is not None
        assert "frame rejected" in status.error
        assert status.decode_rejection_count == 1
    finally:
        source.stop()


def test_no_frame_and_eof_have_distinct_machine_readable_counters() -> None:
    class Backend(FakeBackend):
        def read(self) -> Any | None:
            self.reads += 1
            value = self.frames.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

    source = VC003Source(
        _config(),
        backend=Backend([None, EOFError("synthetic EOF")]),
    )
    source.start()
    try:
        _wait_until(lambda: source.status().lifecycle == "error")
        status = source.status()
        assert status.read_attempts == 2
        assert status.no_frame_count == 1
        assert status.read_failure_count == 1
        assert status.eof_count == 1
        assert status.reconnect_count == 0
        assert status.backend_fallback_count == 0
        assert status.error is not None
        assert "backend EOF" in status.error
    finally:
        source.stop()


def test_timestamp_regression_is_fatal_without_publishing_the_regressed_sample() -> None:
    backend = FakeBackend([_bytes_frame(1), _bytes_frame(2)])
    source = VC003Source(
        _config(),
        backend=backend,
        clock=iter([20, 10]).__next__,
    )
    source.start()
    try:
        _wait_until(lambda: source.status().lifecycle == "error")
        status = source.status()
        assert status.produced == 1
        assert status.pending == 1
        assert status.last_sequence == 1
        with pytest.raises(VC003SourceError, match="moved backwards"):
            source.read()
    finally:
        source.stop()


def test_opencv_backend_import_is_lazy_and_uses_only_selected_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(backend="dshow")
    imports: list[str] = []

    class Capture:
        def __init__(self) -> None:
            self.released = 0
            self.settings: list[tuple[int, float]] = []

        def isOpened(self) -> bool:
            return True

        def set(self, prop: int, value: float) -> bool:
            self.settings.append((prop, value))
            return True

        def get(self, prop: int) -> float:
            return {
                3: 2.0,
                4: 1.0,
                5: 30.0,
                6: float(sum(ord(char) << (8 * index) for index, char in enumerate("MJPG"))),
            }[prop]

        def getBackendName(self) -> str:
            return "DSHOW"

        def read(self) -> tuple[bool, Any]:
            return True, SimpleNamespace(
                shape=(1, 2, 3),
                dtype="uint8",
                tobytes=lambda **_: _bytes_frame(8),
            )

        def release(self) -> None:
            self.released += 1

    capture = Capture()
    fake_cv2 = SimpleNamespace(
        CAP_DSHOW=700,
        CAP_MSMF=1400,
        CAP_ANY=0,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6,
        __version__="4.10.0",
        VideoWriter_fourcc=lambda *chars: sum(
            ord(char) << (8 * index) for index, char in enumerate(chars)
        ),
        VideoCapture=lambda index, api: (assert_call(index, api, capture)),
    )

    def import_module(name: str) -> Any:
        imports.append(name)
        assert name == "cv2"
        return fake_cv2

    def assert_call(index: int, api: int, value: Capture) -> Capture:
        assert index == 0
        assert api == 700
        return value

    monkeypatch.setattr(
        "maple_automation_core.capture.vc003_source.importlib.import_module", import_module
    )
    backend = make_opencv_backend(config)
    assert imports == []
    assert isinstance(backend, OpenCVCaptureBackend)
    backend.start()
    assert imports == ["cv2"]
    assert [property_id for property_id, _value in capture.settings] == [3, 4, 5, 6]
    assert backend.read() is not None
    backend.stop()
    backend.stop()
    assert capture.released == 1


def test_opencv_backend_rejects_measured_fps_and_fourcc_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Capture:
        def isOpened(self) -> bool:
            return True

        def set(self, _prop: int, _value: float) -> bool:
            return True

        def get(self, prop: int) -> float:
            return {
                3: 2.0,
                4: 1.0,
                5: 15.0,
                6: float(sum(ord(char) << (8 * index) for index, char in enumerate("YUY2"))),
            }[prop]

        def getBackendName(self) -> str:
            return "DSHOW"

        def release(self) -> None:
            pass

    fake_cv2 = SimpleNamespace(
        __version__="4.10.0",
        CAP_DSHOW=700,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6,
        VideoWriter_fourcc=lambda *chars: sum(
            ord(char) << (8 * index) for index, char in enumerate(chars)
        ),
        VideoCapture=lambda _index, _api: Capture(),
    )
    monkeypatch.setattr(
        "maple_automation_core.capture.vc003_source.importlib.import_module",
        lambda _name: fake_cv2,
    )
    backend = OpenCVCaptureBackend(_config())
    with pytest.raises(RuntimeError, match="negotiated fps"):
        backend.start()


def test_opencv_backend_accepts_directshow_sub_millihertz_fps_rounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Capture:
        def isOpened(self) -> bool:
            return True

        def set(self, _prop: int, _value: float) -> bool:
            return True

        def get(self, prop: int) -> float:
            return {
                3: 2.0,
                4: 1.0,
                5: 30.00003000003,
                6: float(sum(ord(char) << (8 * index) for index, char in enumerate("MJPG"))),
            }[prop]

        def getBackendName(self) -> str:
            return "DSHOW"

        def release(self) -> None:
            pass

    fake_cv2 = SimpleNamespace(
        __version__="4.10.0",
        CAP_DSHOW=700,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6,
        VideoWriter_fourcc=lambda *chars: sum(
            ord(char) << (8 * index) for index, char in enumerate(chars)
        ),
        VideoCapture=lambda _index, _api: Capture(),
    )
    monkeypatch.setattr(
        "maple_automation_core.capture.vc003_source.importlib.import_module",
        lambda _name: fake_cv2,
    )
    backend = OpenCVCaptureBackend(_config())
    backend.start()
    assert backend.negotiated_facts is not None
    assert backend.negotiated_facts.fps == pytest.approx(30.00003000003)
    backend.stop()


@pytest.mark.parametrize(
    ("property_id", "value", "name"),
    [
        (3, 2.4, "width"),
        (4, 1.5, "height"),
        (
            6,
            float(sum(ord(char) << (8 * index) for index, char in enumerate("MJPG"))) + 0.4,
            "fourcc",
        ),
    ],
)
def test_opencv_backend_rejects_fractional_dimensions_and_fourcc(
    monkeypatch: pytest.MonkeyPatch,
    property_id: int,
    value: float,
    name: str,
) -> None:
    class Capture:
        def isOpened(self) -> bool:
            return True

        def set(self, _prop: int, _value: float) -> bool:
            return True

        def get(self, prop: int) -> float:
            values = {
                3: 2.0,
                4: 1.0,
                5: 30.0,
                6: float(sum(ord(char) << (8 * index) for index, char in enumerate("MJPG"))),
            }
            values[property_id] = value
            return values[prop]

        def getBackendName(self) -> str:
            return "DSHOW"

        def release(self) -> None:
            pass

    fake_cv2 = SimpleNamespace(
        __version__="4.10.0",
        CAP_DSHOW=700,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6,
        VideoWriter_fourcc=lambda *chars: sum(
            ord(char) << (8 * index) for index, char in enumerate(chars)
        ),
        VideoCapture=lambda _index, _api: Capture(),
    )
    monkeypatch.setattr(
        "maple_automation_core.capture.vc003_source.importlib.import_module",
        lambda _name: fake_cv2,
    )
    backend = OpenCVCaptureBackend(_config())
    with pytest.raises(RuntimeError, match=f"non-integral negotiated {name}"):
        backend.start()


def test_opencv_backend_open_failure_does_not_try_another_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    class Capture:
        def isOpened(self) -> bool:
            return False

        def release(self) -> None:
            pass

    fake_cv2 = SimpleNamespace(
        CAP_DSHOW=700,
        VideoCapture=lambda index, api: (calls.append((index, api)) or Capture()),
    )
    monkeypatch.setattr(
        "maple_automation_core.capture.vc003_source.importlib.import_module",
        lambda name: fake_cv2,
    )
    backend = OpenCVCaptureBackend(_config(backend="dshow"))
    with pytest.raises(RuntimeError, match="open failed"):
        backend.start()
    assert calls == [(0, 700)]


def test_concurrent_start_is_single_flight_and_releases_one_backend() -> None:
    entered = threading.Event()
    release = threading.Event()
    factory_count = 0
    factory_lock = threading.Lock()

    class BlockingStartBackend(FakeBackend):
        def start(self) -> None:
            super().start()
            entered.set()
            release.wait(2.0)

    backend = BlockingStartBackend([])

    def factory(_config: VC003SourceConfig) -> FakeBackend:
        nonlocal factory_count
        with factory_lock:
            factory_count += 1
        return backend

    source = VC003Source(_config(), backend_factory=factory)
    errors: list[BaseException] = []
    callers_ready = threading.Barrier(3)

    def invoke_start() -> None:
        try:
            callers_ready.wait()
            source.start()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    callers = [threading.Thread(target=invoke_start) for _ in range(2)]
    for caller in callers:
        caller.start()
    callers_ready.wait()
    assert entered.wait(1.0)
    assert factory_count == 1
    release.set()
    for caller in callers:
        caller.join(1.0)
        assert not caller.is_alive()
    assert errors == []
    assert factory_count == 1
    assert backend.started == 1
    source.stop()
    assert backend.stopped == 1
    assert source.status().residual_worker_count == 0


def test_stop_cancels_blocked_start_and_configure_cannot_mutate_starting_attempt() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingStartBackend(FakeBackend):
        def start(self) -> None:
            self.started += 1
            entered.set()
            release.wait(2.0)

        def stop(self) -> None:
            super().stop()
            release.set()

    backend = BlockingStartBackend([])
    source = VC003Source(_config(), backend_factory=lambda _config: backend)
    errors: list[BaseException] = []

    def invoke_start() -> None:
        try:
            source.start()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    caller = threading.Thread(target=invoke_start)
    caller.start()
    assert entered.wait(1.0)
    with pytest.raises(RuntimeError, match="before the first capture attempt"):
        source.configure(_config(session_id="session-b"))
    started = time.monotonic()
    source.stop()
    assert time.monotonic() - started < 2.0
    caller.join(1.0)
    assert not caller.is_alive()
    assert len(errors) == 1
    assert backend.started == 1
    assert backend.stopped == 1
    assert source.status().residual_worker_count == 0


def test_reset_preserves_fatal_state_while_blocked_capture_worker_remains() -> None:
    read_entered = threading.Event()
    release_read = threading.Event()

    class NonExitingReadBackend(FakeBackend):
        def read(self) -> Any | None:
            self.reads += 1
            read_entered.set()
            release_read.wait(5.0)
            return None

    first = NonExitingReadBackend([])
    second = FakeBackend([])
    backends = iter([first, second])
    source = VC003Source(_config(), backend_factory=lambda _config: next(backends))
    source.start()
    assert read_entered.wait(1.0)
    started = time.monotonic()
    source.stop()
    assert time.monotonic() - started < 2.0
    before = source.status()
    assert before.lifecycle == "error"
    assert before.thread_alive
    with pytest.raises(RuntimeError, match="confirmed backend and worker cleanup"):
        source.reset("session-b")
    after = source.status()
    assert after.lifecycle == "error"
    assert after.session_id == "session-a"
    assert after.thread_alive
    release_read.set()
    _wait_until(lambda: not source.status().thread_alive, timeout_s=1.0)
    source.stop()


def test_reset_rejects_factory_reuse_of_previous_backend_instance() -> None:
    backend = FakeBackend([_bytes_frame(1)])
    source = VC003Source(_config(), backend_factory=lambda _config: backend)
    source.start()
    _wait_until(lambda: source.status().pending == 1)
    source.stop()
    source.reset("session-b")
    with pytest.raises(RuntimeError, match="newly created backend instance"):
        source.start()
    status = source.status()
    assert status.lifecycle == "error"
    assert status.error is not None
    assert backend.started == 1
    assert backend.stopped == 1


def test_backend_identity_history_uses_pruned_weak_references() -> None:
    backend_refs: list[weakref.ReferenceType[FakeBackend]] = []

    def factory(_config: VC003SourceConfig) -> FakeBackend:
        backend = FakeBackend([])
        backend_refs.append(weakref.ref(backend))
        return backend

    source = VC003Source(_config(), backend_factory=factory)
    for index in range(30):
        source.start()
        source.stop()
        source.reset(f"session-{index + 1}")
    gc.collect()
    assert sum(reference() is not None for reference in backend_refs) == 0
    assert len(source._used_backend_refs) <= 1
    assert source.status().residual_worker_count == 0


def test_cas_failure_is_discarded_not_counted_as_delivered(tmp_path: Path) -> None:
    class FailingStore(PixelStore):
        def put_artifact(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("synthetic occurrence failure")

    source = VC003Source(
        _config(),
        backend=FakeBackend([_bytes_frame(1)]),
        pixel_store=FailingStore(tmp_path / "cas"),
    )
    source.start()
    _wait_until(lambda: source.status().pending == 1)
    with pytest.raises(VC003SourceError, match="occurrence failure"):
        source.read()
    status = source.status()
    assert status.produced == 1
    assert status.delivered == 0
    assert status.pending == 0
    assert status.in_flight == 0
    assert status.discarded_on_error == 1
    assert status.accounting_holds
    source.stop()


def test_external_provenance_requested_format_must_exactly_bind_config() -> None:
    config = _config()
    bad_format = {
        "width": 999,
        "height": 888,
        "fps": 12.5,
        "fourcc": "XVID",
        "backend": "dshow",
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": 999 * 3,
        "length": 999 * 888 * 3,
    }
    provenance = CaptureSourceProvenance(
        source_id=config.source_id,
        session_id=config.session_id,
        requested=bad_format,
        negotiated=bad_format,
        backend=config.backend,
        config_sha256=sha256(canonical_json(config.to_dict())).hexdigest(),
        calibration_sha256=canonical_calibration_sha256(
            config.geometry,
            config.transform_version,
        ),
    )
    with pytest.raises(ValueError, match="exact source session"):
        VC003Source(config, backend=FakeBackend([]), provenance=provenance)


def test_spec_mapping_aliases_and_rejections_are_explicit() -> None:
    spec = _spec_from_value(
        {
            "width": 2,
            "height": 1,
            "channels": 3,
            "format": "bgr8",
            "dtype": "uint8",
            "row_stride": 6,
            "byte_length": 6,
        }
    )
    assert spec == RawFrameSpec(width=2, height=1)
    assert _spec_from_value(None) == RawFrameSpec()
    assert _spec_from_value(spec) is spec
    with pytest.raises(TypeError, match="PixelSpec"):
        _spec_from_value(object())
    for field, value in (("color_space", "RGB"), ("layout", "CHW"), ("version", "pixel-v2")):
        with pytest.raises(ValueError, match=field):
            _spec_from_value({"width": 2, "height": 1, field: value})
    with pytest.raises(ValueError, match="spec missing key"):
        _spec_from_value({"height": 1})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_id", "", "source_id"),
        ("session_id", "", "session_id"),
        ("clock_domain", "", "clock_domain"),
        ("transform_version", "", "transform_version"),
        ("device_name", "", "device_name"),
        ("device_index", -1, "device_index"),
        ("device_index", True, "device_index"),
        ("width", 0, "width"),
        ("width", True, "width"),
        ("height", 0, "height"),
        ("fps", 0, "fps"),
        ("fps", True, "fps"),
        ("fps", float("nan"), "fps"),
        ("fps", float("inf"), "fps"),
        ("fps", float("-inf"), "fps"),
        ("fps", 10**1000, "fps"),
        ("pixel_format", "", "pixel_format"),
        ("poll_interval_s", -1, "poll_interval_s"),
        ("poll_interval_s", True, "poll_interval_s"),
        ("poll_interval_s", float("nan"), "poll_interval_s"),
        ("poll_interval_s", float("inf"), "poll_interval_s"),
        ("poll_interval_s", float("-inf"), "poll_interval_s"),
        ("poll_interval_s", 10**1000, "poll_interval_s"),
    ],
)
def test_config_rejects_invalid_scalar_values(field: str, value: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _config(**{field: value})


def test_config_geometry_and_from_dict_paths() -> None:
    geometry = SourceGeometry(
        source_size=FrameSize(width=2, height=1),
        content_rect=SourceRect(x=0, y=0, width=2, height=1),
        working_size=FrameSize(width=2, height=1),
    )
    config = _config(source_geometry=geometry, backend="msmf", pixel_format="MJPEG")
    assert config.geometry is geometry
    assert config.frame_spec == RawFrameSpec(width=2, height=1)
    restored = VC003SourceConfig.from_dict(config.to_dict())
    assert restored == config
    with pytest.raises(TypeError, match="SourceGeometry"):
        _config(source_geometry=object())
    wrong_geometry = replace(
        geometry,
        source_size=FrameSize(width=3, height=1),
        content_rect=SourceRect(x=0, y=0, width=3, height=1),
    )
    with pytest.raises(ValueError, match="source_geometry.source_size"):
        _config(source_geometry=wrong_geometry)
    with pytest.raises(TypeError, match="payload must be a mapping"):
        VC003SourceConfig.from_dict([])  # type: ignore[arg-type]


def test_negotiated_facts_format_and_validation_paths() -> None:
    facts = NegotiatedCaptureFacts(2, 1, 30, "MJPG", "dshow", "DSHOW", "fake")
    assert facts.to_format_dict() == {
        "width": 2,
        "height": 1,
        "fps": 30,
        "fourcc": "MJPG",
        "backend": "dshow",
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": 6,
        "length": 6,
    }
    with pytest.raises(ValueError, match="fps"):
        NegotiatedCaptureFacts(2, 1, 0, "MJPG", "dshow", "dshow", "fake")
    for invalid_fps in (float("nan"), float("inf"), float("-inf"), 10**1000):
        with pytest.raises(ValueError, match="fps"):
            NegotiatedCaptureFacts(2, 1, invalid_fps, "MJPG", "dshow", "dshow", "fake")
    with pytest.raises(ValueError, match="fourcc"):
        NegotiatedCaptureFacts(2, 1, 30, "", "dshow", "dshow", "fake")


def test_raw_frame_coercion_and_invariant_failures() -> None:
    spec = RawFrameSpec(width=2, height=1)
    raw_bytes = _bytes_frame(4)
    digest = pixel_digest(spec, raw_bytes)

    def make(**overrides: Any) -> VC003RawFrame:
        values: dict[str, Any] = {
            "source_id": "capture-card-primary",
            "session_id": "session-a",
            "frame_id": 1,
            "captured_at_ns": 0,
            "clock_domain": "monotonic",
            "transform_version": "capture-v1",
            "content_hash": digest,
            "image_ref": f"cas://sha256/{digest}",
            "source_size": FrameSize(width=2, height=1),
            "image_metadata": {},
            "received_at_ns": 0,
            "raw_bytes": raw_bytes,
            "spec": spec,
        }
        values.update(overrides)
        return VC003RawFrame(**values)

    sample = make(raw_bytes=bytearray(raw_bytes), spec=spec.to_dict())
    assert sample.image_bytes is sample.raw_bytes
    assert sample.frame_bytes is sample.raw_bytes
    assert sample.frame_spec == spec
    assert isinstance(sample.raw_bytes, bytes)
    with pytest.raises(ValueError, match="raw_bytes length"):
        make(raw_bytes=b"short")
    with pytest.raises(ValueError, match="canonical Pixel V1"):
        make(content_hash="0" * 64)
    with pytest.raises(ValueError, match="source_size must match"):
        make(source_size=FrameSize(width=3, height=1))


def test_status_aliases_and_serialization_cover_all_counters() -> None:
    status = RawLatestStatus(
        epoch=2,
        session_id="session-a",
        produced=5,
        delivered=1,
        superseded=2,
        pending=1,
        in_flight=0,
        discarded_on_reset=1,
        discarded_on_error=0,
        max_depth=1,
        consumer_bound=True,
        producer_bound=True,
        last_produced_sequence=5,
        last_delivered_sequence=3,
    )
    assert status.accounted == 5
    assert status.accounting_holds
    assert status.max_queue_depth == 1
    assert status.dropped == 2
    assert status.counter_epoch == 2
    assert status.produced_count == 5
    assert status.delivered_count == 1
    assert status.superseded_count == 2
    assert status.pending_count == 1
    assert status.discarded_on_reset_count == 1
    serialized = status.to_dict()
    assert serialized["accounted"] == 5
    assert serialized["accounting_holds"] is True


def test_source_status_aliases_properties_and_describe_are_consistent() -> None:
    source = VC003Source(_config(), backend=FakeBackend([]))
    assert source.session_id == "session-a"
    assert source.thread is None
    assert source.max_depth == 0
    assert source.raw_latest_slot is source.raw_slot is source.slot
    assert source.pixel_store is not None
    assert source.provenance.session_id == "session-a"
    assert source.negotiated_facts is None
    assert source.last_reset_status is None
    description = source.describe()
    assert description == {
        "read_only": True,
        "source_id": "capture-card-primary",
        "device_name": "VC-003 Video",
        "device_index": 0,
        "backend": "dshow",
        "requested_width": 2,
        "requested_height": 1,
        "requested_fps": 30.0,
        "wire_pixel_format": "mjpg",
        "raw_spec": RawFrameSpec(width=2, height=1).to_dict(),
        "timestamp_origin": "host_monotonic_post_retrieve",
        "upstream_queue": "unknown",
    }
    status = source.status()
    assert status.accounted == 0
    assert status.accounting_holds
    assert status.max_queue_depth == 0
    assert status.dropped == 0
    assert status.counter_epoch == 0
    assert status.produced_count == 0
    assert status.delivered_count == 0
    assert status.superseded_count == 0
    assert status.pending_count == 0
    assert status.discarded_on_reset_count == 0
    serialized = status.to_dict()
    assert serialized["residual_worker_count"] == 0
    assert serialized["accounting_holds"]


def test_source_default_factory_and_configure_type_and_same_session_paths() -> None:
    default_source = VC003Source(_config())
    assert default_source.config.backend == "dshow"
    source = VC003Source(_config(), backend=FakeBackend([]))
    with pytest.raises(TypeError, match="config"):
        source.configure(object())  # type: ignore[arg-type]
    source.configure(_config(width=2, height=1, pixel_format="mjpeg"))
    assert source.status().epoch == 0
    assert source.status().session_id == "session-a"


def test_constructor_rejects_ambiguous_dependencies_and_bad_types() -> None:
    backend = FakeBackend([])

    def factory(_config: VC003SourceConfig) -> FakeBackend:
        return FakeBackend([])

    with pytest.raises(ValueError, match="provide backend or backend_factory"):
        VC003Source(_config(), backend=backend, backend_factory=factory)
    with pytest.raises(ValueError, match="provide backend_factory or factory"):
        VC003Source(_config(), backend_factory=factory, factory=factory)
    with pytest.raises(TypeError, match="clock"):
        VC003Source(_config(), backend=backend, clock=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="config"):
        VC003Source(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="pixel_store"):
        VC003Source(_config(), backend=backend, pixel_store=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="provenance"):
        VC003Source(_config(), backend=backend, provenance=object())  # type: ignore[arg-type]


def test_configure_before_first_attempt_rebinds_default_provenance() -> None:
    source = VC003Source(_config(), backend=FakeBackend([]))
    next_config = _config(session_id="session-b", pixel_format="mjpeg")
    source.configure(next_config)
    assert source.config == next_config
    assert source.session_id == "session-b"
    assert source.provenance.session_id == "session-b"
    assert source.provenance.backend == "dshow"
    assert source.status().lifecycle == "created"
    assert source.status().next_sequence == 1
    source.start()
    with pytest.raises(RuntimeError, match="before the first capture attempt"):
        source.configure(_config(session_id="session-c"))
    source.stop()


def test_raw_latest_slot_lifecycle_timeout_and_identity_paths() -> None:
    unbound: RawLatestSlot[Any] = RawLatestSlot()
    unbound.start()
    assert not unbound.closed
    slot: RawLatestSlot[Any] = RawLatestSlot()
    assert not slot.closed
    assert slot.pending is None
    assert slot.last_reset_status is None
    sample = SimpleNamespace(session_id="session-a", sequence=1)
    slot.start("session-a")
    with pytest.raises(RuntimeError, match="session change"):
        slot.start("session-b")
    with pytest.raises(ValueError, match="sample"):
        slot.publish(None)
    slot.publish(sample)
    with pytest.raises(RuntimeError, match="session"):
        slot.publish(SimpleNamespace(session_id="other", sequence=2))
    # A second producer and consumer are rejected independently.
    producer_errors: list[BaseException] = []

    def publish_from_other_thread() -> None:
        try:
            slot.publish(SimpleNamespace(session_id="session-a", sequence=2))
        except BaseException as exc:
            producer_errors.append(exc)

    producer = threading.Thread(target=publish_from_other_thread)
    producer.start()
    producer.join(timeout=1)
    assert not producer.is_alive()
    assert producer_errors and "one producer" in str(producer_errors[0])
    assert slot.take(timeout_ms=0) is sample
    assert slot.take(timeout_s=0) is None
    assert slot.take(timeout_ns=0) is None
    assert slot.take(deadline_ns=time.monotonic_ns() - 1) is None
    cancelled = threading.Event()
    cancelled.set()
    assert slot.take(timeout=0.1, cancel_event=cancelled) is None
    with pytest.raises(ValueError, match="only one timeout"):
        slot.take(timeout_ms=1, timeout_s=1)
    with pytest.raises(ValueError, match="timeout_ms"):
        slot.take(timeout_ms=True)
    with pytest.raises(ValueError, match="timeout_ns"):
        slot.take(timeout_ns=True)
    with pytest.raises(ValueError, match="deadline_ns"):
        slot.take(deadline_ns=True)
    with pytest.raises(ValueError, match="timeout must"):
        slot.take(timeout=-1)
    with pytest.raises(ValueError, match="timeout must"):
        slot.take(timeout=float("nan"))
    with pytest.raises(ValueError, match="timeout must"):
        slot.take(timeout=float("inf"))
    for huge_timeout in (10**1000, -(10**1000)):
        with pytest.raises(ValueError, match="timeout"):
            slot.take(timeout=huge_timeout)
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        slot.take(timeout=threading.TIMEOUT_MAX + 1.0)
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        slot.take(timeout_s=1e100)
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        slot.take(timeout_ms=(threading.TIMEOUT_MAX * 1000.0) + 1.0)
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        slot.take(timeout_ns=int(threading.TIMEOUT_MAX * 1_000_000_000) + 1_000_000_000)
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        slot.take(
            deadline_ns=(
                time.monotonic_ns() + int(threading.TIMEOUT_MAX * 1_000_000_000) + 1_000_000_000
            )
        )
    with pytest.raises(ValueError, match="timeout_ms"):
        slot.take(timeout_ms=10**1000)
    with pytest.raises(ValueError, match="timeout_ns"):
        slot.take(timeout_ns=10**1000)
    with pytest.raises(ValueError, match="deadline_ns"):
        slot.take(deadline_ns=10**1000)
    with pytest.raises(TypeError, match="cancel_event"):
        slot.take(cancel_event=object())
    slot.stop()
    assert slot.closed
    with pytest.raises(RuntimeError, match="stopped"):
        slot.publish(SimpleNamespace(session_id="session-a", sequence=3))
    with pytest.raises(RuntimeError, match="requires reset"):
        slot.start()


def test_raw_latest_slot_reservations_final_drain_and_reset_accounting() -> None:
    slot: RawLatestSlot[Any] = RawLatestSlot(session_id="session-a")
    sample = SimpleNamespace(session_id="session-a", sequence=1)
    slot.publish(sample)
    reserved = slot.reserve(timeout_s=0)
    assert reserved is sample
    with pytest.raises(RuntimeError, match="already has"):
        slot.reserve()
    with pytest.raises(RuntimeError, match="identity"):
        slot.commit_reserved(SimpleNamespace(sequence=9))
    with pytest.raises(RuntimeError, match="identity"):
        slot.discard_reserved_on_error(SimpleNamespace(sequence=9))
    slot.discard_reserved_on_error(sample)
    assert slot.status().discarded_on_error == 1

    slot.publish(SimpleNamespace(session_id="session-a", sequence=2))
    slot.stop()
    with pytest.raises(RuntimeError, match="identity"):
        slot.commit_reserved(SimpleNamespace(sequence=2))
    drained = slot._reserve_for_final_drain()
    assert drained is not None
    with pytest.raises(RuntimeError, match="already has"):
        slot._reserve_for_final_drain()
    with pytest.raises(RuntimeError, match="controller reservation"):
        slot.commit_reserved(drained)
    with pytest.raises(RuntimeError, match="controller reservation"):
        slot.discard_reserved_on_error(drained)
    with pytest.raises(RuntimeError, match="identity"):
        slot._discard_final_drain_on_error(SimpleNamespace(sequence=7))
    slot._discard_final_drain_on_error(drained)
    assert slot.status().discarded_on_error == 2
    with pytest.raises(RuntimeError, match="identity"):
        slot._commit_final_drain(SimpleNamespace(sequence=7))

    slot.reset("session-b")
    slot.publish(SimpleNamespace(session_id="session-b", sequence=3))
    reserved_for_reset = slot.reserve()
    assert reserved_for_reset is not None
    slot.stop()
    sealed = slot.reset("session-c")
    assert sealed.discarded_on_reset == 1
    assert slot.status().session_id == "session-c"
    with pytest.raises(ValueError, match="new_session_id"):
        slot.reset("session-c")

    unbound = RawLatestSlot[Any]()
    assert unbound.reset().epoch == 0
    session_bound = RawLatestSlot[Any](session_id="bound")
    with pytest.raises(ValueError, match="new_session_id"):
        session_bound.reset()
    pending = RawLatestSlot[Any](session_id="pending")
    pending.publish(SimpleNamespace(session_id="pending", sequence=1))
    sealed_pending = pending.reset("next")
    assert sealed_pending.discarded_on_reset == 1


def test_raw_latest_slot_reserve_timeout_and_cancellation_validation() -> None:
    slot: RawLatestSlot[Any] = RawLatestSlot()
    assert slot.reserve(timeout_ms=0) is None
    assert slot.reserve(timeout_s=0) is None
    assert slot.reserve(timeout_ns=0) is None
    assert slot.reserve(deadline_ns=time.monotonic_ns() - 1) is None
    cancelled = threading.Event()
    cancelled.set()
    assert slot.reserve(timeout=0.1, cancel_event=cancelled) is None
    with pytest.raises(ValueError, match="only one timeout"):
        slot.reserve(timeout_ms=1, timeout_s=1)
    with pytest.raises(ValueError, match="timeout_ms"):
        slot.reserve(timeout_ms=True)
    with pytest.raises(ValueError, match="timeout_ns"):
        slot.reserve(timeout_ns=True)
    with pytest.raises(ValueError, match="deadline_ns"):
        slot.reserve(deadline_ns=True)
    with pytest.raises(ValueError, match="timeout must"):
        slot.reserve(timeout=-1)
    with pytest.raises(ValueError, match="timeout must"):
        slot.reserve(timeout=float("nan"))
    with pytest.raises(ValueError, match="timeout must"):
        slot.reserve(timeout=float("-inf"))
    with pytest.raises(ValueError, match="timeout_ms"):
        slot.reserve(timeout_ms=10**1000)
    with pytest.raises(ValueError, match="timeout_ns"):
        slot.reserve(timeout_ns=10**1000)
    with pytest.raises(ValueError, match="deadline_ns"):
        slot.reserve(deadline_ns=10**1000)
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        slot.reserve(timeout=threading.TIMEOUT_MAX + 1.0)
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        slot.reserve(timeout_s=1e100)
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        slot.reserve(timeout_ms=(threading.TIMEOUT_MAX * 1000.0) + 1.0)
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        slot.reserve(timeout_ns=int(threading.TIMEOUT_MAX * 1_000_000_000) + 1_000_000_000)
    with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
        slot.reserve(
            deadline_ns=(
                time.monotonic_ns() + int(threading.TIMEOUT_MAX * 1_000_000_000) + 1_000_000_000
            )
        )
    with pytest.raises(TypeError, match="cancel_event"):
        slot.reserve(cancel_event=object())

    waiter: RawLatestSlot[Any] = RawLatestSlot()
    started = threading.Event()

    def delayed_reserve() -> None:
        started.set()
        assert waiter.reserve(timeout=0.02) is None

    thread = threading.Thread(target=delayed_reserve)
    thread.start()
    assert started.wait(1)
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_raw_latest_slot_final_drain_requires_closed_slot_and_handles_empty() -> None:
    slot: RawLatestSlot[Any] = RawLatestSlot()
    with pytest.raises(RuntimeError, match="stopped raw latest slot"):
        slot._reserve_for_final_drain()
    slot.stop()
    assert slot._reserve_for_final_drain() is None


def test_opencv_backend_properties_lazy_read_and_measurement_failures() -> None:
    backend = OpenCVCaptureBackend(_config())
    assert backend.device_name == "VC-003 Video"
    assert backend.device_fingerprint_sha256 == UNKNOWN_DEVICE_FINGERPRINT_SHA256
    assert backend.backend_name == "dshow"
    assert backend.negotiated_facts is None
    assert backend.read() is None
    backend.stop()
    assert backend.negotiated_facts is None

    backend._capture = object()
    backend._cv2 = SimpleNamespace()
    backend._stopped = False
    with pytest.raises(RuntimeError, match="does not expose"):
        backend._measure_negotiated_facts()
    backend._capture = None
    with pytest.raises(RuntimeError, match="not open"):
        backend._measure_negotiated_facts()

    class CaptureWithoutRelease:
        pass

    backend._capture = CaptureWithoutRelease()
    backend._stopped = False
    backend.stop()


def test_opencv_backend_read_rejects_malformed_capture_results() -> None:
    backend = OpenCVCaptureBackend(_config())

    class InvalidCapture:
        def read(self) -> Any:
            return "not-a-pair"

    backend._capture = InvalidCapture()
    with pytest.raises(RuntimeError, match="invalid read"):
        backend.read()

    class FailedCapture:
        def __init__(self, result: Any) -> None:
            self.result = result

        def read(self) -> Any:
            return self.result

    backend._capture = FailedCapture((False, object()))
    with pytest.raises(RuntimeError, match="failed to retrieve"):
        backend.read()
    backend._capture = FailedCapture((True, None))
    with pytest.raises(RuntimeError, match="failed to retrieve"):
        backend.read()
    backend._stopped = True
    assert backend.read() is None


def test_opencv_backend_start_defaults_and_second_start_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Capture:
        def __init__(self) -> None:
            self.settings: list[tuple[int, float]] = []
            self.released = 0

        def isOpened(self) -> bool:
            return True

        def set(self, prop: int, value: float) -> bool:
            self.settings.append((prop, value))
            return True

        def get(self, prop: int) -> float:
            return {
                3: 2.0,
                4: 1.0,
                5: 30.0,
                6: float(sum(ord(c) << (8 * i) for i, c in enumerate("RAW "))),
            }[prop]

        def getBackendName(self) -> str:
            return "DSHOW"

        def release(self) -> None:
            self.released += 1

    capture = Capture()
    fake_cv2 = SimpleNamespace(
        __version__="fake",
        VideoCapture=lambda _index, _api: capture,
    )
    monkeypatch.setattr(
        "maple_automation_core.capture.vc003_source.importlib.import_module",
        lambda _name: fake_cv2,
    )
    backend = OpenCVCaptureBackend(_config(pixel_format="raw"))
    backend.start()
    backend.start()
    assert backend.negotiated_facts is not None
    assert backend.negotiated_facts.backend_api == "dshow"
    assert capture.settings == [(3, 2), (4, 1), (5, 30.0)]
    backend.stop()
    backend.stop()
    assert capture.released == 1


@pytest.mark.parametrize("mode", ["missing", "noncallable", "empty", "raises"])
def test_opencv_backend_requires_measured_backend_api(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    class Capture:
        def isOpened(self) -> bool:
            return True

        def set(self, _prop: int, _value: float) -> bool:
            return True

        def get(self, prop: int) -> float:
            return {
                3: 2.0,
                4: 1.0,
                5: 30.0,
                6: float(sum(ord(c) << (8 * i) for i, c in enumerate("MJPG"))),
            }[prop]

        def release(self) -> None:
            pass

    capture = Capture()
    if mode == "noncallable":
        capture.getBackendName = "DSHOW"  # type: ignore[attr-defined]
    elif mode == "empty":
        capture.getBackendName = lambda: ""  # type: ignore[attr-defined]
    elif mode == "raises":

        def get_backend_name() -> str:
            raise RuntimeError("backend name unavailable")

        capture.getBackendName = get_backend_name  # type: ignore[attr-defined]

    fake_cv2 = SimpleNamespace(
        CAP_DSHOW=700,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6,
        VideoCapture=lambda _index, _api: capture,
    )
    monkeypatch.setattr(
        "maple_automation_core.capture.vc003_source.importlib.import_module",
        lambda _name: fake_cv2,
    )
    backend = OpenCVCaptureBackend(_config())
    if mode == "missing":
        assert not hasattr(capture, "getBackendName")
    with pytest.raises(RuntimeError, match="backend API"):
        backend.start()


@pytest.mark.parametrize(
    ("capture_factory", "message"),
    [
        (lambda: None, "open failed"),
        (lambda: SimpleNamespace(isOpened=lambda: False), "open failed"),
    ],
)
def test_opencv_backend_open_failure_releases_when_possible_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    capture_factory: Any,
    message: str,
) -> None:
    fake_cv2 = SimpleNamespace(CAP_DSHOW=700, VideoCapture=lambda _index, _api: capture_factory())
    monkeypatch.setattr(
        "maple_automation_core.capture.vc003_source.importlib.import_module",
        lambda _name: fake_cv2,
    )
    with pytest.raises(RuntimeError, match=message):
        OpenCVCaptureBackend(_config()).start()


@pytest.mark.parametrize(
    ("width", "height", "fps", "fourcc", "backend_name", "message"),
    [
        (3, 1, 30.0, "MJPG", "DSHOW", "negotiated dimensions"),
        (2, 1, 30.0, "YUY2", "DSHOW", "negotiated FourCC"),
        (2, 1, 30.0, "MJPG", "MSMF", "opened backend API"),
        (2, 1, 30.0, "MJPG", "NOTDSHOW", "opened backend API"),
    ],
)
def test_opencv_backend_rejects_dimension_fourcc_and_api_drift(
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    height: int,
    fps: float,
    fourcc: str,
    backend_name: str,
    message: str,
) -> None:
    class Capture:
        def isOpened(self) -> bool:
            return True

        def set(self, _prop: int, _value: float) -> bool:
            return True

        def get(self, prop: int) -> float:
            values = {
                3: float(width),
                4: float(height),
                5: fps,
                6: float(sum(ord(char) << (8 * index) for index, char in enumerate(fourcc))),
            }
            return values[prop]

        def getBackendName(self) -> str:
            return backend_name

        def release(self) -> None:
            pass

    fake_cv2 = SimpleNamespace(
        CAP_DSHOW=700,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6,
        VideoWriter_fourcc=lambda *chars: sum(
            ord(char) << (8 * index) for index, char in enumerate(chars)
        ),
        VideoCapture=lambda _index, _api: Capture(),
    )
    monkeypatch.setattr(
        "maple_automation_core.capture.vc003_source.importlib.import_module",
        lambda _name: fake_cv2,
    )
    with pytest.raises(RuntimeError, match=message):
        OpenCVCaptureBackend(_config()).start()


def test_opencv_backend_measurement_rejects_missing_or_incomplete_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Capture:
        def isOpened(self) -> bool:
            return True

        def set(self, _prop: int, _value: float) -> bool:
            return True

        def get(self, _prop: int) -> float:
            return 0.0

        def release(self) -> None:
            pass

    fake_cv2 = SimpleNamespace(
        CAP_DSHOW=700,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6,
        VideoCapture=lambda _index, _api: Capture(),
    )
    monkeypatch.setattr(
        "maple_automation_core.capture.vc003_source.importlib.import_module",
        lambda _name: fake_cv2,
    )
    with pytest.raises(RuntimeError, match="incomplete negotiated"):
        OpenCVCaptureBackend(_config()).start()

    class NonFiniteCapture(Capture):
        def get(self, prop: int) -> float:
            return float("nan") if prop == 5 else 1.0

    backend = OpenCVCaptureBackend(_config())
    backend._capture = NonFiniteCapture()
    backend._cv2 = fake_cv2
    backend._stopped = False
    with pytest.raises(RuntimeError, match="incomplete negotiated"):
        backend._measure_negotiated_facts()


def test_payload_parts_accepts_backend_frame_mapping_tuple_and_raw_sample_shapes() -> None:
    spec = RawFrameSpec(width=2, height=1)
    data = _bytes_frame(5)
    assert _payload_parts(BackendFrame(data, spec, 7)) == (data, spec, 7)
    assert _payload_parts({"raw_bytes": data, "spec": spec, "captured_at_ns": 8}) == (
        data,
        spec,
        8,
    )
    assert _payload_parts({"image_bytes": data})[0] == data
    assert _payload_parts({"bytes": data})[0] == data
    assert _payload_parts({"data": data})[0] == data
    assert _payload_parts({"frame": data})[0] == data
    with pytest.raises(ValueError, match="frame bytes/data"):
        _payload_parts({"spec": spec})
    assert _payload_parts((data, spec)) == (data, spec, None)
    assert _payload_parts((data, spec, 9)) == (data, spec, 9)
    assert _payload_parts((data, 10)) == (data, None, 10)
    assert _payload_parts((data, "not-a-spec")) == ((data, "not-a-spec"), None, None)
    assert _payload_parts((data,)) == ((data,), None, None)
    assert _payload_parts(data) == (data, None, None)

    source = VC003Source(_config(), backend=FakeBackend([data]))
    source.start()
    try:
        _wait_until(lambda: source.status().pending == 1)
        sample = source.read()
        assert sample is not None
        sample_data, sample_spec, sample_timestamp = _payload_parts(sample)
        assert sample_data == data
        assert sample_spec == spec
        assert sample_timestamp == sample.captured_at_ns
    finally:
        source.stop()


def test_coerce_bytes_and_spec_accepts_all_decoded_frame_representations() -> None:
    config = _config()
    data = _bytes_frame(6)
    spec = RawFrameSpec(width=2, height=1)
    assert _coerce_bytes_and_spec(data, config, None) == (data, spec)
    assert _coerce_bytes_and_spec(bytearray(data), config, None) == (data, spec)
    assert _coerce_bytes_and_spec(memoryview(data), config, None) == (data, spec)
    assert _coerce_bytes_and_spec(data, config, spec.to_dict()) == (data, spec)

    class Decoded:
        shape = (1, 2, 3)
        dtype = "uint8"

        def tobytes(self, **kwargs: Any) -> bytes:
            assert kwargs == {"order": "C"}
            return data

    assert _coerce_bytes_and_spec(Decoded(), config, None) == (data, spec)
    assert _coerce_bytes_and_spec(Decoded(), config, spec.to_dict()) == (data, spec)

    class Packed:
        def tobytes(self) -> bytes:
            return data

    assert _coerce_bytes_and_spec(Packed(), config, None) == (data, spec)
    with pytest.raises(ValueError, match="2-D or 3-D"):
        _coerce_bytes_and_spec(
            SimpleNamespace(shape=(1, 2, 3, 4), dtype="uint8", tobytes=lambda **_: data),
            config,
            None,
        )
    with pytest.raises(ValueError, match="BGR8 requires"):
        _coerce_bytes_and_spec(
            SimpleNamespace(shape=(1, 2), dtype="uint8", tobytes=lambda **_: b"xx"), config, None
        )
    with pytest.raises(TypeError, match="bytes or tobytes"):
        _coerce_bytes_and_spec(object(), config, None)
    with pytest.raises(ValueError, match="dimensions"):
        _coerce_bytes_and_spec(data, _config(width=3), spec)
    with pytest.raises(ValueError, match="does not match Pixel V1"):
        _coerce_bytes_and_spec(b"short", config, None)

    malformed = RawFrameSpec(width=2, height=1)
    object.__setattr__(malformed, "dtype", "uint16")
    with pytest.raises(ValueError, match="require uint8"):
        _coerce_bytes_and_spec(data, config, malformed)


@pytest.mark.parametrize(
    ("attribute", "value", "error"),
    [
        ("device_name", "", "device_name"),
        ("device_name", 3, "device_name"),
        ("device_fingerprint_sha256", "A" * 64, "fingerprint"),
        ("device_fingerprint_sha256", "z" * 64, "hexadecimal"),
    ],
)
def test_backend_contract_rejects_unbound_identity_values(
    attribute: str,
    value: Any,
    error: str,
) -> None:
    backend = FakeBackend([])
    setattr(backend, attribute, value)
    source = VC003Source(_config(), backend=backend)
    with pytest.raises((TypeError, RuntimeError), match=error):
        source.start()
    assert backend.started == 1
    assert backend.stopped == 1


def test_backend_contract_rejects_facts_that_do_not_match_config() -> None:
    backend = FakeBackend([])
    backend.negotiated_facts = replace(backend.negotiated_facts, backend="msmf")
    source = VC003Source(_config(), backend=backend)
    with pytest.raises(RuntimeError, match="contradict source config"):
        source.start()
    assert backend.stopped == 1


def test_external_provenance_freezes_configuration_and_checks_measured_facts() -> None:
    template = VC003Source(_config(), backend=FakeBackend([])).provenance
    measured = FakeBackend([]).negotiated_facts.to_format_dict()
    external = replace(template, negotiated=measured, backend_version="other-version")
    source = VC003Source(_config(), backend=FakeBackend([]), provenance=external)
    with pytest.raises(RuntimeError, match="freezes"):
        source.configure(_config(session_id="session-b"))
    with pytest.raises(RuntimeError, match="contradict"):
        source.start()


def test_backend_factory_requires_new_weakrefable_instances_and_methods() -> None:
    class NoWeakReferenceBackend:
        __slots__ = ("device_fingerprint_sha256", "device_name", "negotiated_facts")

        def __init__(self) -> None:
            self.device_name = "VC-003 Video"
            self.device_fingerprint_sha256 = UNKNOWN_DEVICE_FINGERPRINT_SHA256
            self.negotiated_facts = FakeBackend([]).negotiated_facts

        def start(self) -> None:
            pass

        def read(self) -> Any | None:
            return None

        def stop(self) -> None:
            pass

    with pytest.raises(TypeError, match="weak references"):
        VC003Source(_config(), backend_factory=lambda _config: NoWeakReferenceBackend()).start()

    class MissingReadBackend:
        device_name = "VC-003 Video"
        device_fingerprint_sha256 = UNKNOWN_DEVICE_FINGERPRINT_SHA256
        negotiated_facts = FakeBackend([]).negotiated_facts

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    with pytest.raises(TypeError, match="implement read"):
        VC003Source(_config(), backend_factory=lambda _config: MissingReadBackend()).start()


def test_source_error_tokens_and_backend_shutdown_outcomes() -> None:
    source = VC003Source(_config(), backend=FakeBackend([]))
    source._attempt_token = 1
    source._set_attempt_error(0, "stale")
    assert source.status().error is None
    source._set_error("first")
    source._set_error("second")
    assert source.status().error == "first"

    source._backend_stopped = False
    source._backend = None
    assert source._stop_backend_once() is True
    assert source.status().lifecycle == "error"
    assert source._stop_backend_once(token=0) is False

    entered = threading.Event()
    release = threading.Event()

    class BlockingStopBackend(FakeBackend):
        def stop(self) -> None:
            entered.set()
            release.wait(1.0)

    blocking = BlockingStopBackend([])
    waiting = VC003Source(_config(), backend=blocking)
    waiting._backend = blocking
    waiting._backend_stopped = False
    assert waiting._stop_backend_once(timeout_s=0.001) is False
    assert entered.wait(1.0)
    release.set()
    helper = waiting._backend_stop_thread
    assert helper is not None
    helper.join(timeout=1.0)
    assert not helper.is_alive()
    assert waiting.status().error is not None

    class FailingStopBackend(FakeBackend):
        def stop(self) -> None:
            raise OSError("shutdown failed")

    failing = FailingStopBackend([])
    failed = VC003Source(_config(), backend=failing)
    failed._backend = failing
    failed._backend_stopped = False
    assert failed._stop_backend_once(timeout_s=1.0) is False
    assert failed.status().error is not None
    assert "shutdown failed" in failed.status().error


def test_open_backend_cancellation_does_not_attach_late_backend() -> None:
    backend = FakeBackend([])
    source = VC003Source(_config(), backend_factory=lambda _config: backend)
    source._attempt_token = 1
    source._stop_event.set()
    source._open_backend(0)
    assert source._backend is None
    assert backend.started == 0
    assert backend.stopped == 0


def test_external_provenance_accepts_matching_measured_facts() -> None:
    backend = FakeBackend([])
    template = VC003Source(_config(), backend=FakeBackend([])).provenance
    external = replace(
        template,
        negotiated=backend.negotiated_facts.to_format_dict(),
        backend_version=backend.negotiated_facts.backend_version,
    )
    source = VC003Source(_config(), backend=backend, provenance=external)
    source.start()
    assert source.negotiated_facts == backend.negotiated_facts
    source.stop()


def test_open_backend_can_be_cancelled_after_attachment() -> None:
    backend = FakeBackend([])
    source = VC003Source(_config(), backend_factory=lambda _config: backend)
    source._attempt_token = 1
    source._stop_event.set()
    source._open_backend(1)
    assert source._backend is backend
    assert backend.started == 0
    assert backend.stopped == 1
    assert source._backend_stopped


def test_worker_finally_marks_clean_exit_when_already_stopped() -> None:
    source = VC003Source(_config(), backend=FakeBackend([]))
    source._lifecycle = "running"
    source._thread = threading.current_thread()
    stop_event = threading.Event()
    stop_event.set()
    source._run(source._backend_factory(source.config), 0, stop_event)
    assert source.status().lifecycle == "stopped"


def test_stop_of_never_started_source_is_a_clean_terminal_transition() -> None:
    source = VC003Source(_config(), backend_factory=lambda _config: FakeBackend([]))
    assert source.stop() is None
    status = source.status()
    assert status.lifecycle == "stopped"
    assert status.final_drain_performed


def test_single_flight_stop_controller_reports_lock_contention() -> None:
    class NeverAcquired:
        def acquire(self, timeout: float | None = None) -> bool:
            return False

        def release(self) -> None:
            pass

    source = VC003Source(_config(), backend=FakeBackend([]))
    source._stop_controller_lock = NeverAcquired()  # type: ignore[assignment]
    assert source.stop() is None
    assert source.status().error is not None
    assert "stop controller" in source.status().error


def test_reset_validates_backend_mode_provenance_and_quiescent_consumer() -> None:
    singleton = VC003Source(_config(), backend=FakeBackend([]))
    with pytest.raises(RuntimeError, match="factory"):
        singleton.reset("session-b")

    template = VC003Source(_config(), backend=FakeBackend([])).provenance
    external = VC003Source(
        _config(), backend_factory=lambda _config: FakeBackend([]), provenance=template
    )
    with pytest.raises(ValueError, match="new-session provenance"):
        external.reset("session-b")

    source = VC003Source(_config(), backend_factory=lambda _config: FakeBackend([]))
    invalid = replace(source.provenance, session_id="wrong-session")
    with pytest.raises(ValueError, match="new session/config"):
        source.reset("session-b", provenance=invalid)

    class NeverAcquired:
        def acquire(self, timeout: float | None = None) -> bool:
            return False

        def release(self) -> None:
            pass

    source._consumer_transaction_lock = NeverAcquired()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="quiescent consumer"):
        source.reset("session-b")
    assert source.status().error is not None


def test_read_rejects_a_cas_object_that_does_not_round_trip(tmp_path: Path) -> None:
    class MismatchStore(PixelStore):
        def read(self, _digest: str, _spec: Any = None) -> bytes:
            return b"wrong-pixels"

    source = VC003Source(
        _config(),
        backend=FakeBackend([_bytes_frame(1)]),
        pixel_store=MismatchStore(tmp_path / "cas-mismatch"),
    )
    source.start()
    try:
        _wait_until(lambda: source.status().pending == 1)
        with pytest.raises(VC003SourceError, match="CAS admission failed"):
            source.read()
        status = source.status()
        assert status.discarded_on_error == 1
        assert status.accounting_holds
    finally:
        source.stop()


def test_final_drain_cas_failure_is_recorded_as_terminal_error(tmp_path: Path) -> None:
    class FailingStore(PixelStore):
        def put_artifact(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("final drain CAS failure")

    source = VC003Source(
        _config(),
        backend=FakeBackend([_bytes_frame(1)]),
        pixel_store=FailingStore(tmp_path / "final-drain-cas"),
    )
    source.start()
    _wait_until(lambda: source.status().pending == 1)
    assert source.stop() is None
    status = source.status()
    assert status.lifecycle == "error"
    assert status.error is not None
    assert "pixel CAS admission failed" in status.error
    assert status.discarded_on_error == 1


def test_source_start_running_error_stopped_and_active_worker_states() -> None:
    source = VC003Source(_config(), backend=FakeBackend([]))
    source.start()
    assert source.is_running
    source.start()
    source.stop()
    assert not source.is_running
    with pytest.raises(RuntimeError, match="after stop"):
        source.start()

    errored = VC003Source(_config(), backend=FakeBackend([]))
    errored._lifecycle = "error"
    errored._error = "synthetic"
    with pytest.raises(RuntimeError, match="after a source error"):
        errored.start()

    active = VC003Source(_config(), backend=FakeBackend([]))
    release = threading.Event()
    worker = threading.Thread(target=release.wait)
    worker.start()
    active._thread = worker
    with pytest.raises(RuntimeError, match="still active"):
        active.start()
    release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()


def test_source_read_timeout_forms_and_terminal_condition_paths() -> None:
    source = VC003Source(_config(), backend=FakeBackend([]))
    source.start()
    try:
        assert source.read(timeout_ms=1) is None
        assert source.read(timeout_s=0) is None
        assert source.read(timeout_ns=0) is None
        assert source.read(deadline_ns=time.monotonic_ns() - 1) is None
        cancelled = threading.Event()
        cancelled.set()
        assert source.read(timeout=0.1, cancel_event=cancelled) is None
        with pytest.raises(ValueError, match="only one timeout"):
            source.read(timeout_ms=1, timeout_s=1)
        with pytest.raises(ValueError, match="timeout_ms"):
            source.read(timeout_ms=True)
        with pytest.raises(ValueError, match="timeout_ns"):
            source.read(timeout_ns=True)
        with pytest.raises(ValueError, match="deadline_ns"):
            source.read(deadline_ns=True)
        with pytest.raises(ValueError, match="timeout must"):
            source.read(timeout=-1)
        with pytest.raises(ValueError, match="timeout must"):
            source.read(timeout=float("nan"))
        with pytest.raises(ValueError, match="timeout must"):
            source.read(timeout=float("inf"))
        for huge_timeout in (10**1000, -(10**1000)):
            with pytest.raises(ValueError, match="timeout"):
                source.read(timeout=huge_timeout)
        with pytest.raises(ValueError, match="timeout_ms"):
            source.read(timeout_ms=10**1000)
        with pytest.raises(ValueError, match="timeout_ns"):
            source.read(timeout_ns=10**1000)
        with pytest.raises(ValueError, match="deadline_ns"):
            source.read(deadline_ns=10**1000)
        with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
            source.read(timeout=threading.TIMEOUT_MAX + 1.0)
        with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
            source.read(timeout_s=1e100)
        with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
            source.read(timeout_ms=(threading.TIMEOUT_MAX * 1000.0) + 1.0)
        with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
            source.read(timeout_ns=int(threading.TIMEOUT_MAX * 1_000_000_000) + 1_000_000_000)
        with pytest.raises(ValueError, match="threading.TIMEOUT_MAX"):
            source.read(
                deadline_ns=(
                    time.monotonic_ns() + int(threading.TIMEOUT_MAX * 1_000_000_000) + 1_000_000_000
                )
            )
        with pytest.raises(TypeError, match="cancel_event"):
            source.read(cancel_event=object())
    finally:
        source.stop()

    source = VC003Source(_config(), backend=FakeBackend([]))
    source._set_error("already terminal")
    with pytest.raises(VC003SourceError, match="already terminal"):
        source.read()

    delayed_error = VC003Source(_config(), backend=FakeBackend([]))

    def fail_later() -> None:
        time.sleep(0.005)
        delayed_error._set_error("late terminal")

    setter = threading.Thread(target=fail_later)
    setter.start()
    with pytest.raises(VC003SourceError, match="late terminal"):
        delayed_error.read(timeout=0.1)
    setter.join(timeout=1)


def test_capture_worker_rejects_fact_drift_before_read_and_after_timestamp() -> None:
    class DriftBeforeRead(FakeBackend):
        @property
        def negotiated_facts(self) -> NegotiatedCaptureFacts:
            self.fact_reads += 1
            return replace(self._facts, fourcc="YUY2") if self.fact_reads >= 2 else self._facts

        @negotiated_facts.setter
        def negotiated_facts(self, value: NegotiatedCaptureFacts) -> None:
            self._facts = value
            self.fact_reads = getattr(self, "fact_reads", 0)

    before = DriftBeforeRead([])
    source = VC003Source(_config(), backend=before)
    source.start()
    try:
        _wait_until(lambda: source.status().lifecycle == "error")
        assert source.status().error is not None
        assert "format/identity drift" in source.status().error
        assert before.reads == 0
    finally:
        source.stop()

    class DriftAfterRead(FakeBackend):
        @property
        def negotiated_facts(self) -> NegotiatedCaptureFacts:
            self.fact_reads += 1
            return replace(self._facts, fourcc="YUY2") if self.fact_reads >= 3 else self._facts

        @negotiated_facts.setter
        def negotiated_facts(self, value: NegotiatedCaptureFacts) -> None:
            self._facts = value
            self.fact_reads = getattr(self, "fact_reads", 0)

    after = DriftAfterRead([_bytes_frame(1)])
    source = VC003Source(_config(), backend=after)
    source.start()
    try:
        _wait_until(lambda: source.status().lifecycle == "error")
        assert source.status().produced == 0
        assert source.status().error is not None
        assert "format/identity drift" in source.status().error
    finally:
        source.stop()


@pytest.mark.parametrize(
    ("payload", "clock", "message"),
    [
        (BackendFrame(_bytes_frame(1)), lambda: True, "captured_at_ns"),
        (BackendFrame(_bytes_frame(1), captured_at_ns=True), lambda: 1, "backend captured_at_ns"),
    ],
)
def test_capture_worker_rejects_invalid_timestamp_values(
    payload: BackendFrame,
    clock: Any,
    message: str,
) -> None:
    source = VC003Source(_config(), backend=FakeBackend([payload]), clock=clock)
    source.start()
    try:
        _wait_until(lambda: source.status().lifecycle == "error")
        status = source.status()
        assert status.produced == 0
        assert status.decode_rejection_count == 1
        assert status.error is not None
        assert message in status.error
    finally:
        source.stop()


def test_publish_discards_payload_from_an_old_generation() -> None:
    source = VC003Source(_config(), backend=FakeBackend([]))
    source._generation = 2
    source._publish(1, 0, _bytes_frame(1), RawFrameSpec(width=2, height=1))
    assert source.status().produced == 0
    assert source.status().pending == 0
