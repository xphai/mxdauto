"""Pure, fail-closed player localization over frozen observation lineage.

The mob detector is deliberately not treated as player identity evidence.  A
caller supplies an independent, anonymous player anchor (for the pilot this is
the minimap yellow marker) and this module cross-binds it to the matching
``ObservationResult`` before applying the frozen map transform and platform
graph.  No clock, device, raw pixel, or mutable global state is accessed here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from math import isfinite
from typing import Any, Self

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
from maple_automation_core.domain.coordinates import PixelCoordinate, WorldCoordinate
from maple_automation_core.domain.frame import FrameSize
from maple_automation_core.domain.observation import Observation, ObservationResult
from maple_automation_core.domain.player_world import PlayerState, Visibility

from .platform import PlatformGraph, PlatformMatchStatus
from .transform import LocalizationTransform


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a finite number.")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{field_name} must be a finite number.")
    return 0.0 if result == 0.0 else result


def _normalise_sha256(value: str, field_name: str) -> str:
    ensure_sha256_hex(value, field_name)
    return value.lower()


@dataclass(frozen=True, slots=True)
class WorkingPoint:
    """Floating-point foot anchor in half-open observation working space."""

    x: float
    y: float
    working_size: FrameSize

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _finite_number(self.x, "x"))
        object.__setattr__(self, "y", _finite_number(self.y, "y"))
        if not isinstance(self.working_size, FrameSize):
            raise TypeError("working_size must be FrameSize.")
        if not (0.0 <= self.x < self.working_size.width):
            raise ValueError("x must be inside the half-open working width.")
        if not (0.0 <= self.y < self.working_size.height):
            raise ValueError("y must be inside the half-open working height.")

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "working_size": self.working_size.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "WorkingPoint payload")
        try:
            return cls(
                x=data["x"],
                y=data["y"],
                working_size=FrameSize.from_dict(data["working_size"]),
            )
        except KeyError as exc:
            raise ValueError(f"WorkingPoint payload missing key: {exc.args[0]}") from exc

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())


class PlayerAnchorSource(str, Enum):
    MINIMAP_YELLOW_MARKER = "minimap_yellow_marker"
    REPLAY_FIXTURE = "replay_fixture"


@dataclass(frozen=True, slots=True)
class PlayerCandidate:
    """Anonymous player evidence produced independently of the mob detector."""

    session_id: str
    source_id: str
    source_frame_id: int
    observed_at_ns: int
    generation: int
    subject_id: str
    confidence: float
    visibility: Visibility
    evidence_source: PlayerAnchorSource
    evidence_digest: str
    pixel_digest: str
    calibration_sha256: str
    working_size: FrameSize
    anchor_working: WorkingPoint | None

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.source_id, "source_id")
        ensure_non_negative_int(self.source_frame_id, "source_frame_id")
        ensure_time_ns(self.observed_at_ns, "observed_at_ns")
        ensure_non_negative_int(self.generation, "generation")
        ensure_non_empty_str(self.subject_id, "subject_id")
        ensure_probability(self.confidence, "confidence")
        object.__setattr__(self, "confidence", float(self.confidence))
        if not isinstance(self.visibility, Visibility):
            raise TypeError("visibility must be Visibility.")
        if not isinstance(self.evidence_source, PlayerAnchorSource):
            raise TypeError("evidence_source must be PlayerAnchorSource.")
        object.__setattr__(
            self,
            "evidence_digest",
            _normalise_sha256(self.evidence_digest, "evidence_digest"),
        )
        object.__setattr__(
            self,
            "pixel_digest",
            _normalise_sha256(self.pixel_digest, "pixel_digest"),
        )
        object.__setattr__(
            self,
            "calibration_sha256",
            _normalise_sha256(self.calibration_sha256, "calibration_sha256"),
        )
        if not isinstance(self.working_size, FrameSize):
            raise TypeError("working_size must be FrameSize.")
        if self.visibility is Visibility.LOST:
            if self.anchor_working is not None:
                raise ValueError("lost candidate anchor_working must be None.")
        elif not isinstance(self.anchor_working, WorkingPoint):
            raise TypeError("non-lost candidate requires WorkingPoint anchor_working.")
        if (
            self.anchor_working is not None
            and self.anchor_working.working_size != self.working_size
        ):
            raise ValueError("candidate anchor working_size mismatch.")

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    @property
    def sort_key(self) -> tuple[float | str, ...]:
        point = self.anchor_working
        return (
            -self.confidence,
            self.subject_id,
            self.evidence_source.value,
            -1.0 if point is None else point.y,
            -1.0 if point is None else point.x,
            self.evidence_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source_id": self.source_id,
            "source_frame_id": self.source_frame_id,
            "observed_at_ns": self.observed_at_ns,
            "generation": self.generation,
            "subject_id": self.subject_id,
            "confidence": self.confidence,
            "visibility": self.visibility.value,
            "evidence_source": self.evidence_source.value,
            "evidence_digest": self.evidence_digest,
            "pixel_digest": self.pixel_digest,
            "calibration_sha256": self.calibration_sha256,
            "working_size": self.working_size.to_dict(),
            "anchor_working": (
                None if self.anchor_working is None else self.anchor_working.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "PlayerCandidate payload")
        try:
            anchor = data.get("anchor_working")
            return cls(
                session_id=data["session_id"],
                source_id=data["source_id"],
                source_frame_id=data["source_frame_id"],
                observed_at_ns=data["observed_at_ns"],
                generation=data["generation"],
                subject_id=data["subject_id"],
                confidence=data["confidence"],
                visibility=Visibility(data["visibility"]),
                evidence_source=PlayerAnchorSource(data["evidence_source"]),
                evidence_digest=data["evidence_digest"],
                pixel_digest=data["pixel_digest"],
                calibration_sha256=data["calibration_sha256"],
                working_size=FrameSize.from_dict(data["working_size"]),
                anchor_working=(None if anchor is None else WorkingPoint.from_dict(anchor)),
            )
        except KeyError as exc:
            raise ValueError(f"PlayerCandidate payload missing key: {exc.args[0]}") from exc


class IdentityStatus(str, Enum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"


class LocalizationStatus(str, Enum):
    LOCATED = "located"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PlayerLocation:
    """Typed player location retaining all map and source provenance."""

    session_id: str
    source_id: str
    source_frame_id: int
    observed_at_ns: int
    as_of_ns: int
    generation: int
    subject_id: str | None
    identity_status: IdentityStatus
    visibility: Visibility
    anchor_working: WorkingPoint | None
    world_position: WorldCoordinate | None
    map_id: str
    profile_id: str
    platform_id: str | None
    confidence: float
    transform_version: str
    transform_digest: str
    platform_graph_digest: str
    calibration_sha256: str
    working_size: FrameSize
    pixel_digest: str
    observation_digest: str
    evidence_digest: str | None
    freshness_ns: int
    status: LocalizationStatus

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.source_id, "source_id")
        ensure_non_negative_int(self.source_frame_id, "source_frame_id")
        ensure_time_ns(self.observed_at_ns, "observed_at_ns")
        ensure_time_ns(self.as_of_ns, "as_of_ns")
        if self.as_of_ns < self.observed_at_ns:
            raise ValueError("as_of_ns must not precede observed_at_ns.")
        ensure_non_negative_int(self.generation, "generation")
        if self.subject_id is not None:
            ensure_non_empty_str(self.subject_id, "subject_id")
        if not isinstance(self.identity_status, IdentityStatus):
            raise TypeError("identity_status must be IdentityStatus.")
        if self.identity_status is IdentityStatus.CONFIRMED and self.subject_id is None:
            raise ValueError("confirmed identity requires subject_id.")
        if self.identity_status is not IdentityStatus.CONFIRMED and self.subject_id is not None:
            raise ValueError("unconfirmed identity must not bind subject_id.")
        if not isinstance(self.visibility, Visibility):
            raise TypeError("visibility must be Visibility.")
        if self.anchor_working is not None and not isinstance(self.anchor_working, WorkingPoint):
            raise TypeError("anchor_working must be WorkingPoint or None.")
        if self.world_position is not None and not isinstance(self.world_position, WorldCoordinate):
            raise TypeError("world_position must be WorldCoordinate or None.")
        ensure_non_empty_str(self.map_id, "map_id")
        ensure_non_empty_str(self.profile_id, "profile_id")
        if self.platform_id is not None:
            ensure_non_empty_str(self.platform_id, "platform_id")
        ensure_probability(self.confidence, "confidence")
        object.__setattr__(self, "confidence", float(self.confidence))
        ensure_non_empty_str(self.transform_version, "transform_version")
        for field_name in (
            "transform_digest",
            "platform_graph_digest",
            "calibration_sha256",
            "pixel_digest",
            "observation_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalise_sha256(getattr(self, field_name), field_name),
            )
        if self.evidence_digest is not None:
            object.__setattr__(
                self,
                "evidence_digest",
                _normalise_sha256(self.evidence_digest, "evidence_digest"),
            )
        if not isinstance(self.working_size, FrameSize):
            raise TypeError("working_size must be FrameSize.")
        ensure_non_negative_int(self.freshness_ns, "freshness_ns")
        if self.freshness_ns != self.as_of_ns - self.observed_at_ns:
            raise ValueError("freshness_ns must equal as_of_ns - observed_at_ns.")
        if not isinstance(self.status, LocalizationStatus):
            raise TypeError("status must be LocalizationStatus.")
        has_coordinates = self.anchor_working is not None and self.world_position is not None
        if (self.anchor_working is None) != (self.world_position is None):
            raise ValueError("anchor_working and world_position must appear together.")
        if (
            self.anchor_working is not None
            and self.anchor_working.working_size != self.working_size
        ):
            raise ValueError("location anchor working_size mismatch.")
        if self.visibility is Visibility.LOST and has_coordinates:
            raise ValueError("lost location must not contain coordinates.")
        if self.status is LocalizationStatus.LOCATED and (
            not has_coordinates
            or self.platform_id is None
            or self.identity_status is not IdentityStatus.CONFIRMED
            or self.visibility is not Visibility.VISIBLE
        ):
            raise ValueError("located status requires visible confirmed platform coordinates.")
        if self.status is LocalizationStatus.UNKNOWN and (
            has_coordinates or self.platform_id is not None
        ):
            raise ValueError("unknown status must not bind coordinates or a platform.")
        if self.status is LocalizationStatus.DEGRADED and (
            not has_coordinates
            or self.identity_status is not IdentityStatus.CONFIRMED
            or self.visibility is Visibility.LOST
        ):
            raise ValueError("degraded status requires non-lost confirmed coordinates.")

    @property
    def plan_suppressed(self) -> bool:
        return self.status is not LocalizationStatus.LOCATED

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    def to_player_state(self) -> PlayerState | None:
        """Project a confirmed anonymous location into the existing player contract."""

        if self.plan_suppressed or self.subject_id is None:
            return None
        position: PixelCoordinate | None
        if self.anchor_working is None:
            position = None
        else:
            position = PixelCoordinate(
                min(self.working_size.width - 1, max(0, round(self.anchor_working.x))),
                min(self.working_size.height - 1, max(0, round(self.anchor_working.y))),
            )
        return PlayerState(
            session_id=self.session_id,
            player_id=self.subject_id,
            source_frame_id=self.source_frame_id,
            visibility=self.visibility,
            confidence=self.confidence,
            generation=self.generation,
            freshness_ns=self.freshness_ns,
            position=position,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source_id": self.source_id,
            "source_frame_id": self.source_frame_id,
            "observed_at_ns": self.observed_at_ns,
            "as_of_ns": self.as_of_ns,
            "generation": self.generation,
            "subject_id": self.subject_id,
            "identity_status": self.identity_status.value,
            "visibility": self.visibility.value,
            "anchor_working": (
                None if self.anchor_working is None else self.anchor_working.to_dict()
            ),
            "world_position": (
                None if self.world_position is None else self.world_position.to_dict()
            ),
            "map_id": self.map_id,
            "profile_id": self.profile_id,
            "platform_id": self.platform_id,
            "confidence": self.confidence,
            "transform_version": self.transform_version,
            "transform_digest": self.transform_digest,
            "platform_graph_digest": self.platform_graph_digest,
            "calibration_sha256": self.calibration_sha256,
            "working_size": self.working_size.to_dict(),
            "pixel_digest": self.pixel_digest,
            "observation_digest": self.observation_digest,
            "evidence_digest": self.evidence_digest,
            "freshness_ns": self.freshness_ns,
            "status": self.status.value,
            "plan_suppressed": self.plan_suppressed,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "PlayerLocation payload")
        try:
            anchor = data.get("anchor_working")
            world = data.get("world_position")
            result = cls(
                session_id=data["session_id"],
                source_id=data["source_id"],
                source_frame_id=data["source_frame_id"],
                observed_at_ns=data["observed_at_ns"],
                as_of_ns=data["as_of_ns"],
                generation=data["generation"],
                subject_id=data.get("subject_id"),
                identity_status=IdentityStatus(data["identity_status"]),
                visibility=Visibility(data["visibility"]),
                anchor_working=None if anchor is None else WorkingPoint.from_dict(anchor),
                world_position=None if world is None else WorldCoordinate.from_dict(world),
                map_id=data["map_id"],
                profile_id=data["profile_id"],
                platform_id=data.get("platform_id"),
                confidence=data["confidence"],
                transform_version=data["transform_version"],
                transform_digest=data["transform_digest"],
                platform_graph_digest=data["platform_graph_digest"],
                calibration_sha256=data["calibration_sha256"],
                working_size=FrameSize.from_dict(data["working_size"]),
                pixel_digest=data["pixel_digest"],
                observation_digest=data["observation_digest"],
                evidence_digest=data.get("evidence_digest"),
                freshness_ns=data["freshness_ns"],
                status=LocalizationStatus(data["status"]),
            )
        except KeyError as exc:
            raise ValueError(f"PlayerLocation payload missing key: {exc.args[0]}") from exc
        if "plan_suppressed" in data and data["plan_suppressed"] is not result.plan_suppressed:
            raise ValueError("PlayerLocation plan_suppressed contradicts status.")
        return result


class LocalizationFaultCode(str, Enum):
    OBSERVATION_FAULT = "observation_fault"
    LINEAGE_MISMATCH = "lineage_mismatch"
    STALE = "stale"
    OUT_OF_ORDER = "out_of_order"
    GENERATION_DRIFT = "generation_drift"
    IDENTITY_SWITCH = "identity_switch"
    TRANSFORM_MISMATCH = "transform_mismatch"
    PLATFORM_GRAPH_MISMATCH = "platform_graph_mismatch"


@dataclass(frozen=True, slots=True)
class LocalizationFault:
    session_id: str
    source_id: str
    source_frame_id: int
    failed_at_ns: int
    code: LocalizationFaultCode
    message: str
    observation_digest: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.source_id, "source_id")
        ensure_non_negative_int(self.source_frame_id, "source_frame_id")
        ensure_time_ns(self.failed_at_ns, "failed_at_ns")
        if not isinstance(self.code, LocalizationFaultCode):
            raise TypeError("code must be LocalizationFaultCode.")
        ensure_non_empty_str(self.message, "message")
        object.__setattr__(
            self,
            "observation_digest",
            _normalise_sha256(self.observation_digest, "observation_digest"),
        )
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
            "session_id": self.session_id,
            "source_id": self.source_id,
            "source_frame_id": self.source_frame_id,
            "failed_at_ns": self.failed_at_ns,
            "code": self.code.value,
            "message": self.message,
            "observation_digest": self.observation_digest,
            "details": to_json_dict(self.details),
            "plan_suppressed": True,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "LocalizationFault payload")
        try:
            result = cls(
                session_id=data["session_id"],
                source_id=data["source_id"],
                source_frame_id=data["source_frame_id"],
                failed_at_ns=data["failed_at_ns"],
                code=LocalizationFaultCode(data["code"]),
                message=data["message"],
                observation_digest=data["observation_digest"],
                details=ensure_mapping(data.get("details", {}), "details"),
            )
        except KeyError as exc:
            raise ValueError(f"LocalizationFault payload missing key: {exc.args[0]}") from exc
        if "plan_suppressed" in data and data["plan_suppressed"] is not True:
            raise ValueError("LocalizationFault must suppress planning.")
        return result


@dataclass(frozen=True, slots=True)
class LocalizationResult:
    location: PlayerLocation | None = None
    fault: LocalizationFault | None = None

    def __post_init__(self) -> None:
        if (self.location is None) == (self.fault is None):
            raise ValueError("LocalizationResult requires exactly one branch.")
        if self.location is not None and not isinstance(self.location, PlayerLocation):
            raise TypeError("location must be PlayerLocation or None.")
        if self.fault is not None and not isinstance(self.fault, LocalizationFault):
            raise TypeError("fault must be LocalizationFault or None.")

    @property
    def succeeded(self) -> bool:
        return self.location is not None and self.location.status is LocalizationStatus.LOCATED

    @property
    def plan_suppressed(self) -> bool:
        return self.fault is not None or (
            self.location is not None and self.location.plan_suppressed
        )

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "location" if self.location is not None else "fault",
            "plan_suppressed": self.plan_suppressed,
            "location": None if self.location is None else self.location.to_dict(),
            "fault": None if self.fault is None else self.fault.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "LocalizationResult payload")
        raw_location = data.get("location")
        raw_fault = data.get("fault")
        result = cls(
            location=(None if raw_location is None else PlayerLocation.from_dict(raw_location)),
            fault=None if raw_fault is None else LocalizationFault.from_dict(raw_fault),
        )
        expected_status = "location" if result.location is not None else "fault"
        if "status" in data and data["status"] != expected_status:
            raise ValueError("LocalizationResult status contradicts its branch.")
        if "plan_suppressed" in data and data["plan_suppressed"] is not result.plan_suppressed:
            raise ValueError("LocalizationResult plan_suppressed contradicts its branch.")
        return result


@dataclass(frozen=True, slots=True)
class LocalizationPolicy:
    subject_id: str
    minimum_confidence: float
    maximum_freshness_ns: int

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.subject_id, "subject_id")
        ensure_probability(self.minimum_confidence, "minimum_confidence")
        object.__setattr__(self, "minimum_confidence", float(self.minimum_confidence))
        ensure_non_negative_int(self.maximum_freshness_ns, "maximum_freshness_ns")

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "minimum_confidence": self.minimum_confidence,
            "maximum_freshness_ns": self.maximum_freshness_ns,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "LocalizationPolicy payload")
        try:
            return cls(
                subject_id=data["subject_id"],
                minimum_confidence=data["minimum_confidence"],
                maximum_freshness_ns=data["maximum_freshness_ns"],
            )
        except KeyError as exc:
            raise ValueError(f"LocalizationPolicy payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class LocationState:
    session_id: str
    source_id: str
    last_frame_id: int
    last_observed_at_ns: int
    last_as_of_ns: int
    last_checked_as_of_ns: int
    generation: int
    subject_id: str | None
    last_identity_status: IdentityStatus
    last_location: PlayerLocation | None
    transform_digest: str
    transform_version: str
    platform_graph_digest: str
    platform_graph_version: str
    identity_switch_count: int = 0

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.source_id, "source_id")
        ensure_non_negative_int(self.last_frame_id, "last_frame_id")
        ensure_time_ns(self.last_observed_at_ns, "last_observed_at_ns")
        ensure_time_ns(self.last_as_of_ns, "last_as_of_ns")
        ensure_time_ns(self.last_checked_as_of_ns, "last_checked_as_of_ns")
        if self.last_as_of_ns < self.last_observed_at_ns:
            raise ValueError("last_as_of_ns must not precede last_observed_at_ns.")
        if self.last_checked_as_of_ns < self.last_as_of_ns:
            raise ValueError("last_checked_as_of_ns must not precede last_as_of_ns.")
        ensure_non_negative_int(self.generation, "generation")
        if self.subject_id is not None:
            ensure_non_empty_str(self.subject_id, "subject_id")
        if not isinstance(self.last_identity_status, IdentityStatus):
            raise TypeError("last_identity_status must be IdentityStatus.")
        object.__setattr__(
            self,
            "transform_digest",
            _normalise_sha256(self.transform_digest, "transform_digest"),
        )
        ensure_non_empty_str(self.transform_version, "transform_version")
        object.__setattr__(
            self,
            "platform_graph_digest",
            _normalise_sha256(self.platform_graph_digest, "platform_graph_digest"),
        )
        ensure_non_empty_str(self.platform_graph_version, "platform_graph_version")
        if self.last_location is not None:
            if not isinstance(self.last_location, PlayerLocation):
                raise TypeError("last_location must be PlayerLocation or None.")
            if self.last_location.session_id != self.session_id:
                raise ValueError("last_location session mismatch.")
            if self.last_location.source_id != self.source_id:
                raise ValueError("last_location source mismatch.")
            if self.last_location.source_frame_id != self.last_frame_id:
                raise ValueError("last_location frame mismatch.")
            if self.last_location.observed_at_ns != self.last_observed_at_ns:
                raise ValueError("last_location observed_at_ns mismatch.")
            if self.last_location.as_of_ns != self.last_as_of_ns:
                raise ValueError("last_location as_of_ns mismatch.")
            if self.last_location.subject_id != self.subject_id:
                raise ValueError("last_location subject_id mismatch.")
            if self.last_location.identity_status is not self.last_identity_status:
                raise ValueError("last_location identity_status mismatch.")
            if self.last_location.generation != self.generation:
                raise ValueError("last_location generation mismatch.")
            if self.last_location.transform_digest != self.transform_digest:
                raise ValueError("last_location transform_digest mismatch.")
            if self.last_location.transform_version != self.transform_version:
                raise ValueError("last_location transform_version mismatch.")
            if self.last_location.platform_graph_digest != self.platform_graph_digest:
                raise ValueError("last_location platform_graph_digest mismatch.")
        else:
            if self.subject_id is not None:
                raise ValueError("state without last_location must not bind subject_id.")
            if self.last_identity_status is not IdentityStatus.UNKNOWN:
                raise ValueError("state without last_location must have unknown identity.")
        ensure_non_negative_int(self.identity_switch_count, "identity_switch_count")

    @property
    def digest(self) -> str:
        return hash_payload(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source_id": self.source_id,
            "last_frame_id": self.last_frame_id,
            "last_observed_at_ns": self.last_observed_at_ns,
            "last_as_of_ns": self.last_as_of_ns,
            "last_checked_as_of_ns": self.last_checked_as_of_ns,
            "generation": self.generation,
            "subject_id": self.subject_id,
            "last_identity_status": self.last_identity_status.value,
            "last_location": (None if self.last_location is None else self.last_location.to_dict()),
            "transform_digest": self.transform_digest,
            "transform_version": self.transform_version,
            "platform_graph_digest": self.platform_graph_digest,
            "platform_graph_version": self.platform_graph_version,
            "identity_switch_count": self.identity_switch_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        data = ensure_mapping(value, "LocationState payload")
        try:
            raw_location = data.get("last_location")
            return cls(
                session_id=data["session_id"],
                source_id=data["source_id"],
                last_frame_id=data["last_frame_id"],
                last_observed_at_ns=data["last_observed_at_ns"],
                last_as_of_ns=data["last_as_of_ns"],
                last_checked_as_of_ns=data["last_checked_as_of_ns"],
                generation=data["generation"],
                subject_id=data.get("subject_id"),
                last_identity_status=IdentityStatus(data["last_identity_status"]),
                last_location=(
                    None if raw_location is None else PlayerLocation.from_dict(raw_location)
                ),
                transform_digest=data["transform_digest"],
                transform_version=data["transform_version"],
                platform_graph_digest=data["platform_graph_digest"],
                platform_graph_version=data["platform_graph_version"],
                identity_switch_count=data.get("identity_switch_count", 0),
            )
        except KeyError as exc:
            raise ValueError(f"LocationState payload missing key: {exc.args[0]}") from exc


def _observation_branch(result: ObservationResult) -> Observation | None:
    if not isinstance(result, ObservationResult):
        raise TypeError("observation must be ObservationResult.")
    return result.observation


def _fault(
    observation: ObservationResult,
    *,
    as_of_ns: int,
    code: LocalizationFaultCode,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> LocalizationResult:
    branch = observation.observation if observation.observation is not None else observation.fault
    assert branch is not None
    return LocalizationResult(
        fault=LocalizationFault(
            session_id=branch.session_id,
            source_id=branch.source_id,
            source_frame_id=branch.frame_id,
            failed_at_ns=as_of_ns,
            code=code,
            message=message,
            observation_digest=observation.digest,
            details={} if details is None else details,
        )
    )


def _unknown_location(
    observation: Observation,
    *,
    as_of_ns: int,
    generation: int,
    transform: LocalizationTransform,
    platform_graph: PlatformGraph,
    freshness_ns: int,
    identity_status: IdentityStatus,
    subject_id: str | None,
    evidence_digest: str | None,
    confidence: float,
    visibility: Visibility = Visibility.LOST,
) -> PlayerLocation:
    return PlayerLocation(
        session_id=observation.session_id,
        source_id=observation.source_id,
        source_frame_id=observation.frame_id,
        observed_at_ns=observation.observed_at_ns,
        as_of_ns=as_of_ns,
        generation=generation,
        subject_id=subject_id,
        identity_status=identity_status,
        visibility=visibility,
        anchor_working=None,
        world_position=None,
        map_id=transform.map_id,
        profile_id=transform.profile_id,
        platform_id=None,
        confidence=confidence,
        transform_version=transform.transform_version,
        transform_digest=transform.digest,
        platform_graph_digest=platform_graph.digest,
        calibration_sha256=observation.calibration_sha256,
        working_size=observation.working_size,
        pixel_digest=observation.pixel_digest,
        observation_digest=observation.digest,
        evidence_digest=evidence_digest,
        freshness_ns=freshness_ns,
        status=LocalizationStatus.UNKNOWN,
    )


def _next_state(
    location: PlayerLocation,
    previous: LocationState | None,
    *,
    platform_graph_version: str,
    identity_switch_count: int | None = None,
) -> LocationState:
    return LocationState(
        session_id=location.session_id,
        source_id=location.source_id,
        last_frame_id=location.source_frame_id,
        last_observed_at_ns=location.observed_at_ns,
        last_as_of_ns=location.as_of_ns,
        last_checked_as_of_ns=location.as_of_ns,
        generation=location.generation,
        subject_id=location.subject_id,
        last_identity_status=location.identity_status,
        last_location=location,
        transform_digest=location.transform_digest,
        transform_version=location.transform_version,
        platform_graph_digest=location.platform_graph_digest,
        platform_graph_version=platform_graph_version,
        identity_switch_count=(
            (0 if previous is None else previous.identity_switch_count)
            if identity_switch_count is None
            else identity_switch_count
        ),
    )


def _checked_state(
    previous: LocationState | None,
    as_of_ns: int,
) -> LocationState | None:
    """Advance the monotonic clock fence even when localization fails."""

    if previous is None or as_of_ns <= previous.last_checked_as_of_ns:
        return previous
    return replace(previous, last_checked_as_of_ns=as_of_ns)


def resolve_player_location(
    *,
    observation: ObservationResult,
    candidates: Sequence[PlayerCandidate],
    transform: LocalizationTransform,
    platform_graph: PlatformGraph,
    policy: LocalizationPolicy,
    previous: LocationState | None,
    as_of_ns: int,
) -> tuple[LocalizationResult, LocationState | None]:
    """Resolve one frame without side effects and return the next immutable state."""

    ensure_time_ns(as_of_ns, "as_of_ns")
    if not isinstance(transform, LocalizationTransform):
        raise TypeError("transform must be LocalizationTransform.")
    if not isinstance(platform_graph, PlatformGraph):
        raise TypeError("platform_graph must be PlatformGraph.")
    if not isinstance(policy, LocalizationPolicy):
        raise TypeError("policy must be LocalizationPolicy.")
    if previous is not None and not isinstance(previous, LocationState):
        raise TypeError("previous must be LocationState or None.")
    if not isinstance(candidates, Sequence) or isinstance(candidates, str | bytes):
        raise TypeError("candidates must be a sequence.")
    frozen_candidates = tuple(candidates)
    if any(not isinstance(candidate, PlayerCandidate) for candidate in frozen_candidates):
        raise TypeError("candidates must contain PlayerCandidate values.")

    current = _observation_branch(observation)
    if previous is not None and as_of_ns <= previous.last_checked_as_of_ns:
        return (
            _fault(
                observation,
                as_of_ns=as_of_ns,
                code=LocalizationFaultCode.OUT_OF_ORDER,
                message="as_of_ns is not strictly monotonic",
            ),
            previous,
        )
    checked_previous = _checked_state(previous, as_of_ns)
    branch = current if current is not None else observation.fault
    assert branch is not None
    if previous is not None and (
        branch.session_id != previous.session_id or branch.source_id != previous.source_id
    ):
        return (
            _fault(
                observation,
                as_of_ns=as_of_ns,
                code=LocalizationFaultCode.LINEAGE_MISMATCH,
                message="session or source change requires an explicit localization reset",
            ),
            checked_previous,
        )
    if current is None:
        assert observation.fault is not None
        return (
            _fault(
                observation,
                as_of_ns=as_of_ns,
                code=LocalizationFaultCode.OBSERVATION_FAULT,
                message="upstream observation failed",
                details={"observation_fault_code": observation.fault.code.value},
            ),
            checked_previous,
        )

    if as_of_ns < current.observed_at_ns:
        return (
            _fault(
                observation,
                as_of_ns=as_of_ns,
                code=LocalizationFaultCode.LINEAGE_MISMATCH,
                message="as_of_ns precedes observation",
            ),
            checked_previous,
        )
    freshness_ns = as_of_ns - current.observed_at_ns
    if freshness_ns > policy.maximum_freshness_ns:
        return (
            _fault(
                observation,
                as_of_ns=as_of_ns,
                code=LocalizationFaultCode.STALE,
                message="observation exceeded localization freshness budget",
                details={"freshness_ns": freshness_ns},
            ),
            checked_previous,
        )
    if transform.map_id != platform_graph.map_id:
        return (
            _fault(
                observation,
                as_of_ns=as_of_ns,
                code=LocalizationFaultCode.PLATFORM_GRAPH_MISMATCH,
                message="transform and platform graph map ids differ",
            ),
            checked_previous,
        )
    if transform.map_fingerprint_sha256 != platform_graph.map_fingerprint_sha256:
        return (
            _fault(
                observation,
                as_of_ns=as_of_ns,
                code=LocalizationFaultCode.PLATFORM_GRAPH_MISMATCH,
                message="transform and platform graph map fingerprints differ",
            ),
            checked_previous,
        )
    try:
        transform.validate_context(current.calibration_sha256, current.working_size)
    except (TypeError, ValueError) as exc:
        return (
            _fault(
                observation,
                as_of_ns=as_of_ns,
                code=LocalizationFaultCode.TRANSFORM_MISMATCH,
                message="observation does not match localization transform",
                details={"reason": str(exc)},
            ),
            checked_previous,
        )

    expected_generation = 0 if previous is None else previous.generation + 1
    if previous is not None:
        assert checked_previous is not None
        if (
            current.frame_id <= previous.last_frame_id
            or current.observed_at_ns <= previous.last_observed_at_ns
        ):
            return (
                _fault(
                    observation,
                    as_of_ns=as_of_ns,
                    code=LocalizationFaultCode.OUT_OF_ORDER,
                    message="frame or observation time is not strictly monotonic",
                ),
                checked_previous,
            )
        if (
            transform.digest != previous.transform_digest
            or transform.transform_version != previous.transform_version
        ):
            return (
                _fault(
                    observation,
                    as_of_ns=as_of_ns,
                    code=LocalizationFaultCode.TRANSFORM_MISMATCH,
                    message="localization transform changed within a session",
                ),
                checked_previous,
            )
        if (
            platform_graph.digest != previous.platform_graph_digest
            or platform_graph.graph_version != previous.platform_graph_version
        ):
            return (
                _fault(
                    observation,
                    as_of_ns=as_of_ns,
                    code=LocalizationFaultCode.PLATFORM_GRAPH_MISMATCH,
                    message="platform graph changed within a session",
                ),
                checked_previous,
            )
        if previous.subject_id is not None and previous.subject_id != policy.subject_id:
            switch_count = previous.identity_switch_count + 1
            return (
                _fault(
                    observation,
                    as_of_ns=as_of_ns,
                    code=LocalizationFaultCode.IDENTITY_SWITCH,
                    message="localization policy subject differs from prior state",
                    details={"identity_switch_count": switch_count},
                ),
                replace(checked_previous, identity_switch_count=switch_count),
            )

    for candidate in frozen_candidates:
        if (
            candidate.session_id != current.session_id
            or candidate.source_id != current.source_id
            or candidate.source_frame_id != current.frame_id
            or candidate.observed_at_ns != current.observed_at_ns
            or candidate.pixel_digest != current.pixel_digest
            or candidate.calibration_sha256 != current.calibration_sha256
            or candidate.working_size != current.working_size
        ):
            return (
                _fault(
                    observation,
                    as_of_ns=as_of_ns,
                    code=LocalizationFaultCode.LINEAGE_MISMATCH,
                    message="player candidate does not match observation lineage",
                ),
                checked_previous,
            )
        if candidate.generation != expected_generation:
            return (
                _fault(
                    observation,
                    as_of_ns=as_of_ns,
                    code=LocalizationFaultCode.GENERATION_DRIFT,
                    message="player candidate generation is not canonical",
                    details={"expected": expected_generation, "actual": candidate.generation},
                ),
                checked_previous,
            )

    unexpected_subjects = sorted(
        {
            candidate.subject_id
            for candidate in frozen_candidates
            if candidate.subject_id != policy.subject_id
        }
    )
    if unexpected_subjects:
        switch_count = (0 if previous is None else previous.identity_switch_count) + 1
        result = _fault(
            observation,
            as_of_ns=as_of_ns,
            code=LocalizationFaultCode.IDENTITY_SWITCH,
            message="candidate subject differs from the frozen anonymous subject",
            details={"identity_switch_count": switch_count},
        )
        if previous is None:
            switched_state = LocationState(
                session_id=current.session_id,
                source_id=current.source_id,
                last_frame_id=current.frame_id,
                last_observed_at_ns=current.observed_at_ns,
                last_as_of_ns=as_of_ns,
                last_checked_as_of_ns=as_of_ns,
                generation=expected_generation,
                subject_id=None,
                last_identity_status=IdentityStatus.UNKNOWN,
                last_location=None,
                transform_digest=transform.digest,
                transform_version=transform.transform_version,
                platform_graph_digest=platform_graph.digest,
                platform_graph_version=platform_graph.graph_version,
                identity_switch_count=switch_count,
            )
        else:
            assert checked_previous is not None
            switched_state = replace(
                checked_previous,
                identity_switch_count=switch_count,
            )
        return result, switched_state

    viable = tuple(
        sorted(
            (
                candidate
                for candidate in frozen_candidates
                if candidate.confidence >= policy.minimum_confidence
                and candidate.visibility is not Visibility.LOST
            ),
            key=lambda candidate: candidate.sort_key,
        )
    )
    if not viable:
        evidence = None if not frozen_candidates else frozen_candidates[0].evidence_digest
        confidence = 0.0 if not frozen_candidates else max(c.confidence for c in frozen_candidates)
        location = _unknown_location(
            current,
            generation=expected_generation,
            as_of_ns=as_of_ns,
            transform=transform,
            platform_graph=platform_graph,
            freshness_ns=freshness_ns,
            identity_status=(
                IdentityStatus.UNKNOWN if not frozen_candidates else IdentityStatus.CONFIRMED
            ),
            subject_id=None if not frozen_candidates else policy.subject_id,
            evidence_digest=evidence,
            confidence=confidence,
        )
        return LocalizationResult(location=location), _next_state(
            location,
            checked_previous,
            platform_graph_version=platform_graph.graph_version,
        )

    if len(viable) != 1:
        location = _unknown_location(
            current,
            generation=expected_generation,
            as_of_ns=as_of_ns,
            transform=transform,
            platform_graph=platform_graph,
            freshness_ns=freshness_ns,
            identity_status=IdentityStatus.AMBIGUOUS,
            subject_id=None,
            evidence_digest=hash_payload([candidate.to_dict() for candidate in viable]),
            confidence=max(candidate.confidence for candidate in viable),
        )
        return LocalizationResult(location=location), _next_state(
            location,
            previous,
            platform_graph_version=platform_graph.graph_version,
        )

    candidate = viable[0]
    assert candidate.anchor_working is not None
    try:
        world = transform.apply(
            (candidate.anchor_working.x, candidate.anchor_working.y),
            calibration_sha256=current.calibration_sha256,
            working_size=current.working_size,
        )
    except (TypeError, ValueError) as exc:
        return (
            _fault(
                observation,
                as_of_ns=as_of_ns,
                code=LocalizationFaultCode.TRANSFORM_MISMATCH,
                message="player anchor could not be transformed",
                details={"reason": str(exc)},
            ),
            checked_previous,
        )
    platform = platform_graph.resolve(world)
    platform_id = platform.platform_id if platform.status is PlatformMatchStatus.CONFIRMED else None
    status = (
        LocalizationStatus.LOCATED
        if platform_id is not None and candidate.visibility is Visibility.VISIBLE
        else LocalizationStatus.DEGRADED
    )
    location = PlayerLocation(
        session_id=current.session_id,
        source_id=current.source_id,
        source_frame_id=current.frame_id,
        observed_at_ns=current.observed_at_ns,
        as_of_ns=as_of_ns,
        generation=expected_generation,
        subject_id=policy.subject_id,
        identity_status=IdentityStatus.CONFIRMED,
        visibility=candidate.visibility,
        anchor_working=candidate.anchor_working,
        world_position=world,
        map_id=transform.map_id,
        profile_id=transform.profile_id,
        platform_id=platform_id,
        confidence=candidate.confidence,
        transform_version=transform.transform_version,
        transform_digest=transform.digest,
        platform_graph_digest=platform_graph.digest,
        calibration_sha256=current.calibration_sha256,
        working_size=current.working_size,
        pixel_digest=current.pixel_digest,
        observation_digest=current.digest,
        evidence_digest=candidate.evidence_digest,
        freshness_ns=freshness_ns,
        status=status,
    )
    return LocalizationResult(location=location), _next_state(
        location,
        previous,
        platform_graph_version=platform_graph.graph_version,
    )


__all__ = [
    "IdentityStatus",
    "LocalizationFault",
    "LocalizationFaultCode",
    "LocalizationPolicy",
    "LocalizationResult",
    "LocalizationStatus",
    "LocationState",
    "PlayerAnchorSource",
    "PlayerCandidate",
    "PlayerLocation",
    "WorkingPoint",
    "resolve_player_location",
]
