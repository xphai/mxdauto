from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from ._contract_utils import (
    ensure_json_value,
    ensure_mapping,
    ensure_non_empty_str,
    ensure_non_negative_int,
    ensure_positive_int,
    ensure_sha256_hex,
    ensure_time_ns,
    freeze_json_value,
    to_json_dict,
)


@dataclass(frozen=True, slots=True)
class FrameSize:
    """Frame dimensions used for source/working size declarations."""

    width: int
    height: int

    def __post_init__(self) -> None:
        ensure_positive_int(self.width, "width")
        ensure_positive_int(self.height, "height")

    def to_dict(self) -> dict[str, int]:
        return {"width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FrameSize:
        data = ensure_mapping(value, "FrameSize payload")
        try:
            return cls(width=data["width"], height=data["height"])
        except KeyError as exc:
            raise ValueError(f"FrameSize payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class SourceRect:
    """Capture content rectangle in source pixel space."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        ensure_non_negative_int(self.x, "x")
        ensure_non_negative_int(self.y, "y")
        ensure_positive_int(self.width, "width")
        ensure_positive_int(self.height, "height")

    @property
    def x2(self) -> int:
        return self.x + self.width

    @property
    def y2(self) -> int:
        return self.y + self.height

    @property
    def right(self) -> int:
        """Exclusive right edge (an alias useful to geometry consumers)."""

        return self.x2

    @property
    def bottom(self) -> int:
        """Exclusive bottom edge (an alias useful to geometry consumers)."""

        return self.y2

    def contains(self, frame: FrameSize) -> bool:
        return (
            0 <= self.x <= frame.width
            and 0 <= self.y <= frame.height
            and self.x2 <= frame.width
            and self.y2 <= frame.height
        )

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceRect:
        data = ensure_mapping(value, "SourceRect payload")
        try:
            return cls(
                x=data["x"],
                y=data["y"],
                width=data["width"],
                height=data["height"],
            )
        except KeyError as exc:
            raise ValueError(f"SourceRect payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class SourceGeometry:
    """Capture source geometry and derived working geometry."""

    source_size: FrameSize
    content_rect: SourceRect
    working_size: FrameSize

    def __post_init__(self) -> None:
        if not isinstance(self.source_size, FrameSize):
            raise TypeError("source_size must be FrameSize.")
        if not isinstance(self.content_rect, SourceRect):
            raise TypeError("content_rect must be SourceRect.")
        if not isinstance(self.working_size, FrameSize):
            raise TypeError("working_size must be FrameSize.")

        if not self.content_rect.contains(self.source_size):
            raise ValueError("content_rect must fit inside source_size.")
        # Working geometry may be resized or letterboxed independently of the
        # source/content geometry. FrameSize already enforces positive dimensions.

    @property
    def content_size(self) -> FrameSize:
        """Dimensions of the crop before it is resized to ``working_size``."""

        return FrameSize(width=self.content_rect.width, height=self.content_rect.height)

    @property
    def scale_x(self) -> float:
        """Scale from content pixels to working pixels on the x axis."""

        return self.working_size.width / self.content_rect.width

    @property
    def scale_y(self) -> float:
        """Scale from content pixels to working pixels on the y axis."""

        return self.working_size.height / self.content_rect.height

    @property
    def working_scale_x(self) -> float:
        """Explicit alias for :attr:`scale_x`."""

        return self.scale_x

    @property
    def working_scale_y(self) -> float:
        """Explicit alias for :attr:`scale_y`."""

        return self.scale_y

    @property
    def downsample_x(self) -> float:
        """Content pixels represented by one working pixel on x."""

        return 1.0 / self.scale_x

    @property
    def downsample_y(self) -> float:
        """Content pixels represented by one working pixel on y."""

        return 1.0 / self.scale_y

    @property
    def downsample(self) -> tuple[float, float]:
        """Anisotropic downsampling ratio ``(x, y)``.

        Keeping both axes is intentional: the configured 1366x768 content
        commonly becomes 1296x700, so treating this as one scalar introduces
        coordinate drift.
        """

        return (self.downsample_x, self.downsample_y)

    @property
    def is_uniform_scale(self) -> bool:
        return self.scale_x == self.scale_y

    def content_to_working(self, x: float, y: float) -> tuple[float, float]:
        """Map a point in cropped content pixels to working pixels.

        Both endpoints are accepted, making this method useful for rectangle
        corners as well as pixel centres.
        """

        self._ensure_content_point(x, y)
        return (x * self.scale_x, y * self.scale_y)

    def working_to_content(self, x: float, y: float) -> tuple[float, float]:
        """Map a working-space point back to cropped content pixels."""

        self._ensure_working_point(x, y)
        return (x * self.downsample_x, y * self.downsample_y)

    def content_to_source(self, x: float, y: float) -> tuple[float, float]:
        """Map a cropped-content point to source-frame pixels."""

        self._ensure_content_point(x, y)
        return (self.content_rect.x + x, self.content_rect.y + y)

    def source_to_content(self, x: float, y: float) -> tuple[float, float]:
        """Map a source-frame point to cropped-content pixels."""

        content_x = x - self.content_rect.x
        content_y = y - self.content_rect.y
        self._ensure_content_point(content_x, content_y)
        return (content_x, content_y)

    def working_to_source(self, x: float, y: float) -> tuple[float, float]:
        """Map a working-space point directly to source-frame pixels."""

        content_x, content_y = self.working_to_content(x, y)
        return self.content_to_source(content_x, content_y)

    def source_to_working(self, x: float, y: float) -> tuple[float, float]:
        """Map a source-frame point directly to working pixels."""

        content_x, content_y = self.source_to_content(x, y)
        return self.content_to_working(content_x, content_y)

    def _ensure_content_point(self, x: float, y: float) -> None:
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, int | float)
            or not isinstance(y, int | float)
        ):
            raise ValueError("content coordinates must be numbers.")
        if not isfinite(float(x)) or not isfinite(float(y)):
            raise ValueError("content coordinates must be finite.")
        if not (0.0 <= float(x) <= self.content_rect.width) or not (
            0.0 <= float(y) <= self.content_rect.height
        ):
            raise ValueError(
                f"content coordinate {(x, y)} outside "
                f"{(self.content_rect.width, self.content_rect.height)}"
            )

    def _ensure_working_point(self, x: float, y: float) -> None:
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, int | float)
            or not isinstance(y, int | float)
        ):
            raise ValueError("working coordinates must be numbers.")
        if not isfinite(float(x)) or not isfinite(float(y)):
            raise ValueError("working coordinates must be finite.")
        if not (0.0 <= float(x) <= self.working_size.width) or not (
            0.0 <= float(y) <= self.working_size.height
        ):
            raise ValueError(
                f"working coordinate {(x, y)} outside "
                f"{(self.working_size.width, self.working_size.height)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_size": self.source_size.to_dict(),
            "content_rect": self.content_rect.to_dict(),
            "working_size": self.working_size.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGeometry:
        data = ensure_mapping(value, "SourceGeometry payload")
        try:
            return cls(
                source_size=FrameSize.from_dict(data["source_size"]),
                content_rect=SourceRect.from_dict(data["content_rect"]),
                working_size=FrameSize.from_dict(data["working_size"]),
            )
        except KeyError as exc:
            raise ValueError(f"SourceGeometry payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class CaptureHealth:
    """Timestamp-correctness and freshness checks for a captured frame."""

    session_id: str
    frame_id: int
    source_id: str
    content_hash: str
    clock_domain: str
    captured_at_ns: int
    received_at_ns: int
    transform_version: str
    max_age_ns: int

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.source_id, "source_id")
        ensure_non_empty_str(self.clock_domain, "clock_domain")
        ensure_non_negative_int(self.frame_id, "frame_id")
        ensure_sha256_hex(self.content_hash, "content_hash")
        ensure_non_empty_str(self.transform_version, "transform_version")
        ensure_non_negative_int(self.max_age_ns, "max_age_ns")
        ensure_time_ns(self.captured_at_ns, "captured_at_ns")
        ensure_time_ns(self.received_at_ns, "received_at_ns")
        if self.received_at_ns < self.captured_at_ns:
            raise ValueError("received_at_ns must be >= captured_at_ns.")

    @property
    def age_ns(self) -> int:
        return self.received_at_ns - self.captured_at_ns

    def age_ns_at(self, now_ns: int) -> int:
        ensure_time_ns(now_ns, "now_ns")
        if now_ns < self.captured_at_ns:
            raise ValueError("now_ns must be >= captured_at_ns in the same clock domain.")
        return now_ns - self.captured_at_ns

    @property
    def is_fresh(self) -> bool:
        return self.age_ns <= self.max_age_ns

    def freshness_ns(self) -> int:
        return max(0, self.max_age_ns - self.age_ns)

    def freshness_ns_at(self, now_ns: int) -> int:
        return max(0, self.max_age_ns - self.age_ns_at(now_ns))

    @property
    def expires_at_ns(self) -> int:
        """Capture timestamp at which the freshness lease ends."""

        return self.captured_at_ns + self.max_age_ns

    def is_fresh_at(self, now_ns: int) -> bool:
        return self.age_ns_at(now_ns) <= self.max_age_ns

    def ensure_fresh(self) -> None:
        if not self.is_fresh:
            raise ValueError(f"Frame too old: age_ns={self.age_ns}, max_age_ns={self.max_age_ns}.")

    def ensure_fresh_at(self, now_ns: int) -> None:
        if not self.is_fresh_at(now_ns):
            raise ValueError(
                f"Frame too old at now_ns={now_ns}: "
                f"age_ns={self.age_ns_at(now_ns)}, max_age_ns={self.max_age_ns}."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "source_id": self.source_id,
            "content_hash": self.content_hash,
            "clock_domain": self.clock_domain,
            "captured_at_ns": self.captured_at_ns,
            "received_at_ns": self.received_at_ns,
            "transform_version": self.transform_version,
            "max_age_ns": self.max_age_ns,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CaptureHealth:
        values = ensure_mapping(data, "CaptureHealth payload")
        try:
            return cls(
                session_id=values["session_id"],
                frame_id=values["frame_id"],
                source_id=values["source_id"],
                content_hash=values["content_hash"],
                clock_domain=values["clock_domain"],
                captured_at_ns=values["captured_at_ns"],
                received_at_ns=values["received_at_ns"],
                transform_version=values["transform_version"],
                max_age_ns=values["max_age_ns"],
            )
        except KeyError as exc:
            raise ValueError(f"CaptureHealth payload missing key: {exc.args[0]}") from exc

    @classmethod
    def from_frame(
        cls,
        frame: FramePacket,
        max_age_ns: int,
    ) -> CaptureHealth:
        if not isinstance(frame, FramePacket):
            raise TypeError("frame must be FramePacket.")
        return cls(
            session_id=frame.session_id,
            frame_id=frame.frame_id,
            source_id=frame.source_id,
            content_hash=frame.content_hash,
            clock_domain=frame.clock_domain,
            captured_at_ns=frame.captured_at_ns,
            received_at_ns=frame.received_at_ns,
            transform_version=frame.transform_version,
            max_age_ns=max_age_ns,
        )


@dataclass(frozen=True, slots=True)
class FramePacket:
    """Immutable capture envelope."""

    source_id: str
    session_id: str
    frame_id: int
    captured_at_ns: int
    received_at_ns: int
    transform_version: str
    clock_domain: str
    content_hash: str
    source_geometry: SourceGeometry
    image_ref: str
    capture_health: CaptureHealth
    image_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.source_id, "source_id")
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_negative_int(self.frame_id, "frame_id")
        ensure_time_ns(self.captured_at_ns, "captured_at_ns")
        ensure_time_ns(self.received_at_ns, "received_at_ns")
        ensure_non_empty_str(self.transform_version, "transform_version")
        ensure_non_empty_str(self.clock_domain, "clock_domain")
        ensure_sha256_hex(self.content_hash, "content_hash")
        ensure_non_empty_str(self.image_ref, "image_ref")
        if self.received_at_ns < self.captured_at_ns:
            raise ValueError("received_at_ns must be >= captured_at_ns.")
        if not isinstance(self.source_geometry, SourceGeometry):
            raise TypeError("source_geometry must be a SourceGeometry.")
        if not isinstance(self.capture_health, CaptureHealth):
            raise TypeError("capture_health must be a CaptureHealth.")

        if self.capture_health.session_id != self.session_id:
            raise ValueError("capture_health.session_id must match FramePacket session_id.")
        if self.capture_health.frame_id != self.frame_id:
            raise ValueError("capture_health.frame_id must match FramePacket frame_id.")
        if self.capture_health.source_id != self.source_id:
            raise ValueError("capture_health.source_id must match FramePacket source_id.")
        if self.capture_health.content_hash != self.content_hash:
            raise ValueError("capture_health.content_hash must match FramePacket content_hash.")
        if self.capture_health.clock_domain != self.clock_domain:
            raise ValueError("capture_health.clock_domain must match FramePacket clock_domain.")
        if self.capture_health.transform_version != self.transform_version:
            raise ValueError(
                "capture_health.transform_version must match FramePacket transform_version."
            )
        if self.capture_health.captured_at_ns != self.captured_at_ns:
            raise ValueError("capture_health.captured_at_ns must match FramePacket captured_at_ns.")
        if self.capture_health.received_at_ns != self.received_at_ns:
            raise ValueError("capture_health.received_at_ns must match FramePacket received_at_ns.")

        metadata = ensure_mapping(self.image_metadata, "image_metadata")
        ensure_json_value(metadata, "image_metadata")
        object.__setattr__(
            self,
            "image_metadata",
            freeze_json_value(metadata),
        )

    def age_ns(self) -> int:
        return self.capture_health.age_ns

    def is_fresh(self) -> bool:
        return self.capture_health.is_fresh

    def age_ns_at(self, now_ns: int) -> int:
        return self.capture_health.age_ns_at(now_ns)

    def ensure_fresh(self) -> None:
        self.capture_health.ensure_fresh()

    def is_fresh_at(self, now_ns: int) -> bool:
        return self.capture_health.is_fresh_at(now_ns)

    def freshness_ns(self) -> int:
        return self.capture_health.freshness_ns()

    def freshness_ns_at(self, now_ns: int) -> int:
        return self.capture_health.freshness_ns_at(now_ns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "captured_at_ns": self.captured_at_ns,
            "received_at_ns": self.received_at_ns,
            "transform_version": self.transform_version,
            "clock_domain": self.clock_domain,
            "content_hash": self.content_hash,
            "source_geometry": self.source_geometry.to_dict(),
            "image_ref": self.image_ref,
            "capture_health": self.capture_health.to_dict(),
            "image_metadata": to_json_dict(self.image_metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FramePacket:
        values = ensure_mapping(data, "FramePacket payload")
        try:
            metadata = values.get("image_metadata", {})
            ensure_mapping(metadata, "image_metadata")
            return cls(
                source_id=values["source_id"],
                session_id=values["session_id"],
                frame_id=values["frame_id"],
                captured_at_ns=values["captured_at_ns"],
                received_at_ns=values["received_at_ns"],
                transform_version=values["transform_version"],
                clock_domain=values["clock_domain"],
                content_hash=values["content_hash"],
                source_geometry=SourceGeometry.from_dict(values["source_geometry"]),
                image_ref=values["image_ref"],
                capture_health=CaptureHealth.from_dict(values["capture_health"]),
                image_metadata=metadata,
            )
        except KeyError as exc:
            raise ValueError(f"FramePacket payload missing key: {exc.args[0]}") from exc
