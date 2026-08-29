"""Deterministic capture admission and latest-frame contracts.

The capture boundary deliberately contains no image-library or input-device
dependency.  A source supplies immutable :class:`RawFrame` values, and the
adapter turns only admitted values into the existing domain
:class:`~maple_automation_core.domain.FramePacket` envelope.

DEC-001 is represented as a source crop followed by resize into
``SourceGeometry.working_size``.  Letterboxing/padding is intentionally outside
this contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from threading import Lock, RLock
from time import monotonic_ns
from typing import Any, Protocol, cast, runtime_checkable

from maple_automation_core.domain._contract_utils import (
    canonical_json_bytes,
    ensure_json_value,
    ensure_mapping,
    ensure_non_empty_str,
    ensure_non_negative_int,
    ensure_sha256_hex,
    ensure_time_ns,
    freeze_json_value,
    to_json_dict,
)
from maple_automation_core.domain.frame import (
    CaptureHealth,
    FramePacket,
    FrameSize,
    SourceGeometry,
)


def canonical_geometry_sha256(geometry: SourceGeometry) -> str:
    """Return the canonical SHA-256 identity of a crop-and-resize geometry."""

    if not isinstance(geometry, SourceGeometry):
        raise TypeError("geometry must be SourceGeometry.")
    return sha256(canonical_json_bytes(geometry.to_dict())).hexdigest()


def canonical_calibration_sha256(
    geometry: SourceGeometry,
    transform_version: str = "",
) -> str:
    """Return the canonical SHA-256 identity of geometry plus transform version."""

    if not isinstance(geometry, SourceGeometry):
        raise TypeError("geometry must be SourceGeometry.")
    if not isinstance(transform_version, str):
        raise TypeError("transform_version must be str.")
    if transform_version:
        ensure_non_empty_str(transform_version, "transform_version")
    payload = {
        "geometry": geometry.to_dict(),
        "transform_version": transform_version,
    }
    return sha256(canonical_json_bytes(payload)).hexdigest()


# Short names are useful at call sites while the canonical names make the
# serialization identity explicit.
geometry_sha256 = canonical_geometry_sha256
calibration_sha256 = canonical_calibration_sha256
canonical_geometry_hash = canonical_geometry_sha256
canonical_calibration_hash = canonical_calibration_sha256


@dataclass(frozen=True, slots=True)
class RawFrame:
    """Immutable frame value emitted by a capture source.

    ``source_id``, ``session_id``, ``clock_domain`` and ``transform_version``
    may be omitted by a source that is bound by :class:`FrameSourceConfig`.
    Explicit values are still checked by the adapter, which makes mismatched
    source metadata observable instead of silently rewriting it.

    ``source_size`` is the actual source image size.  A source may provide a
    full ``source_geometry`` as well; when omitted, the adapter applies its
    configured geometry after checking ``source_size``.
    """

    source_id: str | None = None
    session_id: str | None = None
    frame_id: int = 0
    captured_at_ns: int = 0
    clock_domain: str | None = None
    transform_version: str | None = None
    source_geometry: SourceGeometry | None = None
    content_hash: str = ""
    image_ref: str = ""
    source_size: FrameSize | None = None
    image_metadata: Mapping[str, Any] = field(default_factory=dict)
    received_at_ns: int | None = None
    # Constructor aliases keep source integrations readable while the
    # serialized contract remains source_geometry/source_size.
    geometry: SourceGeometry | None = None
    frame_size: FrameSize | None = None

    def __post_init__(self) -> None:
        if self.source_id is not None:
            ensure_non_empty_str(self.source_id, "source_id")
        if self.session_id is not None:
            ensure_non_empty_str(self.session_id, "session_id")
        if self.clock_domain is not None:
            ensure_non_empty_str(self.clock_domain, "clock_domain")
        if self.transform_version is not None:
            ensure_non_empty_str(self.transform_version, "transform_version")
        ensure_non_negative_int(self.frame_id, "frame_id")
        ensure_time_ns(self.captured_at_ns, "captured_at_ns")
        if self.received_at_ns is not None:
            ensure_time_ns(self.received_at_ns, "received_at_ns")
            if self.received_at_ns < self.captured_at_ns:
                raise ValueError("received_at_ns must be >= captured_at_ns.")
        ensure_sha256_hex(self.content_hash, "content_hash")
        ensure_non_empty_str(self.image_ref, "image_ref")

        geometry = self.source_geometry
        if geometry is None:
            geometry = self.geometry
        elif self.geometry is not None and self.geometry != geometry:
            raise ValueError("geometry and source_geometry must match.")
        if geometry is not None and not isinstance(geometry, SourceGeometry):
            raise TypeError("source_geometry must be SourceGeometry.")
        if geometry is not None:
            object.__setattr__(self, "source_geometry", geometry)
            object.__setattr__(self, "geometry", geometry)

        source_size = self.source_size
        if source_size is None:
            source_size = self.frame_size
        elif self.frame_size is not None and self.frame_size != source_size:
            raise ValueError("frame_size and source_size must match.")
        if source_size is not None and not isinstance(source_size, FrameSize):
            raise TypeError("source_size must be FrameSize.")
        if geometry is not None:
            geometry_size = geometry.source_size
            if source_size is None:
                source_size = geometry_size
            elif source_size != geometry_size:
                raise ValueError("source_size must match source_geometry.source_size.")
        if source_size is None:
            raise ValueError("source_size or source_geometry is required.")
        object.__setattr__(self, "source_size", source_size)
        object.__setattr__(self, "frame_size", source_size)

        metadata = ensure_mapping(self.image_metadata, "image_metadata")
        ensure_json_value(metadata, "image_metadata")
        object.__setattr__(self, "image_metadata", freeze_json_value(metadata))

    @property
    def actual_source_size(self) -> FrameSize:
        """The validated source image dimensions."""

        # __post_init__ establishes this invariant; the branch documents the
        # invariant for static type checkers without weakening the public type.
        if self.source_size is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("RawFrame source_size invariant was violated.")
        return self.source_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "captured_at_ns": self.captured_at_ns,
            "received_at_ns": self.received_at_ns,
            "clock_domain": self.clock_domain,
            "transform_version": self.transform_version,
            "source_geometry": (
                None if self.source_geometry is None else self.source_geometry.to_dict()
            ),
            "content_hash": self.content_hash,
            "image_ref": self.image_ref,
            "source_size": self.actual_source_size.to_dict(),
            "image_metadata": to_json_dict(self.image_metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RawFrame:
        values = ensure_mapping(data, "RawFrame payload")
        try:
            geometry_value = values.get("source_geometry", values.get("geometry"))
            source_size_value = values.get("source_size", values.get("frame_size"))
            geometry = (
                None
                if geometry_value is None
                else SourceGeometry.from_dict(ensure_mapping(geometry_value, "source_geometry"))
            )
            source_size = (
                None
                if source_size_value is None
                else FrameSize.from_dict(ensure_mapping(source_size_value, "source_size"))
            )
            metadata = ensure_mapping(values.get("image_metadata", {}), "image_metadata")
            return cls(
                source_id=values.get("source_id"),
                session_id=values.get("session_id"),
                frame_id=values["frame_id"],
                captured_at_ns=values["captured_at_ns"],
                received_at_ns=values.get("received_at_ns"),
                clock_domain=values.get("clock_domain"),
                transform_version=values.get("transform_version"),
                source_geometry=geometry,
                content_hash=values["content_hash"],
                image_ref=values["image_ref"],
                source_size=source_size,
                image_metadata=metadata,
            )
        except KeyError as exc:
            raise ValueError(f"RawFrame payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class FrameSourceConfig:
    """Fixed identity and calibration contract for one adapter session."""

    session_id: str
    source_id: str
    clock_domain: str
    transform_version: str
    source_geometry: SourceGeometry
    max_age_ns: int

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.source_id, "source_id")
        ensure_non_empty_str(self.clock_domain, "clock_domain")
        ensure_non_empty_str(self.transform_version, "transform_version")
        if not isinstance(self.source_geometry, SourceGeometry):
            raise TypeError("source_geometry must be SourceGeometry.")
        ensure_non_negative_int(self.max_age_ns, "max_age_ns")

    @property
    def geometry(self) -> SourceGeometry:
        return self.source_geometry

    @property
    def geometry_hash(self) -> str:
        return canonical_geometry_sha256(self.source_geometry)

    @property
    def calibration_hash(self) -> str:
        return canonical_calibration_sha256(self.source_geometry, self.transform_version)

    @property
    def geometry_digest(self) -> str:
        return self.geometry_hash

    @property
    def calibration_digest(self) -> str:
        return self.calibration_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source_id": self.source_id,
            "clock_domain": self.clock_domain,
            "transform_version": self.transform_version,
            "source_geometry": self.source_geometry.to_dict(),
            "max_age_ns": self.max_age_ns,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FrameSourceConfig:
        values = ensure_mapping(data, "FrameSourceConfig payload")
        try:
            return cls(
                session_id=values["session_id"],
                source_id=values["source_id"],
                clock_domain=values["clock_domain"],
                transform_version=values["transform_version"],
                source_geometry=SourceGeometry.from_dict(values["source_geometry"]),
                max_age_ns=values["max_age_ns"],
            )
        except KeyError as exc:
            raise ValueError(f"FrameSourceConfig payload missing key: {exc.args[0]}") from exc


@runtime_checkable
class FrameSource(Protocol):
    """Minimal source protocol consumed by :class:`FrameSourceAdapter`."""

    def read(self) -> RawFrame | None:
        """Return one source frame, or ``None`` when no frame is ready."""


FrameReader = FrameSource


class Clock(Protocol):
    """Clock object form accepted by :class:`FrameSourceAdapter`."""

    def now_ns(self) -> int:
        """Return the current monotonic time in nanoseconds."""


class FrameAdmissionStatus(str, Enum):
    ACCEPTED = "accepted"
    NO_FRAME = "no_frame"
    STALE = "stale"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    TIMESTAMP_REGRESSION = "timestamp_regression"
    FRAME_SIZE_CHANGED = "frame_size_changed"
    SOURCE_MISMATCH = "source_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    CLOCK_DOMAIN_MISMATCH = "clock_domain_mismatch"
    SOURCE_ERROR = "source_error"

    # Lower-case aliases make status checks natural for integrations that use
    # the wire values directly, while uppercase names follow domain enums.
    accepted = ACCEPTED
    no_frame = NO_FRAME
    stale = STALE
    duplicate = DUPLICATE
    out_of_order = OUT_OF_ORDER
    timestamp_regression = TIMESTAMP_REGRESSION
    frame_size_changed = FRAME_SIZE_CHANGED
    source_mismatch = SOURCE_MISMATCH
    session_mismatch = SESSION_MISMATCH
    clock_domain_mismatch = CLOCK_DOMAIN_MISMATCH
    source_error = SOURCE_ERROR


_FATAL_STATUSES = frozenset(
    {
        FrameAdmissionStatus.DUPLICATE,
        FrameAdmissionStatus.OUT_OF_ORDER,
        FrameAdmissionStatus.TIMESTAMP_REGRESSION,
        FrameAdmissionStatus.FRAME_SIZE_CHANGED,
        FrameAdmissionStatus.SOURCE_MISMATCH,
        FrameAdmissionStatus.SESSION_MISMATCH,
        FrameAdmissionStatus.CLOCK_DOMAIN_MISMATCH,
        FrameAdmissionStatus.SOURCE_ERROR,
    }
)


@dataclass(frozen=True, slots=True)
class FrameAdmissionEvent:
    """Immutable admission outcome and deterministic fault evidence."""

    status: FrameAdmissionStatus
    observed_at_ns: int
    reason: str
    session_id: str = ""
    source_id: str = ""
    frame_id: int | None = None
    plan_suppressed: bool | None = None
    fault_latched: bool | None = None
    previous_frame_id: int | None = None
    gap_detected: bool = False
    missing_frame_count: int = 0
    superseded_count: int = 0
    expected_source_size: FrameSize | None = None
    actual_source_size: FrameSize | None = None
    geometry_hash: str | None = None
    calibration_hash: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, FrameAdmissionStatus):
            raise TypeError("status must be FrameAdmissionStatus.")
        ensure_time_ns(self.observed_at_ns, "observed_at_ns")
        ensure_non_empty_str(self.reason, "reason")
        if self.session_id:
            ensure_non_empty_str(self.session_id, "session_id")
        if self.source_id:
            ensure_non_empty_str(self.source_id, "source_id")
        if self.frame_id is not None:
            ensure_non_negative_int(self.frame_id, "frame_id")
        if self.previous_frame_id is not None:
            ensure_non_negative_int(self.previous_frame_id, "previous_frame_id")
        plan_suppressed = self.plan_suppressed
        if plan_suppressed is None:
            plan_suppressed = self.status is not FrameAdmissionStatus.ACCEPTED
        elif type(plan_suppressed) is not bool:
            raise ValueError("plan_suppressed must be bool.")
        if plan_suppressed is not (self.status is not FrameAdmissionStatus.ACCEPTED):
            raise ValueError("plan_suppressed must match admission status.")
        object.__setattr__(self, "plan_suppressed", plan_suppressed)

        fault_latched = self.fault_latched
        if fault_latched is None:
            fault_latched = self.status in _FATAL_STATUSES
        elif type(fault_latched) is not bool:
            raise ValueError("fault_latched must be bool.")
        if fault_latched is not (self.status in _FATAL_STATUSES):
            raise ValueError("fault_latched must match fatal admission status.")
        object.__setattr__(self, "fault_latched", fault_latched)

        if type(self.gap_detected) is not bool:
            raise ValueError("gap_detected must be bool.")
        ensure_non_negative_int(self.missing_frame_count, "missing_frame_count")
        ensure_non_negative_int(self.superseded_count, "superseded_count")
        if not self.gap_detected and self.missing_frame_count:
            raise ValueError("missing_frame_count requires gap_detected.")
        for value, field_name in (
            (self.expected_source_size, "expected_source_size"),
            (self.actual_source_size, "actual_source_size"),
        ):
            if value is not None and not isinstance(value, FrameSize):
                raise TypeError(f"{field_name} must be FrameSize.")
        for hash_value, field_name in (
            (self.geometry_hash, "geometry_hash"),
            (self.calibration_hash, "calibration_hash"),
        ):
            if hash_value is not None:
                ensure_sha256_hex(hash_value, field_name)
        details = ensure_mapping(self.details, "details")
        ensure_json_value(details, "details")
        object.__setattr__(self, "details", freeze_json_value(details))

    @property
    def event_type(self) -> str:
        return self.status.value

    @property
    def reason_code(self) -> str:
        return self.status.value

    @property
    def admission_status(self) -> FrameAdmissionStatus:
        return self.status

    @property
    def status_code(self) -> str:
        return self.status.value

    @property
    def is_fatal(self) -> bool:
        return self.status in _FATAL_STATUSES

    @property
    def is_latched(self) -> bool:
        return bool(self.fault_latched)

    @property
    def suppressed(self) -> bool:
        return bool(self.plan_suppressed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "observed_at_ns": self.observed_at_ns,
            "reason": self.reason,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "frame_id": self.frame_id,
            "plan_suppressed": self.plan_suppressed,
            "fault_latched": self.fault_latched,
            "previous_frame_id": self.previous_frame_id,
            "gap_detected": self.gap_detected,
            "missing_frame_count": self.missing_frame_count,
            "superseded_count": self.superseded_count,
            "expected_source_size": (
                None if self.expected_source_size is None else self.expected_source_size.to_dict()
            ),
            "actual_source_size": (
                None if self.actual_source_size is None else self.actual_source_size.to_dict()
            ),
            "geometry_hash": self.geometry_hash,
            "calibration_hash": self.calibration_hash,
            "details": to_json_dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FrameAdmissionEvent:
        values = ensure_mapping(data, "FrameAdmissionEvent payload")
        try:
            expected_value = values.get("expected_source_size")
            actual_value = values.get("actual_source_size")
            details = ensure_mapping(values.get("details", {}), "details")
            return cls(
                status=FrameAdmissionStatus(values["status"]),
                observed_at_ns=values["observed_at_ns"],
                reason=values["reason"],
                session_id=values.get("session_id", ""),
                source_id=values.get("source_id", ""),
                frame_id=values.get("frame_id"),
                plan_suppressed=values.get("plan_suppressed"),
                fault_latched=values.get("fault_latched"),
                previous_frame_id=values.get("previous_frame_id"),
                gap_detected=values.get("gap_detected", False),
                missing_frame_count=values.get("missing_frame_count", 0),
                superseded_count=values.get("superseded_count", 0),
                expected_source_size=(
                    None
                    if expected_value is None
                    else FrameSize.from_dict(ensure_mapping(expected_value, "expected_source_size"))
                ),
                actual_source_size=(
                    None
                    if actual_value is None
                    else FrameSize.from_dict(ensure_mapping(actual_value, "actual_source_size"))
                ),
                geometry_hash=values.get("geometry_hash"),
                calibration_hash=values.get("calibration_hash"),
                details=details,
            )
        except KeyError as exc:
            raise ValueError(f"FrameAdmissionEvent payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class FrameAdmissionResult:
    """Admission event paired with an admitted immutable packet, if any."""

    status: FrameAdmissionStatus
    event: FrameAdmissionEvent
    packet: FramePacket | None = None
    # ``frame`` is a source-integration alias; packet is canonical on the wire.
    frame: FramePacket | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, FrameAdmissionStatus):
            raise TypeError("status must be FrameAdmissionStatus.")
        if not isinstance(self.event, FrameAdmissionEvent):
            raise TypeError("event must be FrameAdmissionEvent.")
        if self.event.status is not self.status:
            raise ValueError("event.status must match result.status.")
        packet = self.packet
        if packet is None:
            packet = self.frame
        elif self.frame is not None and self.frame != packet:
            raise ValueError("frame and packet must match.")
        if self.status is FrameAdmissionStatus.ACCEPTED:
            if packet is None:
                raise ValueError("accepted result requires packet.")
            if self.event.plan_suppressed:
                raise ValueError("accepted result must not suppress plans.")
        elif packet is not None:
            raise ValueError("rejected result must not contain packet.")
        if packet is not None and not isinstance(packet, FramePacket):
            raise TypeError("packet must be FramePacket.")
        object.__setattr__(self, "packet", packet)
        object.__setattr__(self, "frame", packet)

    @property
    def accepted(self) -> bool:
        return self.status is FrameAdmissionStatus.ACCEPTED

    @property
    def admission_status(self) -> FrameAdmissionStatus:
        return self.status

    @property
    def event_type(self) -> str:
        return self.status.value

    @property
    def plan_suppressed(self) -> bool:
        return bool(self.event.plan_suppressed)

    @property
    def fault_latched(self) -> bool:
        return bool(self.event.fault_latched)

    @property
    def gap_detected(self) -> bool:
        return self.event.gap_detected

    @property
    def superseded_count(self) -> int:
        return self.event.superseded_count

    @property
    def reason(self) -> str:
        return self.event.reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "event": self.event.to_dict(),
            "packet": None if self.packet is None else self.packet.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FrameAdmissionResult:
        values = ensure_mapping(data, "FrameAdmissionResult payload")
        try:
            event = FrameAdmissionEvent.from_dict(values["event"])
            packet_value = values.get("packet")
            packet = (
                None
                if packet_value is None
                else FramePacket.from_dict(ensure_mapping(packet_value, "packet"))
            )
            return cls(
                status=FrameAdmissionStatus(values["status"]),
                event=event,
                packet=packet,
            )
        except KeyError as exc:
            raise ValueError(f"FrameAdmissionResult payload missing key: {exc.args[0]}") from exc


class LatestFrameBuffer:
    """Thread-safe single-slot latest-frame buffer.

    ``publish`` replaces the prior packet atomically.  The replacement count
    excludes the first publication and all rejected source values.  A read
    with ``now_ns`` atomically expires a stale packet before returning.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._latest: FramePacket | None = None
        self._superseded_count = 0

    @property
    def superseded_count(self) -> int:
        with self._lock:
            return self._superseded_count

    @property
    def latest(self) -> FramePacket | None:
        return self.read_latest()

    def publish(self, packet: FramePacket) -> int:
        if not isinstance(packet, FramePacket):
            raise TypeError("packet must be FramePacket.")
        with self._lock:
            if self._latest is not None:
                self._superseded_count += 1
            self._latest = packet
            return self._superseded_count

    def read_latest(self, now_ns: int | None = None) -> FramePacket | None:
        if now_ns is not None:
            ensure_time_ns(now_ns, "now_ns")
        with self._lock:
            packet = self._latest
            if packet is None:
                return None
            if now_ns is not None and not packet.is_fresh_at(now_ns):
                self._latest = None
                return None
            return packet

    def clear(self) -> None:
        with self._lock:
            self._latest = None


@dataclass(frozen=True, slots=True)
class _ClockAdapter:
    callback: Callable[[], int]


class FrameSourceAdapter:
    """Admit source frames into immutable packets and a latest-frame buffer."""

    def __init__(
        self,
        source: FrameSource | FrameSourceConfig,
        config: FrameSourceConfig | FrameSource,
        clock: Callable[[], int] | Clock = monotonic_ns,
        latest_buffer: LatestFrameBuffer | None = None,
    ) -> None:
        if isinstance(source, FrameSourceConfig):
            actual_config = source
            actual_source = cast(FrameSource, config)
        else:
            actual_source = source
            actual_config = cast(FrameSourceConfig, config)
        if not isinstance(actual_config, FrameSourceConfig):
            raise TypeError("config must be FrameSourceConfig.")
        if not callable(getattr(actual_source, "read", None)):
            raise TypeError("source must implement read().")
        clock_callback: Callable[[], int]
        if callable(clock):
            clock_callback = clock
        else:
            now_method = getattr(clock, "now_ns", None)
            if not callable(now_method):
                raise TypeError("clock must be callable or implement now_ns().")
            clock_callback = cast(Callable[[], int], now_method)
        self.source = actual_source
        self.config = actual_config
        self._clock = _ClockAdapter(callback=clock_callback)
        self._buffer = LatestFrameBuffer() if latest_buffer is None else latest_buffer
        # One producer read/admit transaction runs at a time.  Session reset
        # uses the same outer lock so an in-flight frame cannot cross a reset.
        self._producer_lock = RLock()
        self._lock = Lock()
        self._session_id = actual_config.session_id
        self._last_accepted_frame_id: int | None = None
        self._last_accepted_captured_at_ns: int | None = None
        self._last_observed_at_ns: int | None = None
        self._last_accepted_packet: FramePacket | None = None
        self._latched_result: FrameAdmissionResult | None = None

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    @property
    def latest_buffer(self) -> LatestFrameBuffer:
        return self._buffer

    @property
    def latest_frame(self) -> FramePacket | None:
        return self.read_latest()

    @property
    def last_accepted_frame_id(self) -> int | None:
        with self._lock:
            return self._last_accepted_frame_id

    @property
    def last_accepted(self) -> FramePacket | None:
        with self._lock:
            return self._last_accepted_packet

    @property
    def superseded_count(self) -> int:
        return self._buffer.superseded_count

    @property
    def geometry_hash(self) -> str:
        return self.config.geometry_hash

    @property
    def calibration_hash(self) -> str:
        return self.config.calibration_hash

    @property
    def fault_latched(self) -> bool:
        with self._lock:
            return self._latched_result is not None

    def _now_ns(self, supplied: int | None) -> int:
        value = self._clock.callback() if supplied is None else supplied
        ensure_time_ns(value, "now_ns")
        return value

    def _event(
        self,
        status: FrameAdmissionStatus,
        now_ns: int,
        reason: str,
        *,
        frame_id: int | None = None,
        previous_frame_id: int | None = None,
        gap_detected: bool = False,
        missing_frame_count: int = 0,
        superseded_count: int = 0,
        expected_source_size: FrameSize | None = None,
        actual_source_size: FrameSize | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> FrameAdmissionEvent:
        return FrameAdmissionEvent(
            status=status,
            observed_at_ns=now_ns,
            reason=reason,
            session_id=self._session_id,
            source_id=self.config.source_id,
            frame_id=frame_id,
            plan_suppressed=status is not FrameAdmissionStatus.ACCEPTED,
            fault_latched=status in _FATAL_STATUSES,
            previous_frame_id=previous_frame_id,
            gap_detected=gap_detected,
            missing_frame_count=missing_frame_count,
            superseded_count=superseded_count,
            expected_source_size=expected_source_size,
            actual_source_size=actual_source_size,
            geometry_hash=self.config.geometry_hash,
            calibration_hash=self.config.calibration_hash,
            details={} if details is None else details,
        )

    def _result(
        self,
        event: FrameAdmissionEvent,
        packet: FramePacket | None = None,
    ) -> FrameAdmissionResult:
        result = FrameAdmissionResult(status=event.status, event=event, packet=packet)
        if event.is_fatal:
            self._latched_result = result
        return result

    def _observe_clock(
        self,
        now_ns: int,
        *,
        frame_id: int | None = None,
    ) -> FrameAdmissionResult | None:
        """Advance the session observation clock or latch a rollback.

        The caller holds ``self._lock``.  All poll/ingest/latest outcomes,
        including no-frame and stale results, participate in this watermark.
        """

        previous = self._last_observed_at_ns
        if previous is not None and now_ns < previous:
            return self._result(
                self._event(
                    FrameAdmissionStatus.TIMESTAMP_REGRESSION,
                    now_ns,
                    "receive clock moved backwards",
                    frame_id=frame_id,
                    previous_frame_id=self._last_accepted_frame_id,
                    details={"previous_observed_at_ns": previous},
                )
            )
        self._last_observed_at_ns = now_ns
        return None

    def _no_frame(self, now_ns: int) -> FrameAdmissionResult:
        return self._result(self._event(FrameAdmissionStatus.NO_FRAME, now_ns, "no frame ready"))

    def _source_error(
        self,
        now_ns: int,
        reason: str,
        *,
        frame_id: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> FrameAdmissionResult:
        event = self._event(
            FrameAdmissionStatus.SOURCE_ERROR,
            now_ns,
            reason,
            frame_id=frame_id,
            details=details,
        )
        return self._result(event)

    def poll(self, now_ns: int | None = None) -> FrameAdmissionResult:
        """Read one source value and return its deterministic admission result."""

        with self._producer_lock:
            with self._lock:
                if self._latched_result is not None:
                    return self._latched_result
            try:
                raw = self.source.read()
            except Exception as exc:
                observed_at_ns = self._now_ns(now_ns)
                with self._lock:
                    if self._latched_result is not None:
                        return self._latched_result
                    clock_fault = self._observe_clock(observed_at_ns)
                    if clock_fault is not None:
                        return clock_fault
                    return self._source_error(
                        observed_at_ns,
                        "frame source read raised an exception",
                        details={"exception_type": type(exc).__name__},
                    )
            observed_at_ns = self._now_ns(now_ns)
            return self.ingest(raw, received_at_ns=observed_at_ns)

    # Common integration spellings retain one implementation and one state
    # machine, so callers cannot accidentally bypass latching semantics.
    read = poll

    def _ingest_transaction(
        self,
        raw: RawFrame | None,
        received_at_ns: int | None = None,
    ) -> FrameAdmissionResult:
        """Validate and admit one raw frame using the injected receive clock."""

        with self._lock:
            if self._latched_result is not None:
                return self._latched_result
        observed_at_ns = self._now_ns(received_at_ns)
        with self._lock:
            if self._latched_result is not None:
                return self._latched_result
            frame_id = raw.frame_id if isinstance(raw, RawFrame) else None
            clock_fault = self._observe_clock(observed_at_ns, frame_id=frame_id)
            if clock_fault is not None:
                return clock_fault
            if raw is None:
                return self._no_frame(observed_at_ns)
            if not isinstance(raw, RawFrame):
                return self._source_error(
                    observed_at_ns,
                    "frame source returned an invalid value",
                    details={"actual_type": type(raw).__name__},
                )

            frame_id = raw.frame_id
            if raw.source_id is not None and raw.source_id != self.config.source_id:
                return self._result(
                    self._event(
                        FrameAdmissionStatus.SOURCE_MISMATCH,
                        observed_at_ns,
                        "source_id does not match configured source",
                        frame_id=frame_id,
                        details={"actual_source_id": raw.source_id},
                    )
                )
            if raw.session_id is not None and raw.session_id != self._session_id:
                return self._result(
                    self._event(
                        FrameAdmissionStatus.SESSION_MISMATCH,
                        observed_at_ns,
                        "session_id does not match active session",
                        frame_id=frame_id,
                        details={"actual_session_id": raw.session_id},
                    )
                )
            if raw.clock_domain is not None and raw.clock_domain != self.config.clock_domain:
                return self._result(
                    self._event(
                        FrameAdmissionStatus.CLOCK_DOMAIN_MISMATCH,
                        observed_at_ns,
                        "clock_domain does not match configured clock domain",
                        frame_id=frame_id,
                        details={"actual_clock_domain": raw.clock_domain},
                    )
                )
            if (
                raw.transform_version is not None
                and raw.transform_version != self.config.transform_version
            ):
                return self._result(
                    self._event(
                        FrameAdmissionStatus.SOURCE_MISMATCH,
                        observed_at_ns,
                        "transform_version does not match configured calibration",
                        frame_id=frame_id,
                        details={"actual_transform_version": raw.transform_version},
                    )
                )

            expected_size = self.config.source_geometry.source_size
            actual_size = raw.actual_source_size
            if actual_size != expected_size:
                return self._result(
                    self._event(
                        FrameAdmissionStatus.FRAME_SIZE_CHANGED,
                        observed_at_ns,
                        "source frame size changed",
                        frame_id=frame_id,
                        expected_source_size=expected_size,
                        actual_source_size=actual_size,
                    )
                )

            geometry = raw.source_geometry
            if geometry is not None and geometry.source_size != expected_size:
                return self._result(
                    self._event(
                        FrameAdmissionStatus.FRAME_SIZE_CHANGED,
                        observed_at_ns,
                        "source geometry frame size changed",
                        frame_id=frame_id,
                        expected_source_size=expected_size,
                        actual_source_size=geometry.source_size,
                    )
                )
            if geometry is not None and geometry != self.config.source_geometry:
                return self._result(
                    self._event(
                        FrameAdmissionStatus.SOURCE_MISMATCH,
                        observed_at_ns,
                        "source geometry does not match configured calibration",
                        frame_id=frame_id,
                        details={
                            "actual_geometry_hash": canonical_geometry_sha256(geometry),
                        },
                    )
                )
            actual_geometry = self.config.source_geometry if geometry is None else geometry

            previous_frame_id = self._last_accepted_frame_id
            if previous_frame_id is not None:
                if frame_id == previous_frame_id:
                    return self._result(
                        self._event(
                            FrameAdmissionStatus.DUPLICATE,
                            observed_at_ns,
                            "frame_id duplicates last accepted frame",
                            frame_id=frame_id,
                            previous_frame_id=previous_frame_id,
                        )
                    )
                if frame_id < previous_frame_id:
                    return self._result(
                        self._event(
                            FrameAdmissionStatus.OUT_OF_ORDER,
                            observed_at_ns,
                            "frame_id moved backwards",
                            frame_id=frame_id,
                            previous_frame_id=previous_frame_id,
                        )
                    )

            if raw.captured_at_ns > observed_at_ns:
                return self._result(
                    self._event(
                        FrameAdmissionStatus.TIMESTAMP_REGRESSION,
                        observed_at_ns,
                        "captured_at_ns is newer than receive clock",
                        frame_id=frame_id,
                        previous_frame_id=previous_frame_id,
                    )
                )
            if (
                self._last_accepted_captured_at_ns is not None
                and raw.captured_at_ns < self._last_accepted_captured_at_ns
            ):
                return self._result(
                    self._event(
                        FrameAdmissionStatus.TIMESTAMP_REGRESSION,
                        observed_at_ns,
                        "captured_at_ns moved backwards",
                        frame_id=frame_id,
                        previous_frame_id=previous_frame_id,
                    )
                )
            age_ns = observed_at_ns - raw.captured_at_ns
            if age_ns > self.config.max_age_ns:
                return self._result(
                    self._event(
                        FrameAdmissionStatus.STALE,
                        observed_at_ns,
                        "frame exceeded configured max age",
                        frame_id=frame_id,
                        previous_frame_id=previous_frame_id,
                        details={
                            "age_ns": age_ns,
                            "max_age_ns": self.config.max_age_ns,
                        },
                    )
                )

            try:
                health = CaptureHealth(
                    session_id=self._session_id,
                    frame_id=frame_id,
                    source_id=self.config.source_id,
                    content_hash=raw.content_hash,
                    clock_domain=self.config.clock_domain,
                    captured_at_ns=raw.captured_at_ns,
                    received_at_ns=observed_at_ns,
                    transform_version=self.config.transform_version,
                    max_age_ns=self.config.max_age_ns,
                )
                packet = FramePacket(
                    source_id=self.config.source_id,
                    session_id=self._session_id,
                    frame_id=frame_id,
                    captured_at_ns=raw.captured_at_ns,
                    received_at_ns=observed_at_ns,
                    transform_version=self.config.transform_version,
                    clock_domain=self.config.clock_domain,
                    content_hash=raw.content_hash,
                    source_geometry=actual_geometry,
                    image_ref=raw.image_ref,
                    capture_health=health,
                    image_metadata=raw.image_metadata,
                )
            except (TypeError, ValueError) as exc:
                return self._source_error(
                    observed_at_ns,
                    "raw frame could not be converted to FramePacket",
                    frame_id=frame_id,
                    details={"error_type": type(exc).__name__},
                )

            if previous_frame_id is None or frame_id <= previous_frame_id + 1:
                gap_detected = False
                missing_count = 0
            else:
                gap_detected = True
                missing_count = frame_id - previous_frame_id - 1
            superseded_count = self._buffer.publish(packet)
            self._last_accepted_frame_id = frame_id
            self._last_accepted_captured_at_ns = raw.captured_at_ns
            self._last_accepted_packet = packet
            event = self._event(
                FrameAdmissionStatus.ACCEPTED,
                observed_at_ns,
                "frame accepted",
                frame_id=frame_id,
                previous_frame_id=previous_frame_id,
                gap_detected=gap_detected,
                missing_frame_count=missing_count,
                superseded_count=superseded_count,
            )
            return FrameAdmissionResult(status=event.status, event=event, packet=packet)

    def ingest(
        self,
        raw: RawFrame | None,
        received_at_ns: int | None = None,
    ) -> FrameAdmissionResult:
        """Serialize one direct admission with poll and session reset."""

        with self._producer_lock:
            return self._ingest_transaction(raw, received_at_ns)

    admit = ingest

    def read_latest(self, now_ns: int | None = None) -> FramePacket | None:
        """Return the current fresh packet unless a fatal fault is latched."""

        observed_at_ns = self._now_ns(now_ns)
        with self._lock:
            if self._latched_result is not None:
                return None
            clock_fault = self._observe_clock(
                observed_at_ns,
                frame_id=self._last_accepted_frame_id,
            )
            if clock_fault is not None:
                return None
            # Keep the adapter state and slot observation atomic.  Ingest and
            # reset acquire locks in the same adapter -> buffer order.
            try:
                return self._buffer.read_latest(observed_at_ns)
            except ValueError:
                last_packet = self._last_accepted_packet
                event = self._event(
                    FrameAdmissionStatus.TIMESTAMP_REGRESSION,
                    observed_at_ns,
                    "latest read clock moved backwards",
                    frame_id=None if last_packet is None else last_packet.frame_id,
                    previous_frame_id=self._last_accepted_frame_id,
                    details={
                        "latest_captured_at_ns": (
                            None if last_packet is None else last_packet.captured_at_ns
                        )
                    },
                )
                self._result(event)
                return None

    def reset_session(self, new_session_id: str) -> None:
        """Clear state while binding subsequent frames to a new session."""

        ensure_non_empty_str(new_session_id, "new_session_id")
        with self._producer_lock, self._lock:
            if new_session_id == self._session_id:
                raise ValueError("new_session_id must differ from the active session_id.")
            self._session_id = new_session_id
            self._last_accepted_frame_id = None
            self._last_accepted_captured_at_ns = None
            self._last_observed_at_ns = None
            self._last_accepted_packet = None
            self._latched_result = None
            self._buffer.clear()


__all__ = [
    "Clock",
    "FrameAdmissionEvent",
    "FrameAdmissionResult",
    "FrameAdmissionStatus",
    "FrameReader",
    "FrameSource",
    "FrameSourceAdapter",
    "FrameSourceConfig",
    "LatestFrameBuffer",
    "RawFrame",
    "calibration_sha256",
    "canonical_calibration_hash",
    "canonical_calibration_sha256",
    "canonical_geometry_hash",
    "canonical_geometry_sha256",
    "geometry_sha256",
]
