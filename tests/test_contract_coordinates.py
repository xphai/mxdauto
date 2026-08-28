from __future__ import annotations

import pytest

from maple_automation_core.domain.coordinates import PixelCoordinate, Velocity, WorldCoordinate


def test_pixel_coordinate_valid() -> None:
    point = PixelCoordinate(1920, 1080)
    assert point.x == 1920
    assert point.y == 1080
    assert point.to_dict() == {"x": 1920, "y": 1080}


def test_pixel_coordinate_invalid() -> None:
    with pytest.raises(ValueError):
        PixelCoordinate(-1, 0)


def test_world_and_velocity_coordinates_roundtrip() -> None:
    point = WorldCoordinate(1.25, -3.0)
    assert point.x == 1.25
    assert point.to_dict() == {"x": 1.25, "y": -3.0}

    speed = Velocity(dx=0.5, dy=-0.5)
    assert speed.to_dict() == {"dx": 0.5, "dy": -0.5}

    assert WorldCoordinate.from_dict(point.to_dict()) == point
    assert Velocity.from_dict(speed.to_dict()) == speed
