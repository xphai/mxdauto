"""Immutable platform geometry and deterministic platform matching.

The localization layer keeps platform ownership deliberately small.  A
platform is a finite line segment in world coordinates; a graph binds those
segments to a map fingerprint and a versioned matching policy.  No image,
runtime, or mutable map registry is involved here, which makes the contract
safe to use from replay and planning code.

Matching uses the platform's interpolated height at the query ``x``.  When a
query is outside a platform's horizontal span, the nearest endpoint is used
and the horizontal gap is retained as a secondary error.  Candidates are
ordered by vertical error, horizontal error, and finally platform ID.  A
second candidate close to the first one, or a shared endpoint, is reported as
ambiguous rather than making a non-deterministic ownership decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Any, Self

from maple_automation_core.domain._contract_utils import (
    ensure_mapping,
    ensure_non_empty_str,
    ensure_sha256_hex,
    hash_payload,
)
from maple_automation_core.domain.coordinates import WorldCoordinate


def _finite_number(value: Any, field_name: str) -> float:
    """Validate and normalize one numeric contract value."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a finite number.")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{field_name} must be a finite number.")
    return 0.0 if result == 0.0 else result


def _non_negative_number(value: Any, field_name: str) -> float:
    result = _finite_number(value, field_name)
    if result < 0.0:
        raise ValueError(f"{field_name} must be >= 0.")
    return result


def _normalise_point(point: WorldCoordinate) -> WorldCoordinate:
    """Return a point with signed zero normalized for canonical payloads."""

    return WorldCoordinate(
        0.0 if point.x == 0.0 else point.x,
        0.0 if point.y == 0.0 else point.y,
    )


class PlatformMatchStatus(str, Enum):
    """Fail-closed result states for platform ownership."""

    CONFIRMED = "confirmed"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PlatformSegment:
    """One immutable, non-degenerate world-space platform segment."""

    platform_id: str
    start: WorldCoordinate
    end: WorldCoordinate

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.platform_id, "platform_id")
        if not isinstance(self.start, WorldCoordinate):
            raise TypeError("start must be WorldCoordinate.")
        if not isinstance(self.end, WorldCoordinate):
            raise TypeError("end must be WorldCoordinate.")

        start = _normalise_point(self.start)
        end = _normalise_point(self.end)
        if start == end:
            raise ValueError("platform segment must be non-degenerate.")

        # WorldCoordinate validates each component.  Also validate the
        # derived deltas so extreme-but-finite inputs cannot create an
        # overflowed geometry during matching.
        if not isfinite(end.x - start.x) or not isfinite(end.y - start.y):
            raise ValueError("platform segment geometry must be finite.")

        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def dx(self) -> float:
        return self.end.x - self.start.x

    @property
    def dy(self) -> float:
        return self.end.y - self.start.y

    @property
    def horizontal_min(self) -> float:
        return min(self.start.x, self.end.x)

    @property
    def horizontal_max(self) -> float:
        return max(self.start.x, self.end.x)

    @property
    def is_horizontal(self) -> bool:
        return self.start.y == self.end.y

    def _projection(self, point: WorldCoordinate) -> _PlatformProjection:
        """Project *point* by horizontal interpolation and endpoint clamp."""

        x1, y1 = self.start.x, self.start.y
        x2, y2 = self.end.x, self.end.y
        dx = x2 - x1

        if dx == 0.0:
            # A vertical segment is unusual for a foothold but is still a
            # valid non-degenerate line contract.  Its closest point is
            # obtained by clamping in y; horizontal error remains primary
            # only after vertical error, just as for sloped segments.
            projected_y = min(max(point.y, min(y1, y2)), max(y1, y2))
            projected = WorldCoordinate(x1, projected_y)
        elif point.x <= min(x1, x2):
            projected = self.start if x1 <= x2 else self.end
        elif point.x >= max(x1, x2):
            projected = self.end if x1 <= x2 else self.start
        else:
            # Keeping projected x equal to the query x avoids an avoidable
            # round-off error and is the intended linear foot-y projection.
            ratio = (point.x - x1) / dx
            projected_y = y1 + ratio * (y2 - y1)
            projected = WorldCoordinate(point.x, projected_y)

        horizontal_error = abs(point.x - projected.x)
        vertical_error = abs(point.y - projected.y)
        if not isfinite(horizontal_error) or not isfinite(vertical_error):
            raise ValueError("platform projection produced a non-finite error.")
        return _PlatformProjection(
            platform=self,
            projected_point=projected,
            vertical_error=0.0 if vertical_error == 0.0 else vertical_error,
            horizontal_error=0.0 if horizontal_error == 0.0 else horizontal_error,
        )

    def contains_endpoint(self, point: WorldCoordinate) -> bool:
        """Return whether *point* exactly equals one of this segment's ends."""

        return point == self.start or point == self.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "PlatformSegment payload")
        try:
            return cls(
                platform_id=data["platform_id"],
                start=WorldCoordinate.from_dict(data["start"]),
                end=WorldCoordinate.from_dict(data["end"]),
            )
        except KeyError as exc:
            raise ValueError(f"PlatformSegment payload missing key: {exc.args[0]}") from exc

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    @property
    def sha256(self) -> str:
        return self.digest


@dataclass(frozen=True, slots=True)
class _PlatformProjection:
    platform: PlatformSegment
    projected_point: WorldCoordinate
    vertical_error: float
    horizontal_error: float


@dataclass(frozen=True, slots=True)
class PlatformMatch:
    """Immutable result of resolving one world point against a graph.

    ``candidate_platform_ids`` retains the deterministic candidate order for
    diagnostics and replay.  ``vertical_error``/``horizontal_error`` and
    ``projected_point`` describe the best candidate when the result is
    confirmed; for ambiguous and unknown results they are intentionally
    omitted (``None``), because no unique ownership was established.
    """

    status: PlatformMatchStatus
    platform_id: str | None
    vertical_error: float | None = None
    horizontal_error: float | None = None
    projected_point: WorldCoordinate | None = None
    candidate_platform_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, PlatformMatchStatus):
            raise TypeError("status must be PlatformMatchStatus.")
        if self.platform_id is not None:
            ensure_non_empty_str(self.platform_id, "platform_id")
        if self.status is not PlatformMatchStatus.CONFIRMED and self.platform_id is not None:
            raise ValueError("platform_id must be None unless status is CONFIRMED.")
        if self.status is PlatformMatchStatus.CONFIRMED and self.platform_id is None:
            raise ValueError("platform_id is required when status is CONFIRMED.")

        if (self.vertical_error is None) != (self.horizontal_error is None):
            raise ValueError("vertical_error and horizontal_error must be supplied together.")
        if self.vertical_error is not None and self.horizontal_error is not None:
            object.__setattr__(
                self,
                "vertical_error",
                _non_negative_number(self.vertical_error, "vertical_error"),
            )
            object.__setattr__(
                self,
                "horizontal_error",
                _non_negative_number(self.horizontal_error, "horizontal_error"),
            )
        if self.projected_point is not None and not isinstance(
            self.projected_point, WorldCoordinate
        ):
            raise TypeError("projected_point must be WorldCoordinate or None.")

        if not isinstance(self.candidate_platform_ids, tuple):
            raise TypeError("candidate_platform_ids must be a tuple.")
        for candidate_id in self.candidate_platform_ids:
            ensure_non_empty_str(candidate_id, "candidate_platform_ids item")
        if len(set(self.candidate_platform_ids)) != len(self.candidate_platform_ids):
            raise ValueError("candidate_platform_ids must be unique.")

    @property
    def candidates(self) -> tuple[str, ...]:
        """Short alias for the stable candidate ID order."""

        return self.candidate_platform_ids

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return self.candidate_platform_ids

    @property
    def best_platform_id(self) -> str | None:
        return self.platform_id

    @property
    def vertical_distance(self) -> float | None:
        return self.vertical_error

    @property
    def horizontal_distance(self) -> float | None:
        return self.horizontal_error

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "platform_id": self.platform_id,
            "vertical_error": self.vertical_error,
            "horizontal_error": self.horizontal_error,
            "projected_point": (
                None if self.projected_point is None else self.projected_point.to_dict()
            ),
            "candidate_platform_ids": list(self.candidate_platform_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "PlatformMatch payload")
        try:
            raw_candidates = data.get("candidate_platform_ids", [])
            if not isinstance(raw_candidates, list | tuple):
                raise ValueError("candidate_platform_ids must be an array.")
            projected_raw = data.get("projected_point")
            projected = None if projected_raw is None else WorldCoordinate.from_dict(projected_raw)
            return cls(
                status=PlatformMatchStatus(data["status"]),
                platform_id=data["platform_id"],
                vertical_error=data.get("vertical_error"),
                horizontal_error=data.get("horizontal_error"),
                projected_point=projected,
                candidate_platform_ids=tuple(raw_candidates),
            )
        except KeyError as exc:
            raise ValueError(f"PlatformMatch payload missing key: {exc.args[0]}") from exc

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    @property
    def sha256(self) -> str:
        return self.digest


@dataclass(frozen=True, slots=True)
class PlatformGraph:
    """Immutable, versioned collection of uniquely identified platforms."""

    map_id: str
    map_fingerprint_sha256: str
    graph_version: str
    platforms: tuple[PlatformSegment, ...]
    ambiguity_margin: float
    max_vertical_distance: float
    max_horizontal_distance: float

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.map_id, "map_id")
        ensure_sha256_hex(self.map_fingerprint_sha256, "map_fingerprint_sha256")
        ensure_non_empty_str(self.graph_version, "graph_version")
        if not isinstance(self.platforms, tuple):
            raise TypeError("platforms must be a tuple.")
        if any(not isinstance(platform, PlatformSegment) for platform in self.platforms):
            raise TypeError("platforms must contain PlatformSegment values.")

        platform_ids = tuple(platform.platform_id for platform in self.platforms)
        if len(set(platform_ids)) != len(platform_ids):
            raise ValueError("platform IDs must be unique.")
        object.__setattr__(
            self,
            "platforms",
            tuple(sorted(self.platforms, key=lambda platform: platform.platform_id)),
        )
        object.__setattr__(
            self,
            "map_fingerprint_sha256",
            self.map_fingerprint_sha256.lower(),
        )
        object.__setattr__(
            self,
            "ambiguity_margin",
            _non_negative_number(self.ambiguity_margin, "ambiguity_margin"),
        )
        object.__setattr__(
            self,
            "max_vertical_distance",
            _non_negative_number(self.max_vertical_distance, "max_vertical_distance"),
        )
        object.__setattr__(
            self,
            "max_horizontal_distance",
            _non_negative_number(self.max_horizontal_distance, "max_horizontal_distance"),
        )

    @property
    def platform_ids(self) -> tuple[str, ...]:
        return tuple(platform.platform_id for platform in self.platforms)

    @property
    def version(self) -> str:
        return self.graph_version

    @property
    def map_fingerprint(self) -> str:
        return self.map_fingerprint_sha256

    def resolve(self, world_point: WorldCoordinate) -> PlatformMatch:
        """Resolve a finite world point to a platform ownership result."""

        if not isinstance(world_point, WorldCoordinate):
            raise TypeError("world_point must be WorldCoordinate.")

        candidates: list[_PlatformProjection] = []
        for platform in self.platforms:
            projection = platform._projection(world_point)
            if (
                projection.vertical_error <= self.max_vertical_distance
                and projection.horizontal_error <= self.max_horizontal_distance
            ):
                candidates.append(projection)

        ordered = tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.vertical_error,
                    item.horizontal_error,
                    item.platform.platform_id,
                ),
            )
        )
        candidate_ids = tuple(item.platform.platform_id for item in ordered)
        if not ordered:
            return PlatformMatch(
                status=PlatformMatchStatus.UNKNOWN,
                platform_id=None,
                candidate_platform_ids=(),
            )

        best = ordered[0]
        if len(ordered) == 1:
            return PlatformMatch(
                status=PlatformMatchStatus.CONFIRMED,
                platform_id=best.platform.platform_id,
                vertical_error=best.vertical_error,
                horizontal_error=best.horizontal_error,
                projected_point=best.projected_point,
                candidate_platform_ids=candidate_ids,
            )

        second = ordered[1]
        endpoint_matches = sum(item.platform.contains_endpoint(world_point) for item in ordered)
        shared_endpoint = endpoint_matches >= 2
        vertical_delta = abs(second.vertical_error - best.vertical_error)
        horizontal_delta = abs(second.horizontal_error - best.horizontal_error)
        near_tie = (
            vertical_delta <= self.ambiguity_margin and horizontal_delta <= self.ambiguity_margin
        )
        if shared_endpoint or near_tie:
            return PlatformMatch(
                status=PlatformMatchStatus.AMBIGUOUS,
                platform_id=None,
                candidate_platform_ids=candidate_ids,
            )

        return PlatformMatch(
            status=PlatformMatchStatus.CONFIRMED,
            platform_id=best.platform.platform_id,
            vertical_error=best.vertical_error,
            horizontal_error=best.horizontal_error,
            projected_point=best.projected_point,
            candidate_platform_ids=candidate_ids,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "map_id": self.map_id,
            "map_fingerprint_sha256": self.map_fingerprint_sha256,
            "graph_version": self.graph_version,
            "platforms": [platform.to_dict() for platform in self.platforms],
            "ambiguity_margin": self.ambiguity_margin,
            "max_vertical_distance": self.max_vertical_distance,
            "max_horizontal_distance": self.max_horizontal_distance,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "PlatformGraph payload")
        try:
            raw_platforms = data["platforms"]
            if not isinstance(raw_platforms, list | tuple):
                raise ValueError("platforms must be an array.")
            platforms = tuple(PlatformSegment.from_dict(item) for item in raw_platforms)
            return cls(
                map_id=data["map_id"],
                map_fingerprint_sha256=data["map_fingerprint_sha256"],
                graph_version=data["graph_version"],
                platforms=platforms,
                ambiguity_margin=data["ambiguity_margin"],
                max_vertical_distance=data["max_vertical_distance"],
                max_horizontal_distance=data["max_horizontal_distance"],
            )
        except KeyError as exc:
            raise ValueError(f"PlatformGraph payload missing key: {exc.args[0]}") from exc

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    @property
    def sha256(self) -> str:
        return self.digest


__all__ = [
    "PlatformGraph",
    "PlatformMatch",
    "PlatformMatchStatus",
    "PlatformSegment",
]
