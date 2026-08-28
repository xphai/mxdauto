from __future__ import annotations

from math import inf, nan

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
        source_size=FrameSize(1920, 1080),
        content_rect=SourceRect(277, 156, 1366, 768),
        working_size=FrameSize(1296, 700),
    )


def _frame(
    *,
    captured_at_ns: int = 100,
    received_at_ns: int = 110,
    max_age_ns: int = 100,
) -> FramePacket:
    health = CaptureHealth(
        session_id="s",
        frame_id=4,
        source_id="main",
        content_hash="a" * 64,
        clock_domain="monotonic",
        captured_at_ns=captured_at_ns,
        received_at_ns=received_at_ns,
        transform_version="v1",
        max_age_ns=max_age_ns,
    )
    return FramePacket(
        source_id="main",
        session_id="s",
        frame_id=4,
        captured_at_ns=captured_at_ns,
        received_at_ns=received_at_ns,
        transform_version="v1",
        clock_domain="monotonic",
        content_hash="a" * 64,
        source_geometry=_geometry(),
        image_ref="frame://4",
        capture_health=health,
        image_metadata={"source": "test"},
    )


def test_frame_value_objects_validate_types_and_missing_fields() -> None:
    for constructor, payload in (
        (FrameSize.from_dict, {}),
        (SourceRect.from_dict, {}),
        (SourceGeometry.from_dict, {}),
        (CaptureHealth.from_dict, {}),
        (FramePacket.from_dict, {}),
    ):
        with pytest.raises(ValueError):
            constructor(payload)

    with pytest.raises(ValueError):
        FrameSize(0, 1)
    with pytest.raises(ValueError):
        FrameSize(1, False)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SourceRect(0, 0, 0, 1)
    with pytest.raises((ValueError, TypeError)):
        SourceGeometry(
            source_size=FrameSize(10, 10),
            content_rect=SourceRect(0, 0, 1, 1),
            working_size="bad",  # type: ignore[arg-type]
        )

    rect = SourceRect(1, 2, 3, 4)
    assert rect.right == rect.x2 == 4
    assert rect.bottom == rect.y2 == 6
    assert rect.contains(FrameSize(4, 6))
    assert not rect.contains(FrameSize(3, 6))


def test_source_geometry_mapping_rejects_non_numeric_and_out_of_range_points() -> None:
    geometry = _geometry()
    for operation, point in (
        (geometry.content_to_working, ("x", 1)),
        (geometry.content_to_working, (True, 1)),
        (geometry.content_to_working, (nan, 1)),
        (geometry.content_to_working, (1367, 1)),
        (geometry.working_to_content, ("x", 1)),
        (geometry.working_to_content, (True, 1)),
        (geometry.working_to_content, (inf, 1)),
        (geometry.working_to_content, (1297, 1)),
        (geometry.content_to_source, (0, -1)),
        (geometry.source_to_content, (276, 156)),
    ):
        with pytest.raises(ValueError):
            operation(*point)  # type: ignore[arg-type]

    assert geometry.content_to_source(0, 0) == (277, 156)
    assert geometry.source_to_content(277, 156) == (0, 0)
    assert geometry.source_to_working(277, 156) == (0.0, 0.0)
    assert geometry.working_scale_x == geometry.scale_x
    assert geometry.working_scale_y == geometry.scale_y
    assert not geometry.is_uniform_scale


def test_capture_health_freshness_and_frame_delegation() -> None:
    frame = _frame()
    health = frame.capture_health
    assert health.to_dict()["frame_id"] == 4
    assert health.age_ns == 10
    assert health.age_ns_at(110) == 10
    assert health.freshness_ns() == 90
    assert health.freshness_ns_at(150) == 50
    assert health.is_fresh
    assert frame.is_fresh()
    assert frame.age_ns_at(150) == 50
    assert frame.freshness_ns_at(150) == 50
    frame.ensure_fresh()
    with pytest.raises(ValueError):
        health.ensure_fresh_at(201)
    assert not frame.is_fresh_at(201)
    with pytest.raises(ValueError):
        health.age_ns_at(99)

    assert CaptureHealth.from_frame(frame, 100) == health
    with pytest.raises(TypeError):
        CaptureHealth.from_frame(object(), 1)  # type: ignore[arg-type]


def test_frame_binding_and_strict_hydration() -> None:
    frame = _frame()
    data = frame.to_dict()
    assert FramePacket.from_dict(data) == frame
    with pytest.raises(ValueError):
        FramePacket.from_dict({**data, "image_metadata": []})
    with pytest.raises(ValueError):
        FramePacket.from_dict({**data, "frame_id": "4"})
    with pytest.raises(ValueError):
        FramePacket(
            source_id="main",
            session_id="s",
            frame_id=4,
            captured_at_ns=100,
            received_at_ns=110,
            transform_version="v1",
            clock_domain="monotonic",
            content_hash="a" * 64,
            source_geometry=_geometry(),
            image_ref="frame://4",
            capture_health=frame.capture_health,
            image_metadata=[],  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        CaptureHealth(
            session_id="s",
            frame_id=4,
            source_id="main",
            content_hash="a" * 64,
            clock_domain="monotonic",
            captured_at_ns=110,
            received_at_ns=100,
            transform_version="v1",
            max_age_ns=1,
        )
