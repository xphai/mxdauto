from __future__ import annotations

import threading

import pytest

from maple_automation_core.capture import (
    FrameAdmissionStatus,
    FrameSourceAdapter,
    FrameSourceConfig,
    LatestFrameBuffer,
    RawFrame,
)
from maple_automation_core.domain.frame import FrameSize, SourceGeometry, SourceRect


def _geometry(width: int = 1920, height: int = 1080) -> SourceGeometry:
    return SourceGeometry(
        source_size=FrameSize(width=width, height=height),
        content_rect=SourceRect(x=0, y=0, width=width, height=height),
        working_size=FrameSize(width=640, height=360),
    )


def _config() -> FrameSourceConfig:
    return FrameSourceConfig(
        session_id="s1",
        source_id="camera",
        clock_domain="mono",
        transform_version="v1",
        source_geometry=_geometry(),
        max_age_ns=100,
    )


def _raw(
    frame_id: int,
    captured_at_ns: int = 0,
    *,
    source_id: str | None = "camera",
    session_id: str | None = "s1",
    clock_domain: str | None = "mono",
    transform_version: str | None = "v1",
    geometry: SourceGeometry | None = None,
) -> RawFrame:
    return RawFrame(
        source_id=source_id,
        session_id=session_id,
        frame_id=frame_id,
        captured_at_ns=captured_at_ns,
        clock_domain=clock_domain,
        transform_version=transform_version,
        source_geometry=_geometry() if geometry is None else geometry,
        content_hash=f"{frame_id + 1:064x}",
        image_ref=f"frame://{frame_id}",
    )


class _Source:
    def __init__(self, value: object) -> None:
        self.value = value

    def read(self) -> object:
        return self.value


class _RaisingSource:
    def read(self) -> RawFrame:
        raise RuntimeError("synthetic source failure")


class _BlockingSequenceSource:
    def __init__(self, *frames: RawFrame) -> None:
        self.frames = list(frames)
        self.first_started = threading.Event()
        self.second_started = threading.Event()
        self.release_first = threading.Event()
        self._calls = 0
        self._lock = threading.Lock()

    def read(self) -> RawFrame | None:
        with self._lock:
            call_index = self._calls
            self._calls += 1
            frame = self.frames.pop(0) if self.frames else None
        if call_index == 0:
            self.first_started.set()
            assert self.release_first.wait(2)
        elif call_index == 1:
            self.second_started.set()
        return frame


class _BlockingClock:
    def __init__(self, value: int) -> None:
        self.value = value
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> int:
        self.started.set()
        assert self.release.wait(2)
        return self.value


def test_fatal_faults_latch_original_result_until_reset_session() -> None:
    adapter = FrameSourceAdapter(_Source(_raw(0)), _config(), clock=lambda: 10)
    assert adapter.ingest(_raw(2), 10).status is FrameAdmissionStatus.ACCEPTED
    duplicate = adapter.ingest(_raw(2), 11)
    assert duplicate.status is FrameAdmissionStatus.DUPLICATE
    assert duplicate.plan_suppressed is True
    assert duplicate.fault_latched is True
    assert adapter.poll(99) is duplicate
    assert adapter.ingest(_raw(3), 99) is duplicate

    adapter.reset_session("s2")
    recovered = adapter.ingest(_raw(0, session_id="s2"), 100)
    assert recovered.status is FrameAdmissionStatus.ACCEPTED
    assert recovered.packet is not None
    assert recovered.packet.session_id == "s2"


def test_reset_requires_a_distinct_explicit_session() -> None:
    adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 10)
    assert adapter.ingest(_raw(0), 10).accepted
    duplicate = adapter.ingest(_raw(0), 11)
    assert duplicate.fault_latched

    with pytest.raises(TypeError):
        adapter.reset_session()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="must differ"):
        adapter.reset_session("s1")
    assert adapter.fault_latched
    assert adapter.poll(12) is duplicate


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (_raw(0, source_id="other"), FrameAdmissionStatus.SOURCE_MISMATCH),
        (_raw(0, session_id="other"), FrameAdmissionStatus.SESSION_MISMATCH),
        (_raw(0, clock_domain="wall"), FrameAdmissionStatus.CLOCK_DOMAIN_MISMATCH),
        (_raw(0, transform_version="v2"), FrameAdmissionStatus.SOURCE_MISMATCH),
    ],
)
def test_identity_mismatches_are_fatal(raw: RawFrame, status: FrameAdmissionStatus) -> None:
    adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 10)
    result = adapter.ingest(raw, 10)
    assert result.status is status
    assert result.fault_latched is True
    assert result.plan_suppressed is True
    assert adapter.poll(20) is result


def test_frame_size_change_latches_until_new_session() -> None:
    adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 10)
    changed = adapter.ingest(_raw(0, geometry=_geometry(1280, 720)), 10)
    assert changed.status is FrameAdmissionStatus.FRAME_SIZE_CHANGED
    assert changed.event.expected_source_size == FrameSize(width=1920, height=1080)
    assert changed.event.actual_source_size == FrameSize(width=1280, height=720)
    assert adapter.ingest(_raw(1), 11) is changed

    adapter.reset_session("s2")
    assert adapter.ingest(_raw(0, session_id="s2"), 20).accepted


def test_geometry_calibration_change_with_same_size_is_source_mismatch() -> None:
    adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 10)
    changed = SourceGeometry(
        source_size=FrameSize(width=1920, height=1080),
        content_rect=SourceRect(x=1, y=0, width=1919, height=1080),
        working_size=FrameSize(width=640, height=360),
    )
    result = adapter.ingest(_raw(0, geometry=changed), 10)
    assert result.status is FrameAdmissionStatus.SOURCE_MISMATCH
    assert result.fault_latched is True


def test_out_of_order_and_timestamp_regression_latch() -> None:
    adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 100)
    assert adapter.ingest(_raw(5, 90), 100).accepted
    out_of_order = adapter.ingest(_raw(4, 91), 101)
    assert out_of_order.status is FrameAdmissionStatus.OUT_OF_ORDER
    assert adapter.ingest(_raw(6, 92), 102) is out_of_order

    adapter.reset_session("s2")
    assert adapter.ingest(_raw(1, 90, session_id="s2"), 100).accepted
    regression = adapter.ingest(_raw(2, 89, session_id="s2"), 101)
    assert regression.status is FrameAdmissionStatus.TIMESTAMP_REGRESSION
    assert regression.fault_latched


def test_future_timestamp_is_timestamp_regression_and_source_errors_latch() -> None:
    adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 10)
    future = adapter.ingest(_raw(0, 11), 10)
    assert future.status is FrameAdmissionStatus.TIMESTAMP_REGRESSION
    assert adapter.poll(20) is future

    source_error_adapter = FrameSourceAdapter(_RaisingSource(), _config(), clock=lambda: 10)
    error = source_error_adapter.poll()
    assert error.status is FrameAdmissionStatus.SOURCE_ERROR
    assert error.event.details["exception_type"] == "RuntimeError"
    assert source_error_adapter.poll(11) is error


def test_no_frame_and_stale_are_transient() -> None:
    adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 10)
    no_frame = adapter.poll()
    assert no_frame.status is FrameAdmissionStatus.NO_FRAME
    assert not no_frame.fault_latched
    # A non-negative timestamp beyond max_age exercises stale without using
    # an invalid RawFrame constructor value.
    stale_result = adapter.ingest(_raw(0, 0), 101)
    assert stale_result.status is FrameAdmissionStatus.STALE
    assert not stale_result.fault_latched
    assert adapter.ingest(_raw(0, 0), 101).status is FrameAdmissionStatus.STALE


def test_every_observation_participates_in_clock_rollback_latch() -> None:
    adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 100)
    assert adapter.ingest(None, 100).status is FrameAdmissionStatus.NO_FRAME
    rollback = adapter.ingest(_raw(0, 50), 50)
    assert rollback.status is FrameAdmissionStatus.TIMESTAMP_REGRESSION
    assert rollback.event.reason == "receive clock moved backwards"
    assert rollback.event.details["previous_observed_at_ns"] == 100
    assert rollback.fault_latched

    latest_adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 100)
    assert latest_adapter.ingest(_raw(0, 100), 100).accepted
    assert latest_adapter.read_latest(99) is None
    assert latest_adapter.fault_latched
    assert latest_adapter.poll(101).status is FrameAdmissionStatus.TIMESTAMP_REGRESSION

    stale_adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 101)
    assert stale_adapter.ingest(_raw(0, 0), 101).status is FrameAdmissionStatus.STALE
    assert stale_adapter.ingest(_raw(0, 100), 100).fault_latched


def test_poll_serializes_source_reads_and_reset_waits_for_inflight_frame() -> None:
    source = _BlockingSequenceSource(_raw(0, 100), _raw(1, 101))
    adapter = FrameSourceAdapter(source, _config(), clock=lambda: 100)
    results: list[FrameAdmissionStatus] = []

    first = threading.Thread(target=lambda: results.append(adapter.poll(100).status))
    second = threading.Thread(target=lambda: results.append(adapter.poll(101).status))
    first.start()
    assert source.first_started.wait(1)
    second.start()
    assert not source.second_started.wait(0.05)
    source.release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert results == [FrameAdmissionStatus.ACCEPTED, FrameAdmissionStatus.ACCEPTED]
    assert adapter.last_accepted_frame_id == 1
    assert not adapter.fault_latched

    reset_source = _BlockingSequenceSource(_raw(0, 100))
    reset_adapter = FrameSourceAdapter(reset_source, _config(), clock=lambda: 100)
    poll_done = threading.Event()
    reset_done = threading.Event()
    poll_thread = threading.Thread(target=lambda: (reset_adapter.poll(100), poll_done.set()))
    reset_thread = threading.Thread(
        target=lambda: (reset_adapter.reset_session("s2"), reset_done.set())
    )
    poll_thread.start()
    assert reset_source.first_started.wait(1)
    reset_thread.start()
    assert not reset_done.wait(0.05)
    reset_source.release_first.set()
    poll_thread.join(2)
    reset_thread.join(2)

    assert poll_done.is_set()
    assert reset_done.is_set()
    assert reset_adapter.session_id == "s2"
    assert reset_adapter.last_accepted_frame_id is None
    assert reset_adapter.read_latest(100) is None
    assert not reset_adapter.fault_latched

    blocking_clock = _BlockingClock(100)
    ingest_adapter = FrameSourceAdapter(_Source(None), _config(), clock=blocking_clock)
    ingest_done = threading.Event()
    ingest_reset_done = threading.Event()
    ingest_thread = threading.Thread(
        target=lambda: (ingest_adapter.ingest(_raw(0)), ingest_done.set())
    )
    ingest_reset_thread = threading.Thread(
        target=lambda: (ingest_adapter.reset_session("s2"), ingest_reset_done.set())
    )
    ingest_thread.start()
    assert blocking_clock.started.wait(1)
    ingest_reset_thread.start()
    assert not ingest_reset_done.wait(0.05)
    blocking_clock.release.set()
    ingest_thread.join(2)
    ingest_reset_thread.join(2)

    assert ingest_done.is_set()
    assert ingest_reset_done.is_set()
    assert ingest_adapter.session_id == "s2"
    assert not ingest_adapter.fault_latched


def test_concurrent_latest_buffer_reads_are_consistent() -> None:
    adapter = FrameSourceAdapter(_Source(None), _config(), clock=lambda: 100)
    packet = adapter.ingest(_raw(0), 100).packet
    assert packet is not None
    buffer = LatestFrameBuffer()
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            for _ in range(100):
                buffer.publish(packet)
        except BaseException as exc:  # pragma: no cover - diagnostic guard
            errors.append(exc)

    def read() -> None:
        try:
            for _ in range(100):
                assert buffer.read_latest(100) in (None, packet)
        except BaseException as exc:  # pragma: no cover - diagnostic guard
            errors.append(exc)

    threads = [threading.Thread(target=publish), threading.Thread(target=read)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert buffer.read_latest(100) == packet
