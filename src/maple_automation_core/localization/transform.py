"""Versioned working-pixel to world-coordinate transforms.

The localization boundary deliberately keeps the geometric operation small and
deterministic.  A :class:`LocalizationTransform` is an immutable two-row, three-
column affine matrix together with the identity of the map/calibration it was
derived from.  Callers can therefore not accidentally apply a transform to a
different working image size or calibration.

Only the working-space to world-space projection is needed by the first
localization slice, but the exact inverse is provided as well.  It is useful for
round-trip tests and for consumers that need to draw a world-space result back
onto a working frame.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any, Self

from maple_automation_core.domain._contract_utils import (
    ensure_mapping,
    ensure_non_empty_str,
    ensure_sha256_hex,
    hash_payload,
)
from maple_automation_core.domain.coordinates import PixelCoordinate, WorldCoordinate
from maple_automation_core.domain.frame import FrameSize

type Matrix2x3 = tuple[tuple[float, float, float], tuple[float, float, float]]
type PointLike = PixelCoordinate | Sequence[float]


def _finite_number(value: Any, field_name: str) -> float:
    """Return a canonical float after strict finite-number validation."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a finite number.")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number.") from exc
    if not isfinite(result):
        raise ValueError(f"{field_name} must be a finite number.")
    # JSON has two spellings for zero.  Normalizing -0.0 keeps the transform
    # digest independent of how a caller spelled an otherwise equal matrix.
    return 0.0 if result == 0.0 else result


def _matrix(value: Any) -> Matrix2x3:
    """Validate and freeze a 2x3 affine matrix."""

    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ValueError("matrix must contain exactly two rows.")
    rows: list[tuple[float, float, float]] = []
    for row_index, row in enumerate(value):
        if not isinstance(row, list | tuple) or len(row) != 3:
            raise ValueError(f"matrix row {row_index} must contain exactly three values.")
        rows.append(
            (
                _finite_number(row[0], f"matrix[{row_index}][0]"),
                _finite_number(row[1], f"matrix[{row_index}][1]"),
                _finite_number(row[2], f"matrix[{row_index}][2]"),
            )
        )

    result: Matrix2x3 = (rows[0], rows[1])
    determinant = result[0][0] * result[1][1] - result[0][1] * result[1][0]
    if not isfinite(determinant):
        raise ValueError("matrix determinant must be finite.")
    if determinant == 0.0:
        raise ValueError("matrix must be non-degenerate.")
    return result


def _point(value: Any, field_name: str) -> tuple[float, float]:
    """Validate a finite point represented by a two-item sequence."""

    if isinstance(value, PixelCoordinate):
        return (
            _finite_number(value.x, f"{field_name}[0]"),
            _finite_number(value.y, f"{field_name}[1]"),
        )
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ValueError(f"{field_name} must be a coordinate or a two-item point.")
    return (
        _finite_number(value[0], f"{field_name}[0]"),
        _finite_number(value[1], f"{field_name}[1]"),
    )


def _world_point(value: Any) -> tuple[float, float]:
    """Validate a world point represented by a typed or numeric point."""

    if isinstance(value, WorldCoordinate):
        return float(value.x), float(value.y)
    return _point(value, "world point")


@dataclass(frozen=True, slots=True)
class LocalizationTransform:
    """Immutable affine mapping from working pixels to world coordinates.

    ``matrix`` is stored in the usual two-row affine form::

        ((a, b, tx),
         (c, d, ty))

    and maps ``(x, y)`` to ``(a*x + b*y + tx, c*x + d*y + ty)``.  Both
    endpoints of ``working_size`` are valid because localization also uses
    transforms for rectangle corners.
    """

    map_id: str
    map_fingerprint_sha256: str
    profile_id: str
    transform_version: str
    calibration_sha256: str
    working_size: FrameSize
    matrix: Matrix2x3

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.map_id, "map_id")
        ensure_sha256_hex(self.map_fingerprint_sha256, "map_fingerprint_sha256")
        ensure_non_empty_str(self.profile_id, "profile_id")
        ensure_non_empty_str(self.transform_version, "transform_version")
        ensure_sha256_hex(self.calibration_sha256, "calibration_sha256")
        if not isinstance(self.working_size, FrameSize):
            raise TypeError("working_size must be FrameSize.")

        object.__setattr__(self, "matrix", _matrix(self.matrix))
        object.__setattr__(
            self,
            "map_fingerprint_sha256",
            self.map_fingerprint_sha256.lower(),
        )
        object.__setattr__(self, "calibration_sha256", self.calibration_sha256.lower())

    @property
    def determinant(self) -> float:
        """Determinant of the linear two-by-two portion of ``matrix``."""

        ((a, b, _), (c, d, _)) = self.matrix
        return a * d - b * c

    @property
    def affine_matrix(self) -> Matrix2x3:
        """Explicit alias for callers that prefer the longer matrix name."""

        return self.matrix

    @property
    def coefficients(self) -> tuple[float, float, float, float, float, float]:
        """Return ``(a, b, c, d, tx, ty)`` in canonical coefficient order."""

        ((a, b, tx), (c, d, ty)) = self.matrix
        return (a, b, c, d, tx, ty)

    def validate_context(
        self,
        calibration_sha256: str,
        working_size: FrameSize,
    ) -> None:
        """Check that a caller's calibration context matches this transform."""

        ensure_sha256_hex(calibration_sha256, "calibration_sha256")
        if calibration_sha256.lower() != self.calibration_sha256:
            raise ValueError("calibration_sha256 does not match the transform.")
        if not isinstance(working_size, FrameSize):
            raise TypeError("working_size must be FrameSize.")
        if working_size != self.working_size:
            raise ValueError("working_size does not match the transform.")

    def _validate_optional_context(
        self,
        calibration_sha256: str | None,
        working_size: FrameSize | None,
    ) -> None:
        if (calibration_sha256 is None) != (working_size is None):
            raise ValueError("calibration_sha256 and working_size must be supplied together.")
        if calibration_sha256 is not None and working_size is not None:
            self.validate_context(calibration_sha256, working_size)

    def _working_point(self, value: Any) -> tuple[float, float]:
        x, y = _point(value, "working point")
        if not (0.0 <= x <= self.working_size.width) or not (0.0 <= y <= self.working_size.height):
            raise ValueError(
                f"working point {(x, y)} outside "
                f"{(self.working_size.width, self.working_size.height)}"
            )
        return x, y

    def to_world(
        self,
        point: PointLike,
        *,
        calibration_sha256: str | None = None,
        working_size: FrameSize | None = None,
    ) -> WorldCoordinate:
        """Map a working-space point to a finite :class:`WorldCoordinate`.

        If context arguments are provided, both are required and are checked
        against the transform before any projection is performed.  Omitting
        both is convenient for a transform that is already bound to a trusted
        frame; supplying them makes the boundary check explicit for adapters.
        """

        self._validate_optional_context(calibration_sha256, working_size)
        x, y = self._working_point(point)
        ((a, b, tx), (c, d, ty)) = self.matrix
        world_x = a * x + b * y + tx
        world_y = c * x + d * y + ty
        if not isfinite(world_x) or not isfinite(world_y):
            raise ValueError("mapped world coordinate must be finite.")
        return WorldCoordinate(world_x, world_y)

    def map_to_world(
        self,
        point: PointLike,
        *,
        calibration_sha256: str | None = None,
        working_size: FrameSize | None = None,
    ) -> WorldCoordinate:
        """Alias for :meth:`to_world` used by mapping-oriented call sites."""

        return self.to_world(
            point,
            calibration_sha256=calibration_sha256,
            working_size=working_size,
        )

    def working_to_world(
        self,
        point: PointLike,
        *,
        calibration_sha256: str | None = None,
        working_size: FrameSize | None = None,
    ) -> WorldCoordinate:
        """Alias for :meth:`to_world` using explicit space names."""

        return self.to_world(
            point,
            calibration_sha256=calibration_sha256,
            working_size=working_size,
        )

    def apply(
        self,
        position: PointLike,
        *,
        calibration_sha256: str,
        working_size: FrameSize,
    ) -> WorldCoordinate:
        """Map a point after validating its calibration and working size.

        Unlike the lower-level :meth:`to_world` convenience method, this
        integration-facing operation requires both pieces of frame context.
        That keeps an adapter from accidentally applying a valid transform to
        a frame from another calibration or image size.
        """

        return self.to_world(
            position,
            calibration_sha256=calibration_sha256,
            working_size=working_size,
        )

    def to_working(
        self,
        point: WorldCoordinate | Sequence[float],
        *,
        calibration_sha256: str | None = None,
        working_size: FrameSize | None = None,
    ) -> tuple[float, float]:
        """Apply the exact inverse mapping and return a floating-point point."""

        self._validate_optional_context(calibration_sha256, working_size)
        x, y = _world_point(point)
        ((a, b, tx), (c, d, ty)) = self.matrix
        determinant = self.determinant
        translated_x = x - tx
        translated_y = y - ty
        working_x = (d * translated_x - b * translated_y) / determinant
        working_y = (-c * translated_x + a * translated_y) / determinant
        if not isfinite(working_x) or not isfinite(working_y):
            raise ValueError("mapped working coordinate must be finite.")
        return (0.0 if working_x == 0.0 else working_x, 0.0 if working_y == 0.0 else working_y)

    def inverse(
        self,
        point: WorldCoordinate | Sequence[float],
        *,
        calibration_sha256: str | None = None,
        working_size: FrameSize | None = None,
    ) -> tuple[float, float]:
        """Alias for :meth:`to_working`."""

        return self.to_working(
            point,
            calibration_sha256=calibration_sha256,
            working_size=working_size,
        )

    def world_to_working(
        self,
        point: WorldCoordinate | Sequence[float],
        *,
        calibration_sha256: str | None = None,
        working_size: FrameSize | None = None,
    ) -> tuple[float, float]:
        """Alias for :meth:`to_working` using explicit space names."""

        return self.to_working(
            point,
            calibration_sha256=calibration_sha256,
            working_size=working_size,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical, JSON-compatible transform payload."""

        return {
            "map_id": self.map_id,
            "map_fingerprint_sha256": self.map_fingerprint_sha256,
            "profile_id": self.profile_id,
            "transform_version": self.transform_version,
            "calibration_sha256": self.calibration_sha256,
            "working_size": self.working_size.to_dict(),
            "matrix": [list(row) for row in self.matrix],
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        expected_map_fingerprint_sha256: str | None = None,
        expected_calibration_sha256: str | None = None,
        expected_working_size: FrameSize | None = None,
        expected_digest: str | None = None,
    ) -> Self:
        """Reconstruct a transform and optionally verify an external context."""

        data = ensure_mapping(value, "LocalizationTransform payload")
        try:
            if "matrix" in data and "affine_matrix" in data:
                matrix = _matrix(data["matrix"])
                if matrix != _matrix(data["affine_matrix"]):
                    raise ValueError("matrix and affine_matrix payloads contradict each other.")
            else:
                matrix = data["matrix"] if "matrix" in data else data["affine_matrix"]
            transform = cls(
                matrix=matrix,
                map_id=data["map_id"],
                map_fingerprint_sha256=data["map_fingerprint_sha256"],
                profile_id=data["profile_id"],
                transform_version=data["transform_version"],
                calibration_sha256=data["calibration_sha256"],
                working_size=FrameSize.from_dict(data["working_size"]),
            )
        except KeyError as exc:
            raise ValueError(f"LocalizationTransform payload missing key: {exc.args[0]}") from exc
        if expected_calibration_sha256 is not None or expected_working_size is not None:
            if expected_calibration_sha256 is None or expected_working_size is None:
                raise ValueError(
                    "expected_calibration_sha256 and expected_working_size must be "
                    "supplied together."
                )
            transform.validate_context(expected_calibration_sha256, expected_working_size)
        if expected_map_fingerprint_sha256 is not None:
            ensure_sha256_hex(
                expected_map_fingerprint_sha256,
                "expected_map_fingerprint_sha256",
            )
            if expected_map_fingerprint_sha256.lower() != transform.map_fingerprint_sha256:
                raise ValueError("expected_map_fingerprint_sha256 does not match the transform.")
        if expected_digest is not None:
            ensure_sha256_hex(expected_digest, "expected_digest")
            if expected_digest.lower() != transform.digest:
                raise ValueError("expected_digest does not match the transform.")
        return transform

    @property
    def digest(self) -> str:
        """Canonical SHA-256 identity of the transform and its metadata."""

        return hash_payload(self.to_dict())

    @property
    def sha256(self) -> str:
        """Alias for :attr:`digest`."""

        return self.digest


# The shorter names make the contract discoverable without forcing consumers
# to agree on one spelling.  They intentionally refer to the same immutable
# value type, rather than wrappers with subtly different serialization.
AffineTransform2D = LocalizationTransform
Affine2DTransform = LocalizationTransform
Affine2D = LocalizationTransform
AffineTransform = LocalizationTransform
CoordinateTransform = LocalizationTransform


__all__ = [
    "Affine2D",
    "Affine2DTransform",
    "AffineTransform",
    "AffineTransform2D",
    "CoordinateTransform",
    "LocalizationTransform",
    "Matrix2x3",
    "PointLike",
]
