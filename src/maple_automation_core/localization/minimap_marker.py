"""Read-only minimap yellow-marker extraction.

The extractor is deliberately a small adapter at the capture boundary.  It
reads an already admitted :class:`~maple_automation_core.domain.FramePacket`
from the verified pixel CAS, applies one frozen BGR/HSV rule, and emits an
anonymous :class:`~maple_automation_core.localization.PlayerCandidate` only
when exactly one connected component satisfies the rule.  The module does not
own an input device, resize pixels, learn colours, or remember a previous
location.

All public records are immutable and their serialisations contain hashes and
geometry only.  In particular, neither the raw CAS bytes nor a derived mask
is ever included in evidence or result payloads.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Any, NoReturn, Protocol, Self, cast

import cv2
import numpy as np

from maple_automation_core.capture.frame_source import (
    canonical_calibration_sha256,
    canonical_geometry_sha256,
)
from maple_automation_core.capture.pixel_store import (
    PixelSpec,
    pixel_digest,
    validate_pixels,
)
from maple_automation_core.domain._contract_utils import (
    ensure_json_value,
    ensure_mapping,
    ensure_non_empty_str,
    ensure_non_negative_int,
    ensure_probability,
    ensure_sha256_hex,
    ensure_time_ns,
    freeze_json_value,
    hash_payload,
    to_json_dict,
)
from maple_automation_core.domain.frame import FramePacket, FrameSize, SourceGeometry, SourceRect
from maple_automation_core.domain.player_world import Visibility

from .player_localizer import PlayerAnchorSource, PlayerCandidate, WorkingPoint

MINIMAP_MARKER_SCHEMA_VERSION = "1.0.0"
MINIMAP_MARKER_CONFIG_VERSION = "g1-loc-003b-minimap-marker-v1"
ANONYMOUS_PLAYER_SUBJECT = "anonymous-player"

# These are intentionally constants, rather than learned or runtime-tunable
# values.  OpenCV stores hue in [0, 179], so pure BGR yellow is hue 30.  The
# lower bound of 25 leaves a narrow anti-aliasing band while excluding orange.
YELLOW_BGR: tuple[int, int, int] = (0, 255, 255)
YELLOW_BGR_TOLERANCE = 55
YELLOW_HUE_MIN = 25
YELLOW_HUE_MAX = 35
YELLOW_SATURATION_MIN = 195
YELLOW_VALUE_MIN = 235
YELLOW_BRIGHT_CORE_MIN = 3
YELLOW_GREEN_RED_RATIO = 0.82
YELLOW_AREA_MIN = 3
YELLOW_AREA_MAX = 120
YELLOW_WIDTH_MAX = 20
YELLOW_HEIGHT_MAX = 20
# These core limits are frozen in ``MinimapMarkerConfig`` below.  The module
# constants provide only the immutable constructor defaults; extraction reads
# the config fields so every threshold is digest-bound and serialised.
YELLOW_BRIGHT_B_MAX = 50
YELLOW_BRIGHT_GREEN_RED_MIN = 220
YELLOW_BRIGHT_SATURATION_MIN = 205
YELLOW_BRIGHT_VALUE_MIN = 240


class PixelStoreReader(Protocol):
    """Small structural subset of :class:`PixelStore` used by the extractor."""

    def read(self, digest: str, spec: PixelSpec | None = None) -> bytes: ...


def _normalise_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a SHA-256 hex string.")
    ensure_sha256_hex(value, field_name)
    return value.lower()


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a finite number.")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{field_name} must be a finite number.")
    return 0.0 if result == 0.0 else result


def _fixed_int(value: object, expected: int, field_name: str) -> int:
    if type(value) is not int or value != expected:
        raise ValueError(f"{field_name} is fixed at {expected}.")
    return expected


def _fixed_float(value: object, expected: float, field_name: str) -> float:
    number = _finite_number(value, field_name)
    if number != expected:
        raise ValueError(f"{field_name} is fixed at {expected}.")
    return expected


def _pilot_geometry() -> SourceGeometry:
    """Default DEC-001 source geometry used by the capture pilot."""

    return SourceGeometry(
        source_size=FrameSize(width=1920, height=1080),
        content_rect=SourceRect(x=277, y=167, width=1366, height=768),
        working_size=FrameSize(width=1296, height=700),
    )


@dataclass(frozen=True, slots=True, init=False)
class MinimapMarkerConfig:
    """Frozen, digestable marker extraction contract.

    ``PixelSpec`` is always derived from ``geometry.source_size``.  The
    optional ``source_geometry`` constructor spelling is accepted as a
    convenience for callers that use the capture vocabulary; ``geometry`` is
    the canonical serialised field.
    """

    geometry: SourceGeometry
    pixel_spec: PixelSpec
    transform_version: str
    calibration_sha256: str
    minimap_roi: SourceRect | None
    max_age_ns: int | None
    session_id: str | None
    source_id: str | None
    clock_domain: str | None
    subject_id: str
    candidate_confidence: float
    version: str
    bgr_target: tuple[int, int, int]
    bgr_tolerance: int
    hue_min: int
    hue_max: int
    saturation_min: int
    value_min: int
    bright_core_min: int
    bright_b_max: int
    bright_green_red_min: int
    bright_saturation_min: int
    bright_value_min: int
    green_red_ratio_min: float
    area_min: int
    area_max: int
    width_max: int
    height_max: int

    def __init__(
        self,
        geometry: SourceGeometry | None = None,
        *,
        source_geometry: SourceGeometry | None = None,
        pixel_spec: PixelSpec | None = None,
        transform_version: str = "capture-v1",
        calibration_sha256: str | None = None,
        minimap_roi: SourceRect | None = None,
        roi: SourceRect | None = None,
        max_age_ns: int | None = None,
        session_id: str | None = None,
        source_id: str | None = None,
        clock_domain: str | None = None,
        subject_id: str = ANONYMOUS_PLAYER_SUBJECT,
        candidate_confidence: float = 1.0,
        version: str = MINIMAP_MARKER_CONFIG_VERSION,
        bgr_target: tuple[int, int, int] = YELLOW_BGR,
        bgr_tolerance: int = YELLOW_BGR_TOLERANCE,
        hue_min: int = YELLOW_HUE_MIN,
        hue_max: int = YELLOW_HUE_MAX,
        saturation_min: int = YELLOW_SATURATION_MIN,
        value_min: int = YELLOW_VALUE_MIN,
        bright_core_min: int = YELLOW_BRIGHT_CORE_MIN,
        bright_b_max: int = YELLOW_BRIGHT_B_MAX,
        bright_green_red_min: int = YELLOW_BRIGHT_GREEN_RED_MIN,
        bright_saturation_min: int = YELLOW_BRIGHT_SATURATION_MIN,
        bright_value_min: int = YELLOW_BRIGHT_VALUE_MIN,
        green_red_ratio_min: float = YELLOW_GREEN_RED_RATIO,
        area_min: int = YELLOW_AREA_MIN,
        area_max: int = YELLOW_AREA_MAX,
        width_max: int = YELLOW_WIDTH_MAX,
        height_max: int = YELLOW_HEIGHT_MAX,
    ) -> None:
        if geometry is not None and source_geometry is not None and geometry != source_geometry:
            raise ValueError("geometry and source_geometry must match.")
        actual_geometry = geometry if geometry is not None else source_geometry
        if actual_geometry is None:
            actual_geometry = _pilot_geometry()
        if not isinstance(actual_geometry, SourceGeometry):
            raise TypeError("geometry must be SourceGeometry.")
        if minimap_roi is not None and roi is not None and minimap_roi != roi:
            raise ValueError("minimap_roi and roi must match.")
        actual_roi = minimap_roi if minimap_roi is not None else roi
        if actual_roi is not None:
            if not isinstance(actual_roi, SourceRect):
                raise TypeError("minimap_roi must be SourceRect or None.")
            content_rect = actual_geometry.content_rect
            if not (
                content_rect.x <= actual_roi.x
                and content_rect.y <= actual_roi.y
                and actual_roi.x2 <= content_rect.x2
                and actual_roi.y2 <= content_rect.y2
            ):
                raise ValueError("minimap_roi must fit fully inside geometry.content_rect.")

        if not isinstance(transform_version, str) or not transform_version.strip():
            raise ValueError("transform_version must be a non-empty string.")
        if version != MINIMAP_MARKER_CONFIG_VERSION:
            raise ValueError(f"version must be {MINIMAP_MARKER_CONFIG_VERSION}.")
        for value, field_name in (
            (session_id, "session_id"),
            (source_id, "source_id"),
            (clock_domain, "clock_domain"),
        ):
            if value is not None:
                ensure_non_empty_str(value, field_name)
        ensure_non_empty_str(subject_id, "subject_id")
        ensure_probability(candidate_confidence, "candidate_confidence")
        confidence = float(candidate_confidence)
        if confidence != 1.0:
            raise ValueError("candidate_confidence is fixed at 1.0.")

        if max_age_ns is not None:
            ensure_non_negative_int(max_age_ns, "max_age_ns")

        if subject_id != ANONYMOUS_PLAYER_SUBJECT:
            raise ValueError("subject_id is fixed to the anonymous marker subject.")

        expected_spec = PixelSpec(
            width=actual_geometry.source_size.width,
            height=actual_geometry.source_size.height,
        )
        if pixel_spec is not None and not isinstance(pixel_spec, PixelSpec):
            raise TypeError("pixel_spec must be PixelSpec.")
        actual_spec = expected_spec if pixel_spec is None else pixel_spec
        if actual_spec != expected_spec:
            raise ValueError("pixel_spec must match geometry.source_size as packed BGR8.")

        expected_calibration = canonical_calibration_sha256(actual_geometry, transform_version)
        actual_calibration = expected_calibration
        if calibration_sha256 is not None:
            supplied_calibration = _normalise_sha256(calibration_sha256, "calibration_sha256")
            if supplied_calibration != expected_calibration:
                raise ValueError(
                    "calibration_sha256 does not match geometry and transform_version."
                )
            actual_calibration = supplied_calibration

        if not isinstance(bgr_target, list | tuple) or len(bgr_target) != 3:
            raise ValueError("bgr_target must contain three channel values.")
        target = tuple(bgr_target)
        if any(type(channel) is not int or not 0 <= channel <= 255 for channel in target):
            raise ValueError("bgr_target channels must be integers in [0, 255].")
        if target != YELLOW_BGR:
            raise ValueError(f"bgr_target is fixed at {list(YELLOW_BGR)}.")

        _fixed_int(bgr_tolerance, YELLOW_BGR_TOLERANCE, "bgr_tolerance")
        _fixed_int(hue_min, YELLOW_HUE_MIN, "hue_min")
        _fixed_int(hue_max, YELLOW_HUE_MAX, "hue_max")
        _fixed_int(saturation_min, YELLOW_SATURATION_MIN, "saturation_min")
        _fixed_int(value_min, YELLOW_VALUE_MIN, "value_min")
        _fixed_int(bright_core_min, YELLOW_BRIGHT_CORE_MIN, "bright_core_min")
        _fixed_int(bright_b_max, YELLOW_BRIGHT_B_MAX, "bright_b_max")
        _fixed_int(
            bright_green_red_min,
            YELLOW_BRIGHT_GREEN_RED_MIN,
            "bright_green_red_min",
        )
        _fixed_int(
            bright_saturation_min,
            YELLOW_BRIGHT_SATURATION_MIN,
            "bright_saturation_min",
        )
        _fixed_int(bright_value_min, YELLOW_BRIGHT_VALUE_MIN, "bright_value_min")
        _fixed_float(green_red_ratio_min, YELLOW_GREEN_RED_RATIO, "green_red_ratio_min")
        _fixed_int(area_min, YELLOW_AREA_MIN, "area_min")
        _fixed_int(area_max, YELLOW_AREA_MAX, "area_max")
        _fixed_int(width_max, YELLOW_WIDTH_MAX, "width_max")
        _fixed_int(height_max, YELLOW_HEIGHT_MAX, "height_max")

        object.__setattr__(self, "geometry", actual_geometry)
        object.__setattr__(self, "pixel_spec", actual_spec)
        object.__setattr__(self, "transform_version", transform_version)
        object.__setattr__(self, "calibration_sha256", actual_calibration)
        object.__setattr__(self, "minimap_roi", actual_roi)
        object.__setattr__(self, "max_age_ns", max_age_ns)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "clock_domain", clock_domain)
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "candidate_confidence", confidence)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "bgr_target", target)
        object.__setattr__(self, "bgr_tolerance", YELLOW_BGR_TOLERANCE)
        object.__setattr__(self, "hue_min", YELLOW_HUE_MIN)
        object.__setattr__(self, "hue_max", YELLOW_HUE_MAX)
        object.__setattr__(self, "saturation_min", YELLOW_SATURATION_MIN)
        object.__setattr__(self, "value_min", YELLOW_VALUE_MIN)
        object.__setattr__(self, "bright_core_min", YELLOW_BRIGHT_CORE_MIN)
        object.__setattr__(self, "bright_b_max", YELLOW_BRIGHT_B_MAX)
        object.__setattr__(self, "bright_green_red_min", YELLOW_BRIGHT_GREEN_RED_MIN)
        object.__setattr__(self, "bright_saturation_min", YELLOW_BRIGHT_SATURATION_MIN)
        object.__setattr__(self, "bright_value_min", YELLOW_BRIGHT_VALUE_MIN)
        object.__setattr__(self, "green_red_ratio_min", YELLOW_GREEN_RED_RATIO)
        object.__setattr__(self, "area_min", YELLOW_AREA_MIN)
        object.__setattr__(self, "area_max", YELLOW_AREA_MAX)
        object.__setattr__(self, "width_max", YELLOW_WIDTH_MAX)
        object.__setattr__(self, "height_max", YELLOW_HEIGHT_MAX)

    @property
    def source_geometry(self) -> SourceGeometry:
        return self.geometry

    @property
    def geometry_sha256(self) -> str:
        return canonical_geometry_sha256(self.geometry)

    @property
    def calibration_digest(self) -> str:
        return self.calibration_sha256

    @property
    def target_bgr(self) -> tuple[int, int, int]:
        return self.bgr_target

    @property
    def hsv_s_min(self) -> int:
        return self.saturation_min

    @property
    def hsv_v_min(self) -> int:
        return self.value_min

    @property
    def min_area(self) -> int:
        return self.area_min

    @property
    def max_area(self) -> int:
        return self.area_max

    @property
    def max_component_width(self) -> int:
        return self.width_max

    @property
    def max_component_height(self) -> int:
        return self.height_max

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    @property
    def sha256(self) -> str:
        return self.digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "geometry": self.geometry.to_dict(),
            "pixel_spec": self.pixel_spec.to_dict(),
            "transform_version": self.transform_version,
            "calibration_sha256": self.calibration_sha256,
            "minimap_roi": None if self.minimap_roi is None else self.minimap_roi.to_dict(),
            "max_age_ns": self.max_age_ns,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "clock_domain": self.clock_domain,
            "subject_id": self.subject_id,
            "candidate_confidence": self.candidate_confidence,
            "bgr_target": list(self.bgr_target),
            "bgr_tolerance": self.bgr_tolerance,
            "hue_min": self.hue_min,
            "hue_max": self.hue_max,
            "saturation_min": self.saturation_min,
            "value_min": self.value_min,
            "bright_core_min": self.bright_core_min,
            "bright_b_max": self.bright_b_max,
            "bright_green_red_min": self.bright_green_red_min,
            "bright_saturation_min": self.bright_saturation_min,
            "bright_value_min": self.bright_value_min,
            "green_red_ratio_min": self.green_red_ratio_min,
            "area_min": self.area_min,
            "area_max": self.area_max,
            "width_max": self.width_max,
            "height_max": self.height_max,
        }

    to_hash_only_dict = to_dict
    to_hash_only = to_dict

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "MinimapMarkerConfig payload")
        try:
            raw_target = data.get("bgr_target", list(YELLOW_BGR))
            if not isinstance(raw_target, list | tuple):
                raise ValueError("bgr_target must be an array.")
            raw_roi = data.get("minimap_roi", data.get("roi"))
            result = cls(
                geometry=SourceGeometry.from_dict(data["geometry"]),
                pixel_spec=PixelSpec.from_dict(data["pixel_spec"]),
                transform_version=data["transform_version"],
                calibration_sha256=data["calibration_sha256"],
                minimap_roi=(
                    None
                    if raw_roi is None
                    else SourceRect.from_dict(cast(Mapping[str, Any], raw_roi))
                ),
                max_age_ns=data.get("max_age_ns"),
                session_id=data.get("session_id"),
                source_id=data.get("source_id"),
                clock_domain=data.get("clock_domain"),
                subject_id=data.get("subject_id", ANONYMOUS_PLAYER_SUBJECT),
                candidate_confidence=data.get("candidate_confidence", 1.0),
                version=data.get("version", MINIMAP_MARKER_CONFIG_VERSION),
                bgr_target=tuple(raw_target),
                bgr_tolerance=data.get("bgr_tolerance", YELLOW_BGR_TOLERANCE),
                hue_min=data.get("hue_min", YELLOW_HUE_MIN),
                hue_max=data.get("hue_max", YELLOW_HUE_MAX),
                saturation_min=data.get("saturation_min", YELLOW_SATURATION_MIN),
                value_min=data.get("value_min", YELLOW_VALUE_MIN),
                bright_core_min=data.get("bright_core_min", YELLOW_BRIGHT_CORE_MIN),
                bright_b_max=data["bright_b_max"],
                bright_green_red_min=data["bright_green_red_min"],
                bright_saturation_min=data["bright_saturation_min"],
                bright_value_min=data["bright_value_min"],
                green_red_ratio_min=data.get("green_red_ratio_min", YELLOW_GREEN_RED_RATIO),
                area_min=data.get("area_min", YELLOW_AREA_MIN),
                area_max=data.get("area_max", YELLOW_AREA_MAX),
                width_max=data.get("width_max", YELLOW_WIDTH_MAX),
                height_max=data.get("height_max", YELLOW_HEIGHT_MAX),
            )
        except KeyError as exc:
            raise ValueError(f"MinimapMarkerConfig payload missing key: {exc.args[0]}") from exc
        if "digest" in data and _normalise_sha256(data["digest"], "digest") != result.digest:
            raise ValueError("MinimapMarkerConfig digest mismatch.")
        return result


DEFAULT_MINIMAP_MARKER_CONFIG = MinimapMarkerConfig()


class MinimapMarkerStatus(str, Enum):
    """Extraction outcomes; only ``CANDIDATE`` carries a candidate."""

    CANDIDATE = "candidate"
    NO_CANDIDATE = "no_candidate"
    FAULT = "fault"

    # Common integration spellings retain the same wire values.
    FOUND = CANDIDATE
    UNKNOWN = NO_CANDIDATE
    INVALID = FAULT


class MinimapMarkerFaultCode(str, Enum):
    """Fail-closed validation and storage fault categories."""

    FRAME_TYPE = "frame_type"
    SESSION_MISMATCH = "session_mismatch"
    SOURCE_MISMATCH = "source_mismatch"
    CLOCK_DOMAIN_MISMATCH = "clock_domain_mismatch"
    TRANSFORM_MISMATCH = "transform_mismatch"
    GEOMETRY_MISMATCH = "geometry_mismatch"
    ROI_UNCONFIGURED = "roi_unconfigured"
    CALIBRATION_MISMATCH = "calibration_mismatch"
    IMAGE_REF_MISMATCH = "image_ref_mismatch"
    PIXEL_SPEC_MISMATCH = "pixel_spec_mismatch"
    PIXEL_MISSING = "pixel_missing"
    PIXEL_HASH_MISMATCH = "pixel_hash_mismatch"
    STALE = "stale"
    TIMESTAMP_MISMATCH = "timestamp_mismatch"
    EXTRACTION_ERROR = "extraction_error"

    # Aliases make fault checks readable for callers using capture vocabulary.
    FRAME_STALE = STALE
    FRAME_LINEAGE_MISMATCH = TIMESTAMP_MISMATCH


@dataclass(frozen=True, slots=True)
class MinimapMarkerComponent:
    """Hash-free geometry facts for one accepted yellow component."""

    source_bbox: SourceRect
    source_centroid: tuple[float, float]
    working_bbox: tuple[float, float, float, float]
    anchor_working: WorkingPoint
    area: int
    bright_core_pixels: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_bbox, SourceRect):
            raise TypeError("source_bbox must be SourceRect.")
        if not isinstance(self.source_centroid, tuple) or len(self.source_centroid) != 2:
            raise TypeError("source_centroid must be a two-item tuple.")
        centroid = tuple(
            _finite_number(value, f"source_centroid[{index}]")
            for index, value in enumerate(self.source_centroid)
        )
        if not (
            self.source_bbox.x <= centroid[0] <= self.source_bbox.x2
            and self.source_bbox.y <= centroid[1] <= self.source_bbox.y2
        ):
            raise ValueError("source_centroid must lie inside source_bbox.")
        if not isinstance(self.working_bbox, tuple) or len(self.working_bbox) != 4:
            raise TypeError("working_bbox must be a four-item tuple.")
        working_bbox = tuple(
            _finite_number(value, f"working_bbox[{index}]")
            for index, value in enumerate(self.working_bbox)
        )
        if working_bbox[2] <= working_bbox[0] or working_bbox[3] <= working_bbox[1]:
            raise ValueError("working_bbox must have positive width and height.")
        if not isinstance(self.anchor_working, WorkingPoint):
            raise TypeError("anchor_working must be WorkingPoint.")
        if not self.source_bbox.x <= self.anchor_source[0] <= self.source_bbox.x2:
            raise ValueError("anchor source x must lie inside source_bbox.")
        if not self.source_bbox.y <= self.anchor_source[1] <= self.source_bbox.y2:
            raise ValueError("anchor source y must lie inside source_bbox.")
        ensure_non_negative_int(self.area, "area")
        ensure_non_negative_int(self.bright_core_pixels, "bright_core_pixels")
        if self.bright_core_pixels > self.area:
            raise ValueError("bright_core_pixels must not exceed area.")
        object.__setattr__(self, "source_centroid", cast(tuple[float, float], centroid))
        object.__setattr__(
            self,
            "working_bbox",
            cast(tuple[float, float, float, float], working_bbox),
        )

    @property
    def bbox(self) -> SourceRect:
        return self.source_bbox

    @property
    def centroid(self) -> tuple[float, float]:
        return self.source_centroid

    @property
    def center(self) -> tuple[float, float]:
        return self.source_centroid

    @property
    def anchor(self) -> WorkingPoint:
        return self.anchor_working

    @property
    def anchor_source(self) -> tuple[float, float]:
        return self.source_centroid

    @property
    def width(self) -> int:
        return self.source_bbox.width

    @property
    def height(self) -> int:
        return self.source_bbox.height

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_bbox": self.source_bbox.to_dict(),
            "source_centroid": list(self.source_centroid),
            "working_bbox": list(self.working_bbox),
            "anchor_working": self.anchor_working.to_dict(),
            "area": self.area,
            "bright_core_pixels": self.bright_core_pixels,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "MinimapMarkerComponent payload")
        try:
            raw_centroid = data["source_centroid"]
            raw_working_bbox = data["working_bbox"]
            if not isinstance(raw_centroid, list | tuple) or len(raw_centroid) != 2:
                raise ValueError("source_centroid must be a two-item array.")
            if not isinstance(raw_working_bbox, list | tuple) or len(raw_working_bbox) != 4:
                raise ValueError("working_bbox must be a four-item array.")
            return cls(
                source_bbox=SourceRect.from_dict(data["source_bbox"]),
                source_centroid=tuple(raw_centroid),
                working_bbox=tuple(raw_working_bbox),
                anchor_working=WorkingPoint.from_dict(data["anchor_working"]),
                area=data["area"],
                bright_core_pixels=data["bright_core_pixels"],
            )
        except KeyError as exc:
            raise ValueError(f"MinimapMarkerComponent payload missing key: {exc.args[0]}") from exc


MarkerComponent = MinimapMarkerComponent
MinimapYellowMarkerComponent = MinimapMarkerComponent
YellowMarkerComponent = MinimapMarkerComponent


@dataclass(frozen=True, slots=True)
class MinimapMarkerEvidence:
    """Hash-only extraction evidence bound to one frame and config."""

    config_digest: str
    session_id: str
    source_id: str
    frame_id: int
    captured_at_ns: int
    received_at_ns: int
    checked_at_ns: int
    pixel_digest: str
    image_ref: str
    pixel_spec: PixelSpec
    geometry: SourceGeometry
    geometry_sha256: str
    calibration_sha256: str
    age_ns: int
    freshness_ns: int
    status: MinimapMarkerStatus
    components: tuple[MinimapMarkerComponent, ...] = field(default_factory=tuple)
    observed_at_ns: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "config_digest", _normalise_sha256(self.config_digest, "config_digest")
        )
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.source_id, "source_id")
        ensure_non_negative_int(self.frame_id, "frame_id")
        ensure_time_ns(self.captured_at_ns, "captured_at_ns")
        ensure_time_ns(self.received_at_ns, "received_at_ns")
        ensure_time_ns(self.checked_at_ns, "checked_at_ns")
        if self.received_at_ns < self.captured_at_ns:
            raise ValueError("received_at_ns must be >= captured_at_ns.")
        if self.checked_at_ns < self.captured_at_ns:
            raise ValueError("checked_at_ns must be >= captured_at_ns.")
        if self.checked_at_ns < self.received_at_ns:
            raise ValueError("checked_at_ns must be >= received_at_ns.")
        observed_at_ns = self.checked_at_ns if self.observed_at_ns is None else self.observed_at_ns
        ensure_time_ns(observed_at_ns, "observed_at_ns")
        if observed_at_ns < self.captured_at_ns:
            raise ValueError("observed_at_ns must be >= captured_at_ns.")
        if observed_at_ns < self.received_at_ns:
            raise ValueError("observed_at_ns must be >= received_at_ns.")
        if observed_at_ns > self.checked_at_ns:
            raise ValueError("observed_at_ns must be <= checked_at_ns.")
        object.__setattr__(self, "observed_at_ns", observed_at_ns)
        object.__setattr__(
            self, "pixel_digest", _normalise_sha256(self.pixel_digest, "pixel_digest")
        )
        ensure_non_empty_str(self.image_ref, "image_ref")
        object.__setattr__(self, "image_ref", f"cas://sha256/{self.pixel_digest.lower()}")
        if not isinstance(self.pixel_spec, PixelSpec):
            raise TypeError("pixel_spec must be PixelSpec.")
        if not isinstance(self.geometry, SourceGeometry):
            raise TypeError("geometry must be SourceGeometry.")
        expected_spec = PixelSpec(
            width=self.geometry.source_size.width,
            height=self.geometry.source_size.height,
        )
        if self.pixel_spec != expected_spec:
            raise ValueError("pixel_spec must match evidence geometry source_size.")
        geometry_digest = _normalise_sha256(self.geometry_sha256, "geometry_sha256")
        if geometry_digest != canonical_geometry_sha256(self.geometry):
            raise ValueError("geometry_sha256 does not match geometry.")
        object.__setattr__(self, "geometry_sha256", geometry_digest)
        calibration = _normalise_sha256(self.calibration_sha256, "calibration_sha256")
        object.__setattr__(self, "calibration_sha256", calibration)
        ensure_non_negative_int(self.age_ns, "age_ns")
        ensure_non_negative_int(self.freshness_ns, "freshness_ns")
        if not isinstance(self.status, MinimapMarkerStatus):
            raise TypeError("status must be MinimapMarkerStatus.")
        if not isinstance(self.components, tuple):
            raise TypeError("components must be a tuple.")
        if any(not isinstance(component, MinimapMarkerComponent) for component in self.components):
            raise TypeError("components must contain MinimapMarkerComponent values.")
        object.__setattr__(
            self,
            "components",
            tuple(
                sorted(self.components, key=lambda c: (c.source_bbox.y, c.source_bbox.x, c.digest))
            ),
        )
        if self.status is MinimapMarkerStatus.CANDIDATE and len(self.components) != 1:
            raise ValueError("candidate evidence must contain exactly one component.")

    @property
    def content_hash(self) -> str:
        return self.pixel_digest

    @property
    def component_count(self) -> int:
        return len(self.components)

    @property
    def marker(self) -> MinimapMarkerComponent | None:
        return self.components[0] if len(self.components) == 1 else None

    @property
    def evidence_digest(self) -> str:
        return self.digest

    @property
    def evidence_hash(self) -> str:
        return self.digest

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    @property
    def sha256(self) -> str:
        return self.digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MINIMAP_MARKER_SCHEMA_VERSION,
            "config_digest": self.config_digest,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "frame_id": self.frame_id,
            "captured_at_ns": self.captured_at_ns,
            "received_at_ns": self.received_at_ns,
            "checked_at_ns": self.checked_at_ns,
            "observed_at_ns": self.observed_at_ns,
            "pixel_digest": self.pixel_digest,
            "image_ref": self.image_ref,
            "pixel_spec": self.pixel_spec.to_dict(),
            "geometry": self.geometry.to_dict(),
            "geometry_sha256": self.geometry_sha256,
            "calibration_sha256": self.calibration_sha256,
            "age_ns": self.age_ns,
            "freshness_ns": self.freshness_ns,
            "status": self.status.value,
            "component_count": len(self.components),
            "components": [component.to_dict() for component in self.components],
        }

    def to_hash_only_dict(self) -> dict[str, Any]:
        return self.to_dict()

    to_hash_only = to_hash_only_dict

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "MinimapMarkerEvidence payload")
        try:
            raw_components = data.get("components", [])
            if not isinstance(raw_components, list | tuple):
                raise ValueError("components must be an array.")
            result = cls(
                config_digest=data["config_digest"],
                session_id=data["session_id"],
                source_id=data["source_id"],
                frame_id=data["frame_id"],
                captured_at_ns=data["captured_at_ns"],
                received_at_ns=data["received_at_ns"],
                checked_at_ns=data.get("checked_at_ns", data["received_at_ns"]),
                observed_at_ns=data.get(
                    "observed_at_ns",
                    data.get("checked_at_ns", data["received_at_ns"]),
                ),
                pixel_digest=data["pixel_digest"],
                image_ref=data["image_ref"],
                pixel_spec=PixelSpec.from_dict(data["pixel_spec"]),
                geometry=SourceGeometry.from_dict(data["geometry"]),
                geometry_sha256=data["geometry_sha256"],
                calibration_sha256=data["calibration_sha256"],
                age_ns=data["age_ns"],
                freshness_ns=data["freshness_ns"],
                status=MinimapMarkerStatus(data["status"]),
                components=tuple(
                    MinimapMarkerComponent.from_dict(cast(Mapping[str, Any], component))
                    for component in raw_components
                ),
            )
        except KeyError as exc:
            raise ValueError(f"MinimapMarkerEvidence payload missing key: {exc.args[0]}") from exc
        if "schema_version" in data and data["schema_version"] != MINIMAP_MARKER_SCHEMA_VERSION:
            raise ValueError("unsupported MinimapMarkerEvidence schema_version.")
        if "component_count" in data and data["component_count"] != result.component_count:
            raise ValueError("component_count contradicts components.")
        if "digest" in data and _normalise_sha256(data["digest"], "digest") != result.digest:
            raise ValueError("MinimapMarkerEvidence digest mismatch.")
        return result


MarkerEvidence = MinimapMarkerEvidence
MinimapMarkerExtractionEvidence = MinimapMarkerEvidence
MarkerExtractionEvidence = MinimapMarkerEvidence


@dataclass(frozen=True, slots=True)
class MinimapMarkerFault:
    """Hash-only fail-closed extraction fault."""

    code: MinimapMarkerFaultCode
    message: str
    config_digest: str
    session_id: str
    source_id: str
    frame_id: int
    failed_at_ns: int
    pixel_digest: str
    image_ref: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.code, MinimapMarkerFaultCode):
            raise TypeError("code must be MinimapMarkerFaultCode.")
        ensure_non_empty_str(self.message, "message")
        object.__setattr__(
            self, "config_digest", _normalise_sha256(self.config_digest, "config_digest")
        )
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.source_id, "source_id")
        ensure_non_negative_int(self.frame_id, "frame_id")
        ensure_time_ns(self.failed_at_ns, "failed_at_ns")
        object.__setattr__(
            self, "pixel_digest", _normalise_sha256(self.pixel_digest, "pixel_digest")
        )
        ensure_non_empty_str(self.image_ref, "image_ref")
        object.__setattr__(self, "image_ref", f"cas://sha256/{self.pixel_digest.lower()}")
        details = ensure_mapping(self.details, "details")
        ensure_json_value(details, "details")
        object.__setattr__(self, "details", freeze_json_value(details))

    @property
    def plan_suppressed(self) -> bool:
        return True

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "config_digest": self.config_digest,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "frame_id": self.frame_id,
            "failed_at_ns": self.failed_at_ns,
            "pixel_digest": self.pixel_digest,
            "image_ref": self.image_ref,
            "details": to_json_dict(self.details),
            "plan_suppressed": True,
        }

    to_hash_only_dict = to_dict
    to_hash_only = to_dict

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "MinimapMarkerFault payload")
        try:
            result = cls(
                code=MinimapMarkerFaultCode(data["code"]),
                message=data["message"],
                config_digest=data["config_digest"],
                session_id=data["session_id"],
                source_id=data["source_id"],
                frame_id=data["frame_id"],
                failed_at_ns=data["failed_at_ns"],
                pixel_digest=data["pixel_digest"],
                image_ref=data["image_ref"],
                details=ensure_mapping(data.get("details", {}), "details"),
            )
        except KeyError as exc:
            raise ValueError(f"MinimapMarkerFault payload missing key: {exc.args[0]}") from exc
        if "plan_suppressed" in data and data["plan_suppressed"] is not True:
            raise ValueError("MinimapMarkerFault must suppress planning.")
        if "digest" in data and _normalise_sha256(data["digest"], "digest") != result.digest:
            raise ValueError("MinimapMarkerFault digest mismatch.")
        return result


@dataclass(frozen=True, slots=True)
class MinimapMarkerResult:
    """Exactly one extraction branch with hash-only serialisation."""

    status: MinimapMarkerStatus
    candidate: PlayerCandidate | None
    evidence: MinimapMarkerEvidence | None
    fault: MinimapMarkerFault | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, MinimapMarkerStatus):
            raise TypeError("status must be MinimapMarkerStatus.")
        if self.evidence is not None and not isinstance(self.evidence, MinimapMarkerEvidence):
            raise TypeError("evidence must be MinimapMarkerEvidence or None.")
        if self.candidate is not None and not isinstance(self.candidate, PlayerCandidate):
            raise TypeError("candidate must be PlayerCandidate or None.")
        if self.fault is not None and not isinstance(self.fault, MinimapMarkerFault):
            raise TypeError("fault must be MinimapMarkerFault or None.")
        if self.status is MinimapMarkerStatus.CANDIDATE:
            if self.candidate is None or self.evidence is None or self.fault is not None:
                raise ValueError("candidate result requires candidate and evidence only.")
            if self.evidence.status is not MinimapMarkerStatus.CANDIDATE:
                raise ValueError("candidate evidence status mismatch.")
            if self.candidate.subject_id != ANONYMOUS_PLAYER_SUBJECT:
                raise ValueError("candidate subject must be the anonymous marker subject.")
            if self.candidate.evidence_digest != self.evidence.digest:
                raise ValueError("candidate evidence_digest must match evidence digest.")
            if self.candidate.pixel_digest != self.evidence.pixel_digest:
                raise ValueError("candidate pixel_digest must match evidence pixel_digest.")
            if (
                self.candidate.session_id != self.evidence.session_id
                or self.candidate.source_id != self.evidence.source_id
                or self.candidate.source_frame_id != self.evidence.frame_id
                or self.candidate.observed_at_ns != self.evidence.observed_at_ns
                or self.candidate.calibration_sha256 != self.evidence.calibration_sha256
                or self.candidate.working_size != self.evidence.geometry.working_size
                or self.candidate.evidence_source is not PlayerAnchorSource.MINIMAP_YELLOW_MARKER
            ):
                raise ValueError("candidate lineage does not match marker evidence.")
            marker = self.evidence.marker
            if marker is None or self.candidate.anchor_working != marker.anchor_working:
                raise ValueError("candidate anchor does not match marker evidence.")
        elif self.status is MinimapMarkerStatus.NO_CANDIDATE:
            if self.candidate is not None or self.fault is not None or self.evidence is None:
                raise ValueError("no-candidate result requires evidence and no candidate/fault.")
            if self.evidence.status is not MinimapMarkerStatus.NO_CANDIDATE:
                raise ValueError("no-candidate evidence status mismatch.")
        else:
            if self.candidate is not None or self.fault is None or self.evidence is None:
                raise ValueError("fault result requires evidence and fault only.")
            if self.evidence.status is not MinimapMarkerStatus.FAULT:
                raise ValueError("fault evidence status mismatch.")
            if (
                self.fault.config_digest != self.evidence.config_digest
                or self.fault.session_id != self.evidence.session_id
                or self.fault.source_id != self.evidence.source_id
                or self.fault.frame_id != self.evidence.frame_id
                or self.fault.pixel_digest != self.evidence.pixel_digest
                or self.fault.image_ref != self.evidence.image_ref
            ):
                raise ValueError("fault lineage does not match marker evidence.")

    @property
    def succeeded(self) -> bool:
        return self.status is MinimapMarkerStatus.CANDIDATE and self.candidate is not None

    @property
    def has_candidate(self) -> bool:
        return self.candidate is not None

    @property
    def player_candidate(self) -> PlayerCandidate | None:
        return self.candidate

    @property
    def content_hash(self) -> str | None:
        return None if self.evidence is None else self.evidence.pixel_digest

    @property
    def marker(self) -> MinimapMarkerComponent | None:
        return None if self.evidence is None else self.evidence.marker

    @property
    def components(self) -> tuple[MinimapMarkerComponent, ...]:
        return () if self.evidence is None else self.evidence.components

    @property
    def plan_suppressed(self) -> bool:
        return not self.succeeded

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    @property
    def sha256(self) -> str:
        return self.digest

    @property
    def result_digest(self) -> str:
        return self.digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "plan_suppressed": self.plan_suppressed,
            "candidate": None if self.candidate is None else self.candidate.to_dict(),
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "fault": None if self.fault is None else self.fault.to_dict(),
        }

    def to_hash_only_dict(self) -> dict[str, Any]:
        return self.to_dict()

    to_hash_only = to_hash_only_dict

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "MinimapMarkerResult payload")
        raw_candidate = data.get("candidate")
        raw_evidence = data.get("evidence")
        raw_fault = data.get("fault")
        if raw_candidate is not None and not isinstance(raw_candidate, Mapping):
            raise ValueError("candidate must be an object or null.")
        if raw_evidence is not None and not isinstance(raw_evidence, Mapping):
            raise ValueError("evidence must be an object or null.")
        if raw_fault is not None and not isinstance(raw_fault, Mapping):
            raise ValueError("fault must be an object or null.")
        result = cls(
            status=MinimapMarkerStatus(data["status"]),
            candidate=(None if raw_candidate is None else PlayerCandidate.from_dict(raw_candidate)),
            evidence=(
                None if raw_evidence is None else MinimapMarkerEvidence.from_dict(raw_evidence)
            ),
            fault=(None if raw_fault is None else MinimapMarkerFault.from_dict(raw_fault)),
        )
        if "plan_suppressed" in data and data["plan_suppressed"] is not result.plan_suppressed:
            raise ValueError("MinimapMarkerResult plan_suppressed contradicts status.")
        if "digest" in data and _normalise_sha256(data["digest"], "digest") != result.digest:
            raise ValueError("MinimapMarkerResult digest mismatch.")
        return result


MarkerResult = MinimapMarkerResult
MinimapMarkerExtractionResult = MinimapMarkerResult
MarkerExtractionResult = MinimapMarkerResult
YellowMarkerResult = MinimapMarkerResult


@dataclass(frozen=True, slots=True)
class _ExtractionFault(Exception):
    code: MinimapMarkerFaultCode
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


def _safe_details(value: object) -> object:
    """Reduce an exception detail to the strict, hash-only JSON subset."""

    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else repr(value)
    if isinstance(value, Mapping):
        return {str(key): _safe_details(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_safe_details(item) for item in value]
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


def _raise_fault(
    code: MinimapMarkerFaultCode,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> NoReturn:
    raise _ExtractionFault(code, message, {} if details is None else details)


def _looks_like_store(value: object) -> bool:
    return callable(getattr(value, "read", None))


class MinimapMarkerExtractor:
    """Extract one marker from a verified CAS-backed :class:`FramePacket`."""

    def __init__(
        self,
        config: MinimapMarkerConfig | PixelStoreReader | None = None,
        pixel_store: PixelStoreReader | MinimapMarkerConfig | None = None,
        *,
        cas: PixelStoreReader | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        # Accept both readable orderings: (config, pixel_store) and
        # (pixel_store, config).  Named arguments remain the preferred API.
        actual_config: MinimapMarkerConfig | None
        if isinstance(config, MinimapMarkerConfig):
            actual_config = config
            actual_store = pixel_store
        elif isinstance(pixel_store, MinimapMarkerConfig):
            actual_config = pixel_store
            actual_store = config
        else:
            actual_config = (
                None
                if config is None or _looks_like_store(config)
                else cast(MinimapMarkerConfig, config)
            )
            actual_store = config if _looks_like_store(config) else pixel_store
        if actual_config is None:
            actual_config = DEFAULT_MINIMAP_MARKER_CONFIG
        if not isinstance(actual_config, MinimapMarkerConfig):
            raise TypeError("config must be MinimapMarkerConfig.")
        if cas is not None:
            if actual_store is not None and actual_store is not cas:
                raise ValueError("pixel_store and cas must refer to the same store.")
            actual_store = cas
        if actual_store is None or not callable(getattr(actual_store, "read", None)):
            raise TypeError("pixel_store must expose read(digest, spec).")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable.")
        self.config = actual_config
        self.pixel_store = cast(PixelStoreReader, actual_store)
        self.cas = self.pixel_store
        self.clock = clock

    def _now(self, frame: FramePacket, now_ns: int | None) -> int:
        if now_ns is None:
            if self.clock is None:
                raise ValueError("marker extraction requires now_ns or a clock.")
            now_ns = self.clock()
        ensure_time_ns(now_ns, "now_ns")
        if now_ns < frame.received_at_ns:
            raise ValueError("now_ns must be >= received_at_ns in the frame clock domain.")
        return now_ns

    def _observed_at(self, frame: FramePacket, checked_at_ns: int, value: int | None) -> int:
        observed_at_ns = checked_at_ns if value is None else value
        try:
            ensure_time_ns(observed_at_ns, "observed_at_ns")
        except ValueError as exc:
            _raise_fault(
                MinimapMarkerFaultCode.TIMESTAMP_MISMATCH,
                "candidate observation timestamp is invalid",
                {"reason_type": type(exc).__name__},
            )
        if observed_at_ns < frame.received_at_ns or observed_at_ns > checked_at_ns:
            _raise_fault(
                MinimapMarkerFaultCode.TIMESTAMP_MISMATCH,
                "candidate observation timestamp is outside frame check interval",
            )
        return observed_at_ns

    def _generation(self, generation: int) -> int:
        try:
            ensure_non_negative_int(generation, "generation")
        except ValueError as exc:
            _raise_fault(
                MinimapMarkerFaultCode.EXTRACTION_ERROR,
                "candidate generation is invalid",
                {"reason_type": type(exc).__name__},
            )
        return generation

    def _freshness(self, frame: FramePacket, now_ns: int) -> tuple[int, int]:
        try:
            age_ns = frame.age_ns_at(now_ns)
            freshness_ns = frame.freshness_ns_at(now_ns)
        except ValueError as exc:
            _raise_fault(
                MinimapMarkerFaultCode.TIMESTAMP_MISMATCH,
                "frame freshness timestamp is invalid",
                {"reason_type": type(exc).__name__},
            )
        if age_ns < 0 or freshness_ns < 0:  # defensive invariant for static checkers
            _raise_fault(MinimapMarkerFaultCode.TIMESTAMP_MISMATCH, "frame freshness is negative")
        return age_ns, freshness_ns

    def _make_evidence(
        self,
        frame: FramePacket,
        *,
        now_ns: int,
        age_ns: int,
        freshness_ns: int,
        status: MinimapMarkerStatus,
        components: tuple[MinimapMarkerComponent, ...] = (),
        observed_at_ns: int | None = None,
        geometry: SourceGeometry | None = None,
        pixel_spec: PixelSpec | None = None,
        calibration_sha256: str | None = None,
    ) -> MinimapMarkerEvidence:
        actual_geometry = frame.source_geometry if geometry is None else geometry
        actual_spec = (
            PixelSpec(
                width=actual_geometry.source_size.width,
                height=actual_geometry.source_size.height,
            )
            if pixel_spec is None
            else pixel_spec
        )
        actual_calibration = (
            canonical_calibration_sha256(actual_geometry, frame.transform_version)
            if calibration_sha256 is None
            else calibration_sha256
        )
        canonical_image_ref = f"cas://sha256/{frame.content_hash.lower()}"
        return MinimapMarkerEvidence(
            config_digest=self.config.digest,
            session_id=frame.session_id,
            source_id=frame.source_id,
            frame_id=frame.frame_id,
            captured_at_ns=frame.captured_at_ns,
            received_at_ns=frame.received_at_ns,
            checked_at_ns=now_ns,
            observed_at_ns=observed_at_ns,
            pixel_digest=frame.content_hash,
            image_ref=canonical_image_ref,
            pixel_spec=actual_spec,
            geometry=actual_geometry,
            geometry_sha256=canonical_geometry_sha256(actual_geometry),
            calibration_sha256=actual_calibration,
            age_ns=age_ns,
            freshness_ns=freshness_ns,
            status=status,
            components=components,
        )

    def _fault_result(
        self,
        frame: FramePacket,
        *,
        now_ns: int,
        fault: _ExtractionFault,
        age_ns: int = 0,
        freshness_ns: int = 0,
        observed_at_ns: int | None = None,
    ) -> MinimapMarkerResult:
        evidence = self._make_evidence(
            frame,
            now_ns=now_ns,
            age_ns=max(0, age_ns),
            freshness_ns=max(0, freshness_ns),
            status=MinimapMarkerStatus.FAULT,
            components=(),
            observed_at_ns=observed_at_ns,
        )
        details = _safe_details(fault.details)
        if not isinstance(details, Mapping):
            details = {"detail": details}
        marker_fault = MinimapMarkerFault(
            code=fault.code,
            message=fault.message,
            config_digest=self.config.digest,
            session_id=frame.session_id,
            source_id=frame.source_id,
            frame_id=frame.frame_id,
            failed_at_ns=now_ns,
            pixel_digest=frame.content_hash,
            image_ref=f"cas://sha256/{frame.content_hash.lower()}",
            details=cast(Mapping[str, Any], details),
        )
        return MinimapMarkerResult(
            status=MinimapMarkerStatus.FAULT,
            candidate=None,
            evidence=evidence,
            fault=marker_fault,
        )

    def _validate_frame(self, frame: FramePacket, now_ns: int) -> tuple[int, int, PixelSpec, str]:
        age_ns, freshness_ns = self._freshness(frame, now_ns)
        config = self.config
        if config.session_id is not None and frame.session_id != config.session_id:
            _raise_fault(
                MinimapMarkerFaultCode.SESSION_MISMATCH,
                "frame session does not match frozen marker config",
            )
        if config.source_id is not None and frame.source_id != config.source_id:
            _raise_fault(
                MinimapMarkerFaultCode.SOURCE_MISMATCH,
                "frame source does not match frozen marker config",
            )
        if config.clock_domain is not None and frame.clock_domain != config.clock_domain:
            _raise_fault(
                MinimapMarkerFaultCode.CLOCK_DOMAIN_MISMATCH,
                "frame clock domain does not match frozen marker config",
            )
        if frame.transform_version != config.transform_version:
            _raise_fault(
                MinimapMarkerFaultCode.TRANSFORM_MISMATCH,
                "frame transform version does not match frozen marker config",
            )
        if frame.source_geometry != config.geometry:
            _raise_fault(
                MinimapMarkerFaultCode.GEOMETRY_MISMATCH,
                "frame source geometry does not match frozen marker config",
            )
        roi = config.minimap_roi
        if roi is None:
            _raise_fault(
                MinimapMarkerFaultCode.ROI_UNCONFIGURED,
                "minimap ROI is not configured",
            )
        content_rect = config.geometry.content_rect
        if not (
            content_rect.x <= roi.x
            and content_rect.y <= roi.y
            and roi.x2 <= content_rect.x2
            and roi.y2 <= content_rect.y2
        ):
            _raise_fault(
                MinimapMarkerFaultCode.ROI_UNCONFIGURED,
                "minimap ROI is outside geometry.content_rect",
            )
        expected_image_ref = f"cas://sha256/{frame.content_hash.lower()}"
        if frame.image_ref != expected_image_ref:
            _raise_fault(
                MinimapMarkerFaultCode.IMAGE_REF_MISMATCH,
                "frame image_ref does not match content_hash",
            )
        actual_calibration = canonical_calibration_sha256(
            frame.source_geometry,
            frame.transform_version,
        )
        if actual_calibration != config.calibration_sha256:
            _raise_fault(
                MinimapMarkerFaultCode.CALIBRATION_MISMATCH,
                "frame calibration does not match frozen marker config",
            )
        if config.max_age_ns is not None and frame.capture_health.max_age_ns != config.max_age_ns:
            _raise_fault(
                MinimapMarkerFaultCode.TIMESTAMP_MISMATCH,
                "frame freshness lease does not match frozen marker config",
            )
        if not frame.is_fresh_at(now_ns):
            _raise_fault(
                MinimapMarkerFaultCode.STALE,
                "frame freshness lease has expired",
                {"age_ns": age_ns, "max_age_ns": frame.capture_health.max_age_ns},
            )

        metadata = frame.image_metadata
        for name, expected in (
            ("geometry_sha256", canonical_geometry_sha256(config.geometry)),
            ("calibration_sha256", config.calibration_sha256),
            ("transform_version", config.transform_version),
            ("content_hash", frame.content_hash.lower()),
            ("pixel_digest", frame.content_hash.lower()),
        ):
            supplied = metadata.get(name)
            if supplied is not None:
                if name.endswith("sha256") or name in {"content_hash", "pixel_digest"}:
                    try:
                        actual = _normalise_sha256(supplied, name)
                    except ValueError:
                        _raise_fault(
                            MinimapMarkerFaultCode.CALIBRATION_MISMATCH
                            if name == "calibration_sha256"
                            else MinimapMarkerFaultCode.PIXEL_HASH_MISMATCH,
                            f"{name} attestation is invalid",
                        )
                    if actual != expected:
                        _raise_fault(
                            MinimapMarkerFaultCode.CALIBRATION_MISMATCH
                            if name in {"geometry_sha256", "calibration_sha256"}
                            else MinimapMarkerFaultCode.PIXEL_HASH_MISMATCH,
                            f"{name} attestation does not match frame lineage",
                        )
                elif supplied != expected:
                    _raise_fault(
                        MinimapMarkerFaultCode.TRANSFORM_MISMATCH,
                        f"{name} attestation does not match frame lineage",
                    )
        raw_spec = metadata.get("pixel_spec")
        spec = config.pixel_spec
        if raw_spec is not None:
            try:
                supplied_spec = (
                    raw_spec
                    if isinstance(raw_spec, PixelSpec)
                    else PixelSpec.from_dict(cast(Mapping[str, Any], raw_spec))
                )
            except (TypeError, ValueError) as exc:
                _raise_fault(
                    MinimapMarkerFaultCode.PIXEL_SPEC_MISMATCH,
                    "frame pixel specification is invalid",
                    {"reason_type": type(exc).__name__},
                )
            if supplied_spec != spec:
                _raise_fault(
                    MinimapMarkerFaultCode.PIXEL_SPEC_MISMATCH,
                    "frame pixel specification does not match source geometry",
                )
        return age_ns, freshness_ns, spec, actual_calibration

    def _read_pixels(self, frame: FramePacket, spec: PixelSpec) -> bytes:
        digest = frame.content_hash.lower()
        try:
            raw = self.pixel_store.read(digest, spec)
        except Exception as exc:
            # Storage paths and exception text are intentionally not copied to
            # evidence.  The fault retains only its class and the frame hash.
            _raise_fault(
                MinimapMarkerFaultCode.PIXEL_MISSING,
                "pixel CAS object could not be read",
                {"reason_type": type(exc).__name__},
            )
        try:
            pixels = validate_pixels(spec, raw)
            actual = pixel_digest(spec, pixels)
        except Exception as exc:
            _raise_fault(
                MinimapMarkerFaultCode.PIXEL_HASH_MISMATCH,
                "pixel CAS bytes failed Pixel V1 validation",
                {"reason_type": type(exc).__name__},
            )
        if actual != digest:
            _raise_fault(
                MinimapMarkerFaultCode.PIXEL_HASH_MISMATCH,
                "pixel CAS bytes do not match FramePacket.content_hash",
            )
        return pixels

    def _components(
        self,
        pixels: bytes,
        spec: PixelSpec,
        geometry: SourceGeometry,
    ) -> tuple[MinimapMarkerComponent, ...]:
        # The source bytes are viewed at the exact PixelSpec shape. Detection
        # is bounded by the digest-bound minimap ROI, never by the full
        # content rectangle.
        source = np.frombuffer(pixels, dtype=np.uint8).reshape(spec.shape)
        roi = self.config.minimap_roi
        if roi is None:
            _raise_fault(
                MinimapMarkerFaultCode.ROI_UNCONFIGURED,
                "minimap ROI is not configured",
            )
        source_roi = source[roi.y : roi.y2, roi.x : roi.x2]
        target = np.asarray(self.config.bgr_target, dtype=np.uint8)
        tolerance = np.full((3,), self.config.bgr_tolerance, dtype=np.uint8)
        lower = np.maximum(target.astype(np.int16) - tolerance.astype(np.int16), 0).astype(np.uint8)
        upper = np.minimum(target.astype(np.int16) + tolerance.astype(np.int16), 255).astype(
            np.uint8
        )
        bgr_mask = cv2.inRange(source_roi, lower, upper) != 0

        hsv = cv2.cvtColor(source_roi, cv2.COLOR_BGR2HSV)
        hsv_mask = (
            (hsv[:, :, 0] >= self.config.hue_min)
            & (hsv[:, :, 0] <= self.config.hue_max)
            & (hsv[:, :, 1] >= self.config.saturation_min)
            & (hsv[:, :, 2] >= self.config.value_min)
        )
        # The hue floor is the explicit orange exclusion.  The channel-ratio
        # gate also rejects red-heavy orange edge pixels that fall in the
        # broad BGR tolerance band.
        green = source_roi[:, :, 1].astype(np.float32)
        red = source_roi[:, :, 2].astype(np.float32)
        ratio_mask = green >= self.config.green_red_ratio_min * np.maximum(red, 1.0)
        mask = bgr_mask & hsv_mask & ratio_mask

        # The core is intentionally stricter and independent from the broad
        # component mask.  Its count is joined to labels below in one ROI-wide
        # aggregation, so near-yellow edge pixels cannot satisfy the core rule.
        bright_core = (
            (source_roi[:, :, 0] <= self.config.bright_b_max)
            & (source_roi[:, :, 1] >= self.config.bright_green_red_min)
            & (source_roi[:, :, 2] >= self.config.bright_green_red_min)
            & (hsv[:, :, 1] >= self.config.bright_saturation_min)
            & (hsv[:, :, 2] >= self.config.bright_value_min)
        )
        labels_count, labels, stats, centroids = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8), connectivity=8
        )
        core_counts = np.bincount(
            labels.reshape(-1),
            weights=bright_core.reshape(-1).astype(np.int64),
            minlength=labels_count,
        )
        components: list[MinimapMarkerComponent] = []
        for label in range(1, labels_count):
            x = roi.x + int(stats[label, cv2.CC_STAT_LEFT])
            y = roi.y + int(stats[label, cv2.CC_STAT_TOP])
            width = int(stats[label, cv2.CC_STAT_WIDTH])
            height = int(stats[label, cv2.CC_STAT_HEIGHT])
            area = int(stats[label, cv2.CC_STAT_AREA])
            core_pixels = int(core_counts[label])
            if not (
                self.config.area_min <= area <= self.config.area_max
                and width <= self.config.width_max
                and height <= self.config.height_max
                and core_pixels >= self.config.bright_core_min
            ):
                continue
            source_bbox = SourceRect(x=x, y=y, width=width, height=height)
            centroid = (
                roi.x + float(centroids[label, 0]),
                roi.y + float(centroids[label, 1]),
            )
            working_left, working_top = geometry.source_to_working(float(x), float(y))
            working_right, working_bottom = geometry.source_to_working(
                float(x + width), float(y + height)
            )
            anchor_x, anchor_y = geometry.source_to_working(*centroid)
            anchor = WorkingPoint(
                x=anchor_x,
                y=anchor_y,
                working_size=geometry.working_size,
            )
            components.append(
                MinimapMarkerComponent(
                    source_bbox=source_bbox,
                    source_centroid=centroid,
                    working_bbox=(working_left, working_top, working_right, working_bottom),
                    anchor_working=anchor,
                    area=area,
                    bright_core_pixels=core_pixels,
                )
            )
        return tuple(sorted(components, key=lambda c: (c.source_bbox.y, c.source_bbox.x, c.digest)))

    def extract(
        self,
        frame: FramePacket,
        now_ns: int | None = None,
        observed_at_ns: int | None = None,
        generation: int = 0,
    ) -> MinimapMarkerResult:
        """Return one deterministic marker result for ``frame``.

        Type errors at the API boundary follow the rest of the package and
        are raised immediately.  Once a real ``FramePacket`` is supplied, all
        lineage, freshness, CAS, and detector failures become a fault result
        with no candidate.
        """

        if not isinstance(frame, FramePacket):
            raise TypeError("frame must be FramePacket.")
        try:
            checked_at_ns = self._now(frame, now_ns)
        except Exception as exc:
            # Keep a valid terminal timestamp for the fail-closed record;
            # this is not used as a successful observation timestamp.
            checked_at_ns = frame.received_at_ns
            fault = _ExtractionFault(
                MinimapMarkerFaultCode.TIMESTAMP_MISMATCH,
                "marker extraction clock returned an invalid timestamp",
                {"reason_type": type(exc).__name__},
            )
            return self._fault_result(frame, now_ns=checked_at_ns, fault=fault)

        actual_observed_at_ns: int | None = None
        try:
            actual_observed_at_ns = self._observed_at(
                frame,
                checked_at_ns,
                observed_at_ns,
            )
            actual_generation = self._generation(generation)
            age_ns, freshness_ns, spec, calibration = self._validate_frame(frame, checked_at_ns)
            pixels = self._read_pixels(frame, spec)
            components = self._components(pixels, spec, frame.source_geometry)
            if len(components) != 1:
                evidence = self._make_evidence(
                    frame,
                    now_ns=checked_at_ns,
                    age_ns=age_ns,
                    freshness_ns=freshness_ns,
                    status=MinimapMarkerStatus.NO_CANDIDATE,
                    components=components,
                    observed_at_ns=actual_observed_at_ns,
                    pixel_spec=spec,
                    calibration_sha256=calibration,
                )
                return MinimapMarkerResult(
                    status=MinimapMarkerStatus.NO_CANDIDATE,
                    candidate=None,
                    evidence=evidence,
                )

            evidence = self._make_evidence(
                frame,
                now_ns=checked_at_ns,
                age_ns=age_ns,
                freshness_ns=freshness_ns,
                status=MinimapMarkerStatus.CANDIDATE,
                components=components,
                observed_at_ns=actual_observed_at_ns,
                pixel_spec=spec,
                calibration_sha256=calibration,
            )
            component = components[0]
            candidate = PlayerCandidate(
                session_id=frame.session_id,
                source_id=frame.source_id,
                source_frame_id=frame.frame_id,
                observed_at_ns=actual_observed_at_ns,
                generation=actual_generation,
                subject_id=self.config.subject_id,
                confidence=self.config.candidate_confidence,
                visibility=Visibility.VISIBLE,
                evidence_source=PlayerAnchorSource.MINIMAP_YELLOW_MARKER,
                evidence_digest=evidence.digest,
                pixel_digest=frame.content_hash,
                calibration_sha256=calibration,
                working_size=frame.source_geometry.working_size,
                anchor_working=component.anchor_working,
            )
            return MinimapMarkerResult(
                status=MinimapMarkerStatus.CANDIDATE,
                candidate=candidate,
                evidence=evidence,
            )
        except _ExtractionFault as extraction_fault:
            try:
                age_ns, freshness_ns = self._freshness(frame, checked_at_ns)
            except _ExtractionFault:
                age_ns, freshness_ns = 0, 0
            return self._fault_result(
                frame,
                now_ns=checked_at_ns,
                fault=extraction_fault,
                age_ns=age_ns,
                freshness_ns=freshness_ns,
                observed_at_ns=actual_observed_at_ns,
            )
        except (TypeError, ValueError, cv2.error) as exc:
            # Decoder/library errors are still fail-closed and are represented
            # without copying exception paths or raw arrays into evidence.
            library_fault = _ExtractionFault(
                MinimapMarkerFaultCode.EXTRACTION_ERROR,
                "yellow marker extraction failed closed",
                {"reason_type": type(exc).__name__},
            )
            try:
                age_ns, freshness_ns = self._freshness(frame, checked_at_ns)
            except _ExtractionFault:
                age_ns, freshness_ns = 0, 0
            return self._fault_result(
                frame,
                now_ns=checked_at_ns,
                fault=library_fault,
                age_ns=age_ns,
                freshness_ns=freshness_ns,
                observed_at_ns=actual_observed_at_ns,
            )

    def extract_candidate(
        self,
        frame: FramePacket,
        now_ns: int | None = None,
        observed_at_ns: int | None = None,
        generation: int = 0,
    ) -> PlayerCandidate | None:
        """Return only the unique candidate, suppressing all other branches."""

        return self.extract(
            frame,
            now_ns=now_ns,
            observed_at_ns=observed_at_ns,
            generation=generation,
        ).candidate

    def __call__(
        self,
        frame: FramePacket,
        now_ns: int | None = None,
        observed_at_ns: int | None = None,
        generation: int = 0,
    ) -> MinimapMarkerResult:
        return self.extract(
            frame,
            now_ns=now_ns,
            observed_at_ns=observed_at_ns,
            generation=generation,
        )


MinimapYellowMarkerExtractor = MinimapMarkerExtractor
YellowMarkerExtractor = MinimapMarkerExtractor


def extract_minimap_marker(
    frame: FramePacket,
    pixel_store: PixelStoreReader | None = None,
    config: MinimapMarkerConfig | None = None,
    *,
    cas: PixelStoreReader | None = None,
    now_ns: int | None = None,
    clock: Callable[[], int] | None = None,
    observed_at_ns: int | None = None,
    generation: int = 0,
) -> MinimapMarkerResult:
    """Functional wrapper around :class:`MinimapMarkerExtractor`."""

    return MinimapMarkerExtractor(
        config=config,
        pixel_store=pixel_store,
        cas=cas,
        clock=clock,
    ).extract(
        frame,
        now_ns=now_ns,
        observed_at_ns=observed_at_ns,
        generation=generation,
    )


extract_minimap_yellow_marker = extract_minimap_marker
extract_yellow_marker = extract_minimap_marker
extract_marker = extract_minimap_marker
extract_minimap_candidate = extract_minimap_marker


def extract_player_candidate(
    frame: FramePacket,
    pixel_store: PixelStoreReader | None = None,
    config: MinimapMarkerConfig | None = None,
    *,
    cas: PixelStoreReader | None = None,
    now_ns: int | None = None,
    clock: Callable[[], int] | None = None,
    observed_at_ns: int | None = None,
    generation: int = 0,
) -> PlayerCandidate | None:
    """Functional candidate-only convenience wrapper."""

    return extract_minimap_marker(
        frame,
        pixel_store,
        config,
        cas=cas,
        now_ns=now_ns,
        clock=clock,
        observed_at_ns=observed_at_ns,
        generation=generation,
    ).candidate


find_minimap_marker = extract_minimap_marker
extract_marker_candidate = extract_player_candidate

# Compatibility spellings used by integrations that name the policy after
# the extraction step rather than the image region.
MinimapMarkerExtractionConfig = MinimapMarkerConfig
MarkerExtractionConfig = MinimapMarkerConfig
YellowMarkerConfig = MinimapMarkerConfig
MinimapYellowMarkerConfig = MinimapMarkerConfig
MarkerStatus = MinimapMarkerStatus
MarkerFaultCode = MinimapMarkerFaultCode
MinimapMarkerExtractionFaultCode = MinimapMarkerFaultCode


__all__ = [
    "ANONYMOUS_PLAYER_SUBJECT",
    "DEFAULT_MINIMAP_MARKER_CONFIG",
    "YELLOW_AREA_MAX",
    "YELLOW_AREA_MIN",
    "YELLOW_BGR",
    "YELLOW_BGR_TOLERANCE",
    "YELLOW_BRIGHT_B_MAX",
    "YELLOW_BRIGHT_CORE_MIN",
    "YELLOW_BRIGHT_GREEN_RED_MIN",
    "YELLOW_BRIGHT_SATURATION_MIN",
    "YELLOW_BRIGHT_VALUE_MIN",
    "YELLOW_GREEN_RED_RATIO",
    "YELLOW_HEIGHT_MAX",
    "YELLOW_HUE_MAX",
    "YELLOW_HUE_MIN",
    "YELLOW_SATURATION_MIN",
    "YELLOW_VALUE_MIN",
    "YELLOW_WIDTH_MAX",
    "MarkerComponent",
    "MarkerEvidence",
    "MarkerExtractionConfig",
    "MarkerExtractionEvidence",
    "MarkerExtractionResult",
    "MarkerFaultCode",
    "MarkerResult",
    "MarkerStatus",
    "MinimapMarkerComponent",
    "MinimapMarkerConfig",
    "MinimapMarkerEvidence",
    "MinimapMarkerExtractionConfig",
    "MinimapMarkerExtractionEvidence",
    "MinimapMarkerExtractionFaultCode",
    "MinimapMarkerExtractionResult",
    "MinimapMarkerExtractor",
    "MinimapMarkerFault",
    "MinimapMarkerFaultCode",
    "MinimapMarkerResult",
    "MinimapMarkerSchemaVersion",
    "MinimapMarkerStatus",
    "MinimapYellowMarkerComponent",
    "MinimapYellowMarkerConfig",
    "MinimapYellowMarkerExtractor",
    "YellowMarkerComponent",
    "YellowMarkerConfig",
    "YellowMarkerExtractor",
    "YellowMarkerResult",
    "extract_marker",
    "extract_marker_candidate",
    "extract_minimap_candidate",
    "extract_minimap_marker",
    "extract_minimap_yellow_marker",
    "extract_player_candidate",
    "extract_yellow_marker",
    "find_minimap_marker",
]


# A descriptive alias avoids forcing callers to remember the all-caps schema
# constant while retaining the canonical constant above.
MinimapMarkerSchemaVersion = MINIMAP_MARKER_SCHEMA_VERSION
