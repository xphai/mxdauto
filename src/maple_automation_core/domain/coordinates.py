from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any

from ._contract_utils import ensure_mapping, ensure_non_negative_int


@dataclass(frozen=True, slots=True)
class PixelCoordinate:
    """Pixel-space coordinate on the captured frame."""

    x: int
    y: int

    def __post_init__(self) -> None:
        ensure_non_negative_int(self.x, "x")
        ensure_non_negative_int(self.y, "y")

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PixelCoordinate:
        data = ensure_mapping(value, "PixelCoordinate payload")
        try:
            return cls(x=data["x"], y=data["y"])
        except KeyError as exc:
            raise ValueError(f"PixelCoordinate payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class WorldCoordinate:
    """World-space coordinate in logical map units."""

    x: float
    y: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.x, bool)
            or isinstance(self.y, bool)
            or not isinstance(self.x, int | float)
            or not isinstance(self.y, int | float)
        ):
            raise ValueError("x/y must be numbers.")
        object.__setattr__(self, "x", float(self.x))
        object.__setattr__(self, "y", float(self.y))
        if not all(isfinite(v) for v in (self.x, self.y)):
            raise ValueError("x/y must be finite.")

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorldCoordinate:
        data = ensure_mapping(value, "WorldCoordinate payload")
        try:
            return cls(x=data["x"], y=data["y"])
        except KeyError as exc:
            raise ValueError(f"WorldCoordinate payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class Velocity:
    dx: float
    dy: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.dx, bool)
            or isinstance(self.dy, bool)
            or not isinstance(self.dx, int | float)
            or not isinstance(self.dy, int | float)
        ):
            raise ValueError("dx/dy must be numbers.")
        object.__setattr__(self, "dx", float(self.dx))
        object.__setattr__(self, "dy", float(self.dy))
        if not all(isfinite(v) for v in (self.dx, self.dy)):
            raise ValueError("dx/dy must be finite.")

    def to_dict(self) -> dict[str, float]:
        return {"dx": self.dx, "dy": self.dy}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Velocity:
        data = ensure_mapping(value, "Velocity payload")
        try:
            return cls(dx=data["dx"], dy=data["dy"])
        except KeyError as exc:
            raise ValueError(f"Velocity payload missing key: {exc.args[0]}") from exc
