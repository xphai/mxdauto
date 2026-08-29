from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from maple_automation_core.capture import (
    FrameAdmissionStatus,
    FrameSourceAdapter,
    FrameSourceConfig,
    LatestFrameBuffer,
    RawFrame,
    canonical_calibration_sha256,
    canonical_geometry_sha256,
)
from maple_automation_core.domain.frame import (
    FramePacket,
    FrameSize,
    SourceGeometry,
    SourceRect,
)


def _geometry() -> SourceGeometry:
    # DEC-001 capture card crop and processing dimensions.
    return SourceGeometry(
        source_size=FrameSize(width=1920, height=1080),
        content_rect=SourceRect(x=277, y=167, width=1366, height=768),
        working_size=FrameSize(width=1296, height=700),
    )


def _config(*, max_age_ns: int = 100) -> FrameSourceConfig:
    return FrameSourceConfig(
        session_id="session-1",
        source_id="vc-003",
        clock_domain="monotonic",
        transform_version="calibration-v1",
        source_geometry=_geometry(),
        max_age_ns=max_age_ns,
    )


def _raw(
    frame_id: int,
    captured_at_ns: int = 100,
    *,
    source_id: str | None = "vc-003",
    session_id: str | None = "session-1",
    clock_domain: str | None = "monotonic",
    transform_version: str | None = "calibration-v1",
    geometry: SourceGeometry | None = None,
    source_size: FrameSize | None = None,
) -> RawFrame:
    actual_geometry = _geometry() if geometry is None else geometry
    return RawFrame(
        source_id=source_id,
        session_id=session_id,
        frame_id=frame_id,
        captured_at_ns=captured_at_ns,
        clock_domain=clock_domain,
        transform_version=transform_version,
        source_geometry=actual_geometry,
        source_size=source_size,
        content_hash=f"{frame_id + 1:064x}",
        image_ref=f"frame://{frame_id}",
        image_metadata={"frame_id": frame_id, "tags": ["fixture"]},
    )


class _QueueSource:
    def __init__(self, *frames: RawFrame | None) -> None:
        self.frames = list(frames)

    def read(self) -> RawFrame | None:
        return self.frames.pop(0) if self.frames else None


def test_geometry_and_calibration_hashes_are_canonical() -> None:
    geometry = _geometry()
    first = canonical_geometry_sha256(geometry)
    second = canonical_geometry_sha256(SourceGeometry.from_dict(geometry.to_dict()))
    assert first == second
    assert (
        first
        == FrameSourceConfig(
            "session-1", "vc-003", "monotonic", "calibration-v1", geometry, 100
        ).geometry_hash
    )
    assert canonical_calibration_sha256(geometry, "v1") != canonical_calibration_sha256(
        geometry, "v2"
    )
    assert _config().calibration_hash == canonical_calibration_sha256(geometry, "calibration-v1")


def test_raw_and_config_are_immutable_and_roundtrip_through_json() -> None:
    raw = _raw(0)
    raw_data = json.loads(json.dumps(raw.to_dict()))
    assert RawFrame.from_dict(raw_data) == raw
    with pytest.raises(FrozenInstanceError):
        raw.frame_id = 2  # type: ignore[misc]
    with pytest.raises((TypeError, AttributeError)):
        raw.image_metadata["new"] = True  # type: ignore[index]

    config = _config()
    assert FrameSourceConfig.from_dict(json.loads(json.dumps(config.to_dict()))) == config


def test_adapter_accepts_packet_and_replaces_latest_atomically() -> None:
    source = _QueueSource(_raw(0), _raw(1, captured_at_ns=150))
    adapter = FrameSourceAdapter(source, _config(), clock=lambda: 200)

    first = adapter.poll()
    second = adapter.poll()
    assert first.status is FrameAdmissionStatus.ACCEPTED
    assert second.status is FrameAdmissionStatus.ACCEPTED
    assert first.packet is not None
    assert second.packet is not None
    assert second.event.superseded_count == 1
    assert adapter.superseded_count == 1
    assert adapter.read_latest(200) == second.packet
    assert adapter.last_accepted_frame_id == 1
    assert adapter.last_accepted == second.packet


def test_adapter_reports_gap_but_admits_frame() -> None:
    adapter = FrameSourceAdapter(_QueueSource(), _config(), clock=lambda: 200)
    assert adapter.ingest(_raw(2), 200).status is FrameAdmissionStatus.ACCEPTED
    result = adapter.ingest(_raw(5, captured_at_ns=201), 201)
    assert result.status is FrameAdmissionStatus.ACCEPTED
    assert result.event.gap_detected is True
    assert result.event.missing_frame_count == 2
    assert result.plan_suppressed is False


def test_stale_is_transient_and_exact_age_boundary_is_fresh() -> None:
    adapter = FrameSourceAdapter(_QueueSource(), _config(max_age_ns=100), clock=lambda: 200)
    stale = adapter.ingest(_raw(0, captured_at_ns=99), 200)
    assert stale.status is FrameAdmissionStatus.STALE
    assert stale.plan_suppressed is True
    assert stale.fault_latched is False
    assert adapter.fault_latched is False
    assert adapter.last_accepted_frame_id is None

    boundary_adapter = FrameSourceAdapter(
        _QueueSource(), _config(max_age_ns=100), clock=lambda: 200
    )
    assert boundary_adapter.ingest(_raw(0, captured_at_ns=100), 200).accepted


def test_latest_expires_at_read_time() -> None:
    adapter = FrameSourceAdapter(_QueueSource(), _config(max_age_ns=10), clock=lambda: 100)
    accepted = adapter.ingest(_raw(0, captured_at_ns=90), 100)
    assert accepted.accepted
    assert adapter.read_latest(100) == accepted.packet
    # At age 11 the one-slot read expires the packet atomically.
    assert adapter.read_latest(101) is None
    assert adapter.read_latest(101) is None


def test_frame_source_statuses_are_immutable_and_roundtrip() -> None:
    adapter = FrameSourceAdapter(_QueueSource(), _config(), clock=lambda: 200)
    result = adapter.ingest(_raw(0), 200)
    data = json.loads(json.dumps(result.to_dict()))
    hydrated = result.from_dict(data)
    assert hydrated == result
    with pytest.raises(FrozenInstanceError):
        result.status = FrameAdmissionStatus.STALE  # type: ignore[misc]


def test_latest_buffer_counts_only_replacements() -> None:
    buffer = LatestFrameBuffer()
    adapter = FrameSourceAdapter(_QueueSource(), _config(), clock=lambda: 200)
    packet = adapter.ingest(_raw(0), 200).packet
    assert isinstance(packet, FramePacket)
    assert buffer.superseded_count == 0
    assert buffer.publish(packet) == 0
    assert buffer.publish(packet) == 1
    assert buffer.read_latest(200) == packet
    buffer.clear()
    assert buffer.read_latest() is None
    assert buffer.superseded_count == 1
