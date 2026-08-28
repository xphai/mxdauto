from __future__ import annotations

import json

import pytest

from maple_automation_core.domain.frame import (
    CaptureHealth,
    FramePacket,
    FrameSize,
    SourceGeometry,
    SourceRect,
)


def _geometry() -> SourceGeometry:
    return SourceGeometry(
        source_size=FrameSize(width=1920, height=1080),
        content_rect=SourceRect(x=10, y=20, width=1800, height=1000),
        working_size=FrameSize(width=1800, height=1000),
    )


def _health() -> CaptureHealth:
    return CaptureHealth(
        session_id="session-1",
        frame_id=7,
        source_id="main",
        content_hash="e" * 64,
        clock_domain="monotonic",
        captured_at_ns=1000,
        received_at_ns=2000,
        transform_version="v1",
        max_age_ns=5000,
    )


def test_source_geometry_valid_and_roundtrip() -> None:
    geometry = _geometry()
    assert geometry.to_dict() == {
        "source_size": {"width": 1920, "height": 1080},
        "content_rect": {"x": 10, "y": 20, "width": 1800, "height": 1000},
        "working_size": {"width": 1800, "height": 1000},
    }

    assert SourceGeometry.from_dict(geometry.to_dict()) == geometry


def test_source_geometry_invalid_values() -> None:
    with pytest.raises(ValueError):
        SourceGeometry(
            source_size=FrameSize(width=100, height=100),
            content_rect=SourceRect(x=101, y=0, width=1, height=1),
            working_size=FrameSize(width=100, height=100),
        )


def test_source_geometry_allows_independent_working_resize() -> None:
    geometry = SourceGeometry(
        source_size=FrameSize(width=1920, height=1080),
        content_rect=SourceRect(x=277, y=156, width=1366, height=768),
        working_size=FrameSize(width=1296, height=700),
    )
    assert geometry.working_size == FrameSize(width=1296, height=700)


def test_source_geometry_exposes_anisotropic_downsample_mapping() -> None:
    geometry = _geometry()
    assert geometry.content_size == FrameSize(width=1800, height=1000)
    assert geometry.scale_x == 1.0
    assert geometry.scale_y == 1.0
    assert geometry.downsample == (1.0, 1.0)
    assert geometry.content_to_working(90, 80) == (90.0, 80.0)
    assert geometry.working_to_source(90, 80) == (100.0, 100.0)

    resized = SourceGeometry(
        source_size=FrameSize(width=1920, height=1080),
        content_rect=SourceRect(x=277, y=156, width=1366, height=768),
        working_size=FrameSize(width=1296, height=700),
    )
    assert resized.downsample_x == pytest.approx(1366 / 1296)
    assert resized.downsample_y == pytest.approx(768 / 700)
    assert resized.source_to_working(277, 156) == (0.0, 0.0)
    assert resized.working_to_source(1296, 700) == pytest.approx((1643.0, 924.0))


def test_frame_metadata_is_strict_and_deeply_immutable() -> None:
    geometry = SourceGeometry(
        source_size=FrameSize(width=10, height=10),
        content_rect=SourceRect(x=0, y=0, width=10, height=10),
        working_size=FrameSize(width=5, height=5),
    )
    metadata = {"nested": {"items": [1, 2]}}
    frame = FramePacket(
        source_id="main",
        session_id="session-1",
        frame_id=1,
        captured_at_ns=0,
        received_at_ns=1,
        transform_version="v1",
        clock_domain="monotonic",
        content_hash="e" * 64,
        source_geometry=geometry,
        image_ref="frame://1",
        capture_health=CaptureHealth(
            session_id="session-1",
            frame_id=1,
            source_id="main",
            content_hash="e" * 64,
            clock_domain="monotonic",
            captured_at_ns=0,
            received_at_ns=1,
            transform_version="v1",
            max_age_ns=10,
        ),
        image_metadata=metadata,
    )
    metadata["nested"]["items"].append(3)
    assert frame.to_dict()["image_metadata"] == {"nested": {"items": [1, 2]}}
    with pytest.raises(TypeError):
        frame.image_metadata["new"] = True  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        frame.image_metadata["nested"]["items"].append(3)  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        FramePacket(
            source_id="main",
            session_id="session-1",
            frame_id=1,
            captured_at_ns=0,
            received_at_ns=1,
            transform_version="v1",
            clock_domain="monotonic",
            content_hash="e" * 64,
            source_geometry=geometry,
            image_ref="frame://1",
            capture_health=frame.capture_health,
            image_metadata={"bad": {1, 2}},
        )


def test_frame_packet_roundtrip() -> None:
    packet = FramePacket(
        source_id="main",
        session_id="session-1",
        frame_id=7,
        captured_at_ns=1000,
        received_at_ns=2000,
        transform_version="v1",
        clock_domain="monotonic",
        content_hash="e" * 64,
        source_geometry=SourceGeometry(
            source_size=FrameSize(width=100, height=100),
            content_rect=SourceRect(x=0, y=0, width=1, height=1),
            working_size=FrameSize(width=1, height=1),
        ),
        image_ref="frame://001.png",
        capture_health=_health(),
        image_metadata={"mode": "rgb", "roi": [0, 1, 2], "tags": {"a": 1}},
    )

    serialized = json.dumps(packet.to_dict())
    data = json.loads(serialized)
    hydrated = FramePacket.from_dict(data)
    assert hydrated == packet
    assert packet.age_ns() == 1000


def test_frame_packet_illegal_time_order_and_empty_session() -> None:
    base = SourceGeometry(
        source_size=FrameSize(width=10, height=10),
        content_rect=SourceRect(x=0, y=0, width=1, height=1),
        working_size=FrameSize(width=1, height=1),
    )
    with pytest.raises(ValueError):
        FramePacket(
            source_id="main",
            session_id="",
            frame_id=0,
            captured_at_ns=10,
            received_at_ns=11,
            transform_version="v1",
            clock_domain="monotonic",
            content_hash="e" * 64,
            source_geometry=base,
            image_ref="x",
            capture_health=_health(),
        )

    with pytest.raises(ValueError):
        FramePacket(
            source_id="main",
            session_id="s",
            frame_id=0,
            captured_at_ns=10,
            received_at_ns=9,
            transform_version="v1",
            clock_domain="monotonic",
            content_hash="e" * 64,
            source_geometry=base,
            image_ref="x",
            capture_health=_health(),
        )


def test_capture_health_validation_and_boundary() -> None:
    packet = _health_frame(10_000, 15_000)
    fresh = CaptureHealth.from_frame(packet, max_age_ns=5_000)
    stale = CaptureHealth.from_frame(packet, max_age_ns=4_999)
    assert fresh.is_fresh is True
    assert stale.is_fresh is False
    assert stale.age_ns_at(16_000) == 6_000
    assert stale.freshness_ns_at(16_000) == 0
    assert not stale.is_fresh_at(16_000)
    assert fresh.is_fresh_at(15_000)
    with pytest.raises(ValueError):
        stale.ensure_fresh_at(16_000)
    with pytest.raises(ValueError):
        stale.is_fresh_at(9_999)


def test_capture_health_zero_age_budget_and_serialization() -> None:
    health = CaptureHealth(
        session_id="s",
        frame_id=0,
        source_id="main",
        content_hash="a" * 64,
        clock_domain="monotonic",
        captured_at_ns=0,
        received_at_ns=0,
        transform_version="v1",
        max_age_ns=0,
    )
    assert health.is_fresh
    assert health.expires_at_ns == 0
    assert health.is_fresh_at(0)
    assert not health.is_fresh_at(1)
    assert CaptureHealth.from_dict(health.to_dict()) == health


def test_capture_health_and_frame_binding() -> None:
    geometry = _geometry()
    frame = FramePacket(
        source_id="main",
        session_id="session-1",
        frame_id=7,
        captured_at_ns=1000,
        received_at_ns=1000,
        transform_version="v1",
        clock_domain="monotonic",
        content_hash="e" * 64,
        source_geometry=geometry,
        image_ref="frame://001.png",
        capture_health=CaptureHealth(
            session_id="session-1",
            frame_id=7,
            source_id="main",
            content_hash="e" * 64,
            clock_domain="monotonic",
            captured_at_ns=1000,
            received_at_ns=1000,
            transform_version="v1",
            max_age_ns=1000,
        ),
    )
    assert frame.capture_health == CaptureHealth.from_frame(frame, max_age_ns=1000)


def _health_frame(captured_at_ns: int, received_at_ns: int) -> FramePacket:
    return FramePacket(
        source_id="main",
        session_id="s",
        frame_id=0,
        captured_at_ns=captured_at_ns,
        received_at_ns=received_at_ns,
        transform_version="v1",
        clock_domain="monotonic",
        content_hash="a" * 64,
        source_geometry=SourceGeometry(
            source_size=FrameSize(width=1, height=1),
            content_rect=SourceRect(x=0, y=0, width=1, height=1),
            working_size=FrameSize(width=1, height=1),
        ),
        image_ref="x",
        capture_health=CaptureHealth(
            session_id="s",
            frame_id=0,
            source_id="main",
            content_hash="a" * 64,
            clock_domain="monotonic",
            captured_at_ns=captured_at_ns,
            received_at_ns=received_at_ns,
            transform_version="v1",
            max_age_ns=5_000,
        ),
    )
