"""Pure VC-003 live marker/candidate-boundary integration.

This module deliberately stops at the boundary owned by G1-LOC-003C:

``VC003Source -> FrameSourceAdapter -> MinimapMarkerExtractor``.

It does not construct observations, affine transforms, platform graphs, or
planner actions.  The selector is independent of marker output, and every
public record is hash-only.  Pixels are kept in a capacity-one in-memory CAS
until the selected occurrence is copied to a caller-owned restricted
``PixelStore``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from threading import RLock
from typing import Any, Protocol, cast

from maple_automation_core.capture.frame_source import (
    FrameAdmissionResult,
    FrameAdmissionStatus,
    FrameSource,
    FrameSourceAdapter,
    FrameSourceConfig,
    RawFrame,
    canonical_geometry_sha256,
)
from maple_automation_core.capture.pixel_store import (
    PixelArtifact,
    PixelSpec,
    PixelStore,
    canonical_json,
    pixel_digest,
    validate_pixels,
)
from maple_automation_core.capture.vc003_source import VC003Source, VC003SourceConfig
from maple_automation_core.domain.frame import FramePacket, FrameSize, SourceGeometry, SourceRect
from maple_automation_core.localization.minimap_marker import (
    MinimapMarkerConfig,
    MinimapMarkerExtractor,
    MinimapMarkerResult,
)
from maple_automation_core.replay.event_tape import EventTape

SCHEMA_VERSION = "1.0.0"
VC003_LIVE_MARKER_SCHEMA_VERSION = SCHEMA_VERSION
VC003_LIVE_MARKER_VERSION = "g1-loc-003c-vc003-live-marker-v1"
VC003_LIVE_MARKER_CONFIG_VERSION = VC003_LIVE_MARKER_VERSION
VC003_LIVE_MARKER_SCOPE = "G1-LOC-003C"
VC003_LIVE_MARKER_TRUTH_SCOPE = "live_marker_integration_only"
GENERATION = 0
VC003_LIVE_MARKER_GENERATION = GENERATION

BUCKET_COUNT = 100
FIXED_BUCKET_COUNT = BUCKET_COUNT
BUCKET_DURATION_NS = 3_000_000_000
BUCKET_SPAN_NS = BUCKET_DURATION_NS
BUCKET_SECONDS = 3
WARMUP_SECONDS = 30
MEASUREMENT_SECONDS = 300
MEASUREMENT_DURATION_NS = MEASUREMENT_SECONDS * 1_000_000_000
MAX_AGE_NS = 250_000_000

FULL_FRAME_WIDTH = 1920
FULL_FRAME_HEIGHT = 1080
FULL_FRAME_CALIBRATION_SHA256 = "bde680518546eaef708f190a7087b5d7b6623a1b744826d5e9565d63d2c5d549"
MARKER_CONFIG_SEMANTIC_SHA256 = "47936cf77e46ebc62fd3d6dae241237307ebb370fd81a197745486812c58f22a"
MINIMAP_ROI = SourceRect(x=309, y=238, width=97, height=113)

_SHA256_ZERO = "0" * 64
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "path",
        "paths",
        "absolute_path",
        "raw_pixel",
        "raw_pixels",
        "pixels",
        "image_ref",
        "coordinates",
        "coordinate",
        "bbox",
        "source_bbox",
        "source_centroid",
        "working_bbox",
        "anchor_working",
        "centroid",
        "anchor",
        "x",
        "y",
        "device_original_id",
    }
)


class VC003LiveMarkerError(ValueError):
    """Base error for malformed or contradictory live-marker evidence."""


class VC003LiveMarkerValidationError(VC003LiveMarkerError):
    """Raised when selector, rows, results, or CAS lineage contradicts."""


class VC003LiveMarkerPrivacyError(VC003LiveMarkerValidationError):
    """Raised when a public row contains a private/raw field."""


LiveMarkerError = VC003LiveMarkerError
LiveMarkerValidationError = VC003LiveMarkerValidationError


def _canonical(value: object) -> bytes:
    try:
        return canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise VC003LiveMarkerError("value must be strict JSON") from exc


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise VC003LiveMarkerError(f"{field_name} must be a SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise VC003LiveMarkerError(f"{field_name} must be a SHA-256 hex string") from exc
    return value.lower()


def _token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VC003LiveMarkerError(f"{field_name} must be a non-empty string")
    if any(char in value for char in "\\\r\n\t"):
        raise VC003LiveMarkerError(f"{field_name} must be a portable token")
    return value


def _non_negative(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise VC003LiveMarkerError(f"{field_name} must be a non-negative integer")
    return value


def _status(value: object) -> str:
    token = getattr(value, "value", value)
    if not isinstance(token, str):
        token = str(token)
    token = token.casefold()
    if token in {"candidate", "found", "detected", "accepted"}:
        return "candidate"
    if token in {"no_candidate", "no-marker", "no_marker", "none", "unknown"}:
        return "no_candidate"
    if token in {"fault", "error", "invalid", "rejected"}:
        return "fault"
    raise VC003LiveMarkerValidationError(f"unknown marker status: {token!r}")


def _obj_attr(value: object, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _ensure_time(value: object, field_name: str) -> int:
    return _non_negative(value, field_name)


def full_frame_geometry() -> SourceGeometry:
    """Return the fixed full-frame 1920x1080 geometry used by VC-003."""

    return SourceGeometry(
        source_size=FrameSize(width=FULL_FRAME_WIDTH, height=FULL_FRAME_HEIGHT),
        content_rect=SourceRect(x=0, y=0, width=FULL_FRAME_WIDTH, height=FULL_FRAME_HEIGHT),
        working_size=FrameSize(width=FULL_FRAME_WIDTH, height=FULL_FRAME_HEIGHT),
    )


FULL_FRAME_GEOMETRY = full_frame_geometry()
FULL_FRAME_PIXEL_SPEC = PixelSpec(width=FULL_FRAME_WIDTH, height=FULL_FRAME_HEIGHT)
FULL_FRAME_GEOMETRY_SHA256 = canonical_geometry_sha256(FULL_FRAME_GEOMETRY)


def default_minimap_marker_config(
    *,
    session_id: str | None = None,
    source_id: str | None = None,
    clock_domain: str | None = None,
    max_age_ns: int = MAX_AGE_NS,
) -> MinimapMarkerConfig:
    """Build the 003B marker contract without loading a crop geometry."""

    return MinimapMarkerConfig(
        geometry=FULL_FRAME_GEOMETRY,
        pixel_spec=FULL_FRAME_PIXEL_SPEC,
        transform_version="capture-v1",
        calibration_sha256=FULL_FRAME_CALIBRATION_SHA256,
        minimap_roi=MINIMAP_ROI,
        max_age_ns=_non_negative(max_age_ns, "max_age_ns"),
        session_id=session_id,
        source_id=source_id,
        clock_domain=clock_domain,
    )


DEFAULT_LIVE_MARKER_CONFIG = default_minimap_marker_config()
DEFAULT_MARKER_CONFIG = DEFAULT_LIVE_MARKER_CONFIG


def build_frame_source_config(
    source_config: VC003SourceConfig | None = None,
    *,
    max_age_ns: int = MAX_AGE_NS,
) -> FrameSourceConfig:
    """Create the adapter config from VC-003's *full-frame* source config."""

    source = VC003SourceConfig() if source_config is None else source_config
    if not isinstance(source, VC003SourceConfig):
        raise TypeError("source_config must be VC003SourceConfig")
    if source.geometry != FULL_FRAME_GEOMETRY:
        raise VC003LiveMarkerValidationError("VC-003 live marker requires full-frame geometry")
    if source.transform_version != "capture-v1":
        raise VC003LiveMarkerValidationError("VC-003 live marker requires capture-v1 calibration")
    _non_negative(max_age_ns, "max_age_ns")
    return FrameSourceConfig(
        session_id=source.session_id,
        source_id=source.source_id,
        clock_domain=source.clock_domain,
        transform_version=source.transform_version,
        source_geometry=source.geometry,
        max_age_ns=max_age_ns,
    )


make_frame_source_config = build_frame_source_config
full_frame_source_config = build_frame_source_config


@dataclass(frozen=True, slots=True, init=False)
class VC003LiveMarkerThresholds:
    """Frozen sampling/accounting thresholds for one live-marker run."""

    bucket_count: int
    bucket_duration_ns: int
    max_age_ns: int
    generation: int
    min_candidate_count: int
    max_marker_faults: int

    def __init__(
        self,
        bucket_count: int = BUCKET_COUNT,
        bucket_duration_ns: int = BUCKET_DURATION_NS,
        *,
        bucket_seconds: int | float | None = None,
        max_age_ns: int = MAX_AGE_NS,
        generation: int = GENERATION,
        min_candidate_count: int = 1,
        max_marker_faults: int = 0,
    ) -> None:
        if bucket_seconds is not None and bucket_seconds != BUCKET_SECONDS:
            raise VC003LiveMarkerValidationError("bucket_seconds is fixed at 3")
        if bucket_count != BUCKET_COUNT:
            raise VC003LiveMarkerValidationError("bucket_count is fixed at 100")
        if bucket_duration_ns != BUCKET_DURATION_NS:
            raise VC003LiveMarkerValidationError("bucket_duration_ns is fixed at 3 seconds")
        if generation != GENERATION:
            raise VC003LiveMarkerValidationError("generation is fixed at 0")
        object.__setattr__(self, "bucket_count", BUCKET_COUNT)
        object.__setattr__(self, "bucket_duration_ns", BUCKET_DURATION_NS)
        object.__setattr__(self, "max_age_ns", _non_negative(max_age_ns, "max_age_ns"))
        object.__setattr__(self, "generation", GENERATION)
        object.__setattr__(
            self, "min_candidate_count", _non_negative(min_candidate_count, "min_candidate_count")
        )
        object.__setattr__(
            self, "max_marker_faults", _non_negative(max_marker_faults, "max_marker_faults")
        )

    @property
    def bucket_seconds(self) -> int:
        return BUCKET_SECONDS

    @property
    def duration_ns(self) -> int:
        return self.bucket_duration_ns

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "version": VC003_LIVE_MARKER_VERSION,
            "scope": VC003_LIVE_MARKER_SCOPE,
            "truth_scope": VC003_LIVE_MARKER_TRUTH_SCOPE,
            "bucket_count": self.bucket_count,
            "bucket_duration_ns": self.bucket_duration_ns,
            "bucket_seconds": BUCKET_SECONDS,
            "measurement_seconds": MEASUREMENT_SECONDS,
            "max_age_ns": self.max_age_ns,
            "generation": self.generation,
            "min_candidate_count": self.min_candidate_count,
            "max_marker_faults": self.max_marker_faults,
        }

    to_hash_only_dict = to_dict
    to_hash_only = to_dict


LiveMarkerThresholds = VC003LiveMarkerThresholds
VC003LiveMarkerThresholdConfig = VC003LiveMarkerThresholds
LiveMarkerThresholdConfig = VC003LiveMarkerThresholds
VC003Thresholds = VC003LiveMarkerThresholds


class PixelStoreReader(Protocol):
    def read(self, digest: str, spec: PixelSpec | None = None) -> bytes: ...


class ReadOnlyPixelStore:
    """Minimal reader view passed into ``MinimapMarkerExtractor``.

    The wrapper intentionally has no write, path, root, or mutation method.
    """

    __slots__ = ("_store",)

    def __init__(self, store: PixelStoreReader) -> None:
        if not callable(getattr(store, "read", None)):
            raise TypeError("store must expose read(digest, spec)")
        self._store = store

    def read(self, digest: str, spec: PixelSpec | None = None) -> bytes:
        return bytes(self._store.read(_sha256(digest, "digest"), spec))

    get = read
    load = read
    read_pixels = read

    def exists(self, digest: str, spec: PixelSpec | None = None) -> bool:
        try:
            self.read(digest, spec)
        except Exception:
            return False
        return True

    has = exists


ReadOnlyPixelStoreWrapper = ReadOnlyPixelStore
PixelStoreReadOnly = ReadOnlyPixelStore
CASReadOnly = ReadOnlyPixelStore


@dataclass(frozen=True, slots=True)
class MemoryCASArtifact:
    """Private metadata for the one in-memory CAS slot."""

    pixel_digest: str
    spec: PixelSpec
    byte_length: int
    source_provenance_id: str
    session_id: str
    source_sequence: int

    def __post_init__(self) -> None:
        _sha256(self.pixel_digest, "pixel_digest")
        if not isinstance(self.spec, PixelSpec):
            raise TypeError("spec must be PixelSpec")
        if self.byte_length != self.spec.length:
            raise VC003LiveMarkerValidationError("memory artifact byte length does not match spec")
        _token(self.source_provenance_id, "source_provenance_id")
        _token(self.session_id, "session_id")
        _non_negative(self.source_sequence, "source_sequence")

    @property
    def digest(self) -> str:
        return self.pixel_digest

    @property
    def artifact_sha256(self) -> str:
        return _digest(
            {
                "pixel_digest": self.pixel_digest,
                "spec": self.spec.to_dict(),
                "byte_length": self.byte_length,
                "source_provenance_id": self.source_provenance_id,
                "session_id": self.session_id,
                "source_sequence": self.source_sequence,
            }
        )

    @property
    def ref(self) -> str:
        return f"cas://sha256/{self.pixel_digest}"


@dataclass(frozen=True, slots=True)
class _MemoryEntry:
    artifact: MemoryCASArtifact
    data: bytes = field(repr=False, compare=False)


class CapacityOneMemoryCAS:
    """Thread-safe, capacity-one volatile pixel CAS.

    ``put`` atomically replaces the current sample.  ``retain_selected``
    copies exactly one digest to an external ``PixelStore`` occurrence and
    verifies the external read-back before returning.
    """

    capacity = 1

    def __init__(self) -> None:
        self._lock = RLock()
        self._entry: _MemoryEntry | None = None
        self._superseded_count = 0
        self._put_count = 0

    @staticmethod
    def _arguments(
        spec_or_pixels: PixelSpec | object,
        pixels_or_spec: object | PixelSpec | None,
    ) -> tuple[PixelSpec, bytes]:
        if isinstance(spec_or_pixels, PixelSpec):
            if pixels_or_spec is None or isinstance(pixels_or_spec, PixelSpec):
                raise TypeError("pixels are required")
            return spec_or_pixels, validate_pixels(spec_or_pixels, pixels_or_spec)
        if isinstance(pixels_or_spec, PixelSpec):
            return pixels_or_spec, validate_pixels(pixels_or_spec, spec_or_pixels)
        raise TypeError("put expects (PixelSpec, pixels) or (pixels, PixelSpec)")

    def put(
        self,
        spec_or_pixels: PixelSpec | object,
        pixels_or_spec: object | PixelSpec | None = None,
        *,
        source_provenance_id: str = "unknown",
        session_id: str = "unknown",
        source_sequence: int = 0,
        expected_pixel_digest: str | None = None,
    ) -> str:
        spec, data = self._arguments(spec_or_pixels, pixels_or_spec)
        digest = pixel_digest(spec, data)
        if (
            expected_pixel_digest is not None
            and _sha256(expected_pixel_digest, "expected_pixel_digest") != digest
        ):
            raise VC003LiveMarkerValidationError("expected pixel digest does not match bytes")
        provenance = _token(source_provenance_id, "source_provenance_id")
        session = _token(session_id, "session_id")
        sequence = _non_negative(source_sequence, "source_sequence")
        artifact = MemoryCASArtifact(
            pixel_digest=digest,
            spec=spec,
            byte_length=len(data),
            source_provenance_id=provenance,
            session_id=session,
            source_sequence=sequence,
        )
        with self._lock:
            if self._entry is not None:
                self._superseded_count += 1
            self._entry = _MemoryEntry(artifact=artifact, data=bytes(data))
            self._put_count += 1
        return digest

    store = put
    write = put

    def read(self, digest: str, spec: PixelSpec | None = None) -> bytes:
        expected = _sha256(digest, "digest")
        with self._lock:
            entry = self._entry
            if entry is None or entry.artifact.pixel_digest != expected:
                raise VC003LiveMarkerValidationError("pixel digest is not in the capacity-one CAS")
            if spec is not None and entry.artifact.spec != spec:
                raise VC003LiveMarkerValidationError("pixel spec does not match CAS entry")
            data = entry.data
            actual_spec = entry.artifact.spec
        if pixel_digest(actual_spec, data) != expected:
            raise VC003LiveMarkerValidationError("in-memory CAS digest verification failed")
        return bytes(data)

    get = read
    load = read
    read_pixels = read

    def artifact(self, digest: str) -> MemoryCASArtifact:
        expected = _sha256(digest, "digest")
        with self._lock:
            if self._entry is None or self._entry.artifact.pixel_digest != expected:
                raise VC003LiveMarkerValidationError("pixel digest is not in the capacity-one CAS")
            return self._entry.artifact

    get_artifact = artifact

    def exists(self, digest: str, spec: PixelSpec | None = None) -> bool:
        try:
            self.read(digest, spec)
        except Exception:
            return False
        return True

    has = exists

    @property
    def superseded_count(self) -> int:
        with self._lock:
            return self._superseded_count

    @property
    def put_count(self) -> int:
        with self._lock:
            return self._put_count

    @property
    def latest_digest(self) -> str | None:
        with self._lock:
            return None if self._entry is None else self._entry.artifact.pixel_digest

    @property
    def latest(self) -> MemoryCASArtifact | None:
        with self._lock:
            return None if self._entry is None else self._entry.artifact

    def snapshot(self) -> dict[str, Any]:
        latest = self.latest
        return {
            "capacity": 1,
            "occupied": latest is not None,
            "latest_pixel_digest": None if latest is None else latest.pixel_digest,
            "put_count": self.put_count,
            "superseded_count": self.superseded_count,
        }

    def retain_selected(
        self,
        digest: str,
        destination: PixelStore,
        *,
        source_provenance_id: str,
        session_id: str,
        source_sequence: int,
        privacy_class: str = "restricted",
        retention_class: str = "candidate",
    ) -> PixelArtifact:
        expected = _sha256(digest, "digest")
        if privacy_class != "restricted" or retention_class != "candidate":
            raise VC003LiveMarkerValidationError(
                "selected retention is fixed to restricted/candidate"
            )
        with self._lock:
            entry = self._entry
            if entry is None or entry.artifact.pixel_digest != expected:
                raise VC003LiveMarkerValidationError(
                    "selected occurrence was evicted before retention"
                )
            spec = entry.artifact.spec
            data = bytes(entry.data)
            entry_provenance = entry.artifact.source_provenance_id
            entry_session = entry.artifact.session_id
            entry_sequence = entry.artifact.source_sequence
        provenance = _token(source_provenance_id, "source_provenance_id")
        session = _token(session_id, "session_id")
        sequence = _non_negative(source_sequence, "source_sequence")
        if (
            provenance != entry_provenance
            or session != entry_session
            or sequence != entry_sequence
        ):
            raise VC003LiveMarkerValidationError(
                "selected retention identity does not match the CAS occurrence"
            )
        if not callable(getattr(destination, "put_artifact", None)):
            raise TypeError("destination must expose put_artifact")
        artifact = destination.put_artifact(
            spec,
            data,
            privacy_class=privacy_class,
            retention_class=retention_class,
            source_provenance_id=provenance,
            session_id=session,
            source_sequence=sequence,
            expected_pixel_digest=expected,
        )
        if not isinstance(artifact, PixelArtifact):
            raise VC003LiveMarkerValidationError("external CAS returned invalid artifact")
        if (
            artifact.pixel_digest != expected
            or artifact.privacy_class != privacy_class
            or artifact.retention_class != retention_class
            or artifact.source_provenance_id != provenance
            or artifact.session_id != session
            or artifact.source_sequence != sequence
        ):
            raise VC003LiveMarkerValidationError("external CAS changed selected digest")
        if destination.read(expected, spec) != data:
            raise VC003LiveMarkerValidationError("external CAS read-back mismatch")
        return artifact

    retain = retain_selected
    retain_to = retain_selected
    retain_selected_occurrence = retain_selected


InMemoryCapacityOneCAS = CapacityOneMemoryCAS
CapacityOneCAS = CapacityOneMemoryCAS
EphemeralMemoryCAS = CapacityOneMemoryCAS


@dataclass(frozen=True, slots=True)
class VC003BucketSelection:
    """One selector decision and its original accepted packet."""

    bucket_index: int
    bucket_start_ns: int
    bucket_end_ns: int
    admitted_at_ns: int
    frame_id: int
    captured_at_ns: int
    received_at_ns: int
    session_id: str
    source_id: str
    pixel_digest: str
    packet: object = field(repr=False, compare=False)
    generation: int = GENERATION

    def __post_init__(self) -> None:
        if not 0 <= self.bucket_index < BUCKET_COUNT:
            raise VC003LiveMarkerValidationError("bucket index is outside fixed selector")
        start = _ensure_time(self.bucket_start_ns, "bucket_start_ns")
        end = _ensure_time(self.bucket_end_ns, "bucket_end_ns")
        admitted = _ensure_time(self.admitted_at_ns, "admitted_at_ns")
        if end - start != BUCKET_DURATION_NS or not start <= admitted < end:
            raise VC003LiveMarkerValidationError("selection timestamp is not in a 3-second bucket")
        if admitted != self.received_at_ns:
            raise VC003LiveMarkerValidationError(
                "admitted_at_ns must equal FramePacket.received_at_ns"
            )
        _non_negative(self.frame_id, "frame_id")
        _ensure_time(self.captured_at_ns, "captured_at_ns")
        _ensure_time(self.received_at_ns, "received_at_ns")
        if self.captured_at_ns > self.received_at_ns:
            raise VC003LiveMarkerValidationError(
                "captured_at_ns must not exceed received_at_ns"
            )
        _token(self.session_id, "session_id")
        _token(self.source_id, "source_id")
        _sha256(self.pixel_digest, "pixel_digest")
        if self.generation != GENERATION:
            raise VC003LiveMarkerValidationError("generation is fixed at 0")
        packet_frame_id = _obj_attr(self.packet, "frame_id", default=None)
        if packet_frame_id is not None and packet_frame_id != self.frame_id:
            raise VC003LiveMarkerValidationError("selection frame_id does not match packet")
        packet_session_id = _obj_attr(self.packet, "session_id", default=None)
        if packet_session_id is not None and packet_session_id != self.session_id:
            raise VC003LiveMarkerValidationError("selection session_id does not match packet")
        packet_source_id = _obj_attr(self.packet, "source_id", default=None)
        if packet_source_id is not None and packet_source_id != self.source_id:
            raise VC003LiveMarkerValidationError("selection source_id does not match packet")
        packet_digest = _obj_attr(self.packet, "content_hash", "pixel_digest", default=None)
        if packet_digest is not None and packet_digest != self.pixel_digest:
            raise VC003LiveMarkerValidationError("selection pixel_digest does not match packet")
        packet_received = _obj_attr(self.packet, "received_at_ns", default=None)
        if packet_received is not None and packet_received != self.received_at_ns:
            raise VC003LiveMarkerValidationError(
                "selection received_at_ns does not match packet"
            )

    @property
    def frame(self) -> object:
        return self.packet

    @property
    def sample_id(self) -> str:
        return f"bucket-{self.bucket_index:03d}"

    @property
    def occurrence_id(self) -> str:
        return f"{self.session_id}:{self.source_id}:{self.frame_id}"

    @property
    def frame_digest(self) -> str:
        """Hash of selected frame identity without exposing pixel bytes."""

        return _digest(
            {
                "session_id": self.session_id,
                "source_id": self.source_id,
                "frame_id": self.frame_id,
                "captured_at_ns": self.captured_at_ns,
                "admitted_at_ns": self.admitted_at_ns,
                "pixel_digest": self.pixel_digest,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": VC003_LIVE_MARKER_SCOPE,
            "sample_id": self.sample_id,
            "bucket_index": self.bucket_index,
            "bucket_start_ns": self.bucket_start_ns,
            "bucket_end_ns": self.bucket_end_ns,
            "admitted_at_ns": self.admitted_at_ns,
            "frame_id": self.frame_id,
            "captured_at_ns": self.captured_at_ns,
            "received_at_ns": self.received_at_ns,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "pixel_digest": self.pixel_digest,
            "generation": self.generation,
        }

    to_hash_only_dict = to_dict
    to_hash_only = to_dict


SelectedOccurrence = VC003BucketSelection
BucketSelection = VC003BucketSelection


class FixedBucketSelector:
    """Output-independent first-accepted selector for 100 half-open buckets."""

    def __init__(
        self,
        start_at_ns: int = 0,
        *,
        measurement_start_ns: int | None = None,
        origin_ns: int | None = None,
        bucket_count: int = BUCKET_COUNT,
        bucket_duration_ns: int = BUCKET_DURATION_NS,
    ) -> None:
        alternatives = [value for value in (measurement_start_ns, origin_ns) if value is not None]
        if len(alternatives) > 1 or (
            alternatives and start_at_ns != 0 and alternatives[0] != start_at_ns
        ):
            raise VC003LiveMarkerValidationError("selector start aliases must agree")
        if bucket_count != BUCKET_COUNT or bucket_duration_ns != BUCKET_DURATION_NS:
            raise VC003LiveMarkerValidationError("selector is fixed at 100 buckets of 3 seconds")
        self.start_at_ns = _ensure_time(
            start_at_ns if not alternatives else alternatives[0], "start_at_ns"
        )
        self.bucket_count = BUCKET_COUNT
        self.bucket_duration_ns = BUCKET_DURATION_NS
        self.generation = GENERATION
        self._selected: dict[int, VC003BucketSelection] = {}
        self._accepted: dict[int, list[VC003BucketSelection]] = {}
        self._occurrences: set[tuple[str, str, int]] = set()
        self._lock = RLock()

    @property
    def end_at_ns(self) -> int:
        return self.start_at_ns + BUCKET_COUNT * BUCKET_DURATION_NS

    @property
    def selector_version(self) -> str:
        return VC003_LIVE_MARKER_VERSION

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def bucket_index(self, admitted_at_ns: int) -> int | None:
        admitted = _ensure_time(admitted_at_ns, "admitted_at_ns")
        if admitted < self.start_at_ns or admitted >= self.end_at_ns:
            return None
        return (admitted - self.start_at_ns) // BUCKET_DURATION_NS

    bucket_for = bucket_index

    @staticmethod
    def _accepted_packet(value: object) -> tuple[object, int] | None:
        if isinstance(value, FrameAdmissionResult):
            if not value.accepted or value.packet is None:
                return None
            return value.packet, value.event.observed_at_ns
        if isinstance(value, FramePacket):
            return value, value.received_at_ns
        accepted = _obj_attr(value, "accepted", default=None)
        status = _obj_attr(value, "status", "admission_status", default=None)
        if accepted is False or status in {
            FrameAdmissionStatus.NO_FRAME,
            FrameAdmissionStatus.STALE,
            FrameAdmissionStatus.DUPLICATE,
            FrameAdmissionStatus.OUT_OF_ORDER,
            FrameAdmissionStatus.FRAME_SIZE_CHANGED,
            FrameAdmissionStatus.SOURCE_MISMATCH,
            FrameAdmissionStatus.SESSION_MISMATCH,
            FrameAdmissionStatus.CLOCK_DOMAIN_MISMATCH,
            FrameAdmissionStatus.SOURCE_ERROR,
        }:
            return None
        packet = _obj_attr(value, "packet", "frame", default=value)
        admitted = _obj_attr(value, "admitted_at_ns", default=None)
        if admitted is None:
            event = _obj_attr(value, "event", default=None)
            admitted = _obj_attr(event, "observed_at_ns", default=None)
        if admitted is None:
            admitted = _obj_attr(packet, "received_at_ns", default=None)
        if packet is None or admitted is None:
            raise TypeError("accepted occurrence must expose packet and admitted_at_ns")
        return packet, _ensure_time(admitted, "admitted_at_ns")

    def consider(self, value: object, *, admitted_at_ns: int | None = None) -> bool:
        accepted = self._accepted_packet(value)
        if accepted is None:
            return False
        packet, inferred_admitted = accepted
        # The adapter's receive timestamp is the only bucket clock.  An
        # explicit timestamp is accepted as a consistency assertion, never as
        # a replacement that could forge a bucket.
        packet_received = _ensure_time(
            _obj_attr(packet, "received_at_ns", default=inferred_admitted),
            "received_at_ns",
        )
        if (
            admitted_at_ns is not None
            and _ensure_time(admitted_at_ns, "admitted_at_ns") != packet_received
        ):
            raise VC003LiveMarkerValidationError(
                "selector bucket clock must equal FramePacket.received_at_ns"
            )
        admitted = packet_received
        index = self.bucket_index(admitted)
        if index is None:
            return False
        frame_id = _non_negative(_obj_attr(packet, "frame_id", default=-1), "frame_id")
        session_id = _token(_obj_attr(packet, "session_id", default=""), "session_id")
        source_id = _token(_obj_attr(packet, "source_id", default=""), "source_id")
        content_hash = _sha256(
            _obj_attr(packet, "content_hash", "pixel_digest", default=""), "pixel_digest"
        )
        captured = _ensure_time(_obj_attr(packet, "captured_at_ns", default=0), "captured_at_ns")
        received = packet_received
        selection = VC003BucketSelection(
            bucket_index=index,
            bucket_start_ns=self.start_at_ns + index * BUCKET_DURATION_NS,
            bucket_end_ns=self.start_at_ns + (index + 1) * BUCKET_DURATION_NS,
            admitted_at_ns=admitted,
            frame_id=frame_id,
            captured_at_ns=captured,
            received_at_ns=received,
            session_id=session_id,
            source_id=source_id,
            pixel_digest=content_hash,
            packet=packet,
        )
        occurrence = (session_id, source_id, frame_id)
        with self._lock:
            if occurrence in self._occurrences:
                raise VC003LiveMarkerValidationError("duplicate accepted frame occurrence")
            self._occurrences.add(occurrence)
            bucket_accepted = self._accepted.setdefault(index, [])
            bucket_accepted.append(selection)
            if index in self._selected:
                return False
            self._selected[index] = selection
            return True

    accept = consider
    admit = consider
    add = consider

    @property
    def selected(self) -> tuple[VC003BucketSelection, ...]:
        with self._lock:
            return tuple(self._selected[index] for index in sorted(self._selected))

    @property
    def selections(self) -> tuple[VC003BucketSelection, ...]:
        return self.selected

    @property
    def accepted_ledger(self) -> tuple[VC003BucketSelection, ...]:
        with self._lock:
            return tuple(item for index in sorted(self._accepted) for item in self._accepted[index])

    @property
    def coverage(self) -> int:
        with self._lock:
            return len(self._selected)

    @property
    def missing_buckets(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(index for index in range(BUCKET_COUNT) if index not in self._selected)

    @property
    def complete(self) -> bool:
        return self.coverage == BUCKET_COUNT

    def validate_complete(self) -> None:
        if not self.complete:
            raise VC003LiveMarkerValidationError(
                f"selector missing buckets: {list(self.missing_buckets)!r}"
            )
        self.validate_first_accepted()

    def validate_first_accepted(self) -> None:
        """Verify that every selected bucket is the physical first acceptance."""

        for index, entries in self._accepted.items():
            if not entries or self._selected[index] != entries[0]:
                raise VC003LiveMarkerValidationError("selector first-accepted invariant failed")

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            buckets: list[dict[str, Any] | None] = [
                None if index not in self._selected else self._selected[index].to_dict()
                for index in range(BUCKET_COUNT)
            ]
            accepted_counts = [len(self._accepted.get(index, [])) for index in range(BUCKET_COUNT)]
        return {
            "schema_version": SCHEMA_VERSION,
            "version": self.selector_version,
            "scope": VC003_LIVE_MARKER_SCOPE,
            "truth_scope": VC003_LIVE_MARKER_TRUTH_SCOPE,
            "start_at_ns": self.start_at_ns,
            "end_at_ns": self.end_at_ns,
            "bucket_count": BUCKET_COUNT,
            "bucket_duration_ns": BUCKET_DURATION_NS,
            "generation": GENERATION,
            "coverage": self.coverage,
            "buckets": buckets,
            "accepted_counts": accepted_counts,
        }

    to_hash_only_dict = to_dict
    to_hash_only = to_dict

    @classmethod
    def select(
        cls,
        accepted: Iterable[object],
        *,
        start_at_ns: int = 0,
    ) -> FixedBucketSelector:
        selector = cls(start_at_ns=start_at_ns)
        for value in accepted:
            selector.consider(value)
        return selector


Fixed100BucketSelector = FixedBucketSelector
ThreeSecondBucketSelector = FixedBucketSelector


def _result_attr(result: object, *names: str, default: Any = None) -> Any:
    return _obj_attr(result, *names, default=default)


def _result_payload(result: object) -> dict[str, Any]:
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        body = to_dict()
    elif isinstance(result, Mapping):
        body = dict(result)
    else:
        raise TypeError("marker result must expose to_dict")
    if not isinstance(body, Mapping):
        raise TypeError("marker result to_dict must return a mapping")
    return cast(dict[str, Any], dict(body))


def _result_digest(result: object) -> str:
    declared = _result_attr(result, "result_digest", "digest", default=None)
    if isinstance(declared, str):
        return _sha256(declared, "result_digest")
    return _digest(_result_payload(result))


def _candidate_digest(result: object) -> str | None:
    candidate = _result_attr(result, "candidate", "player_candidate", default=None)
    if candidate is None:
        return None
    declared = _obj_attr(candidate, "digest", "candidate_digest", default=None)
    if isinstance(declared, str):
        return _sha256(declared, "candidate_digest")
    body = _obj_attr(candidate, "to_dict", default=None)
    return _digest(body() if callable(body) else candidate)


def _evidence_digest(result: object) -> str | None:
    evidence = _result_attr(result, "evidence", default=None)
    if evidence is None:
        return None
    declared = _obj_attr(evidence, "digest", "evidence_digest", default=None)
    if isinstance(declared, str):
        return _sha256(declared, "evidence_digest")
    body = _obj_attr(evidence, "to_dict", default=None)
    return _digest(body() if callable(body) else evidence)


def _fault_code(result: object) -> str | None:
    fault = _result_attr(result, "fault", default=None)
    if fault is None:
        return None
    code = _obj_attr(fault, "code", default=fault)
    token = getattr(code, "value", code)
    return _token(token, "fault_code")


@dataclass(frozen=True, slots=True)
class VC003SelectedRow:
    """Hash-only public row for one selected marker result."""

    sample_id: str
    bucket_index: int
    bucket_start_ns: int
    bucket_end_ns: int
    session_id: str
    source_id: str
    frame_id: int
    captured_at_ns: int
    admitted_at_ns: int
    checked_at_ns: int
    completed_at_ns: int
    generation: int
    pixel_digest: str
    artifact_sha256: str
    source_provenance_id: str
    source_sequence: int
    config_digest: str
    calibration_sha256: str
    marker_status: str
    fault_code: str | None
    evidence_digest: str | None
    candidate_digest: str | None
    result_digest: str
    state_digest: str
    plan_suppressed: bool
    retained: bool = True
    schema_version: str = SCHEMA_VERSION
    scope: str = VC003_LIVE_MARKER_SCOPE
    _public_frame_digest: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.scope != VC003_LIVE_MARKER_SCOPE:
            raise VC003LiveMarkerValidationError("selected row schema or scope mismatch")
        if self.sample_id != f"bucket-{self.bucket_index:03d}":
            raise VC003LiveMarkerValidationError("sample_id does not match bucket")
        if not 0 <= self.bucket_index < BUCKET_COUNT:
            raise VC003LiveMarkerValidationError("bucket index outside fixed selector")
        _non_negative(self.bucket_start_ns, "bucket_start_ns")
        _non_negative(self.bucket_end_ns, "bucket_end_ns")
        if self.bucket_end_ns - self.bucket_start_ns != BUCKET_DURATION_NS:
            raise VC003LiveMarkerValidationError("bucket width is not 3 seconds")
        if not self.bucket_start_ns <= self.admitted_at_ns < self.bucket_end_ns:
            raise VC003LiveMarkerValidationError("admitted timestamp is outside bucket")
        for text_value, text_name in (
            (self.session_id, "session_id"),
            (self.source_id, "source_id"),
            (self.source_provenance_id, "source_provenance_id"),
        ):
            _token(text_value, text_name)
        for integer_value, integer_name in (
            (self.frame_id, "frame_id"),
            (self.captured_at_ns, "captured_at_ns"),
            (self.admitted_at_ns, "admitted_at_ns"),
            (self.checked_at_ns, "checked_at_ns"),
            (self.completed_at_ns, "completed_at_ns"),
            (self.source_sequence, "source_sequence"),
        ):
            _non_negative(integer_value, integer_name)
        if self.checked_at_ns < self.admitted_at_ns or self.completed_at_ns < self.checked_at_ns:
            raise VC003LiveMarkerValidationError("row timestamps moved backwards")
        if self.captured_at_ns > self.admitted_at_ns:
            raise VC003LiveMarkerValidationError(
                "captured_at_ns must not exceed admitted_at_ns"
            )
        if self.generation != GENERATION:
            raise VC003LiveMarkerValidationError("generation is fixed at 0")
        for value, name in (
            (self.pixel_digest, "pixel_digest"),
            (self.artifact_sha256, "artifact_sha256"),
            (self.config_digest, "config_digest"),
            (self.calibration_sha256, "calibration_sha256"),
            (self.result_digest, "result_digest"),
            (self.state_digest, "state_digest"),
        ):
            _sha256(value, name)
        if self._public_frame_digest is not None:
            _sha256(self._public_frame_digest, "frame_digest")
        if self.calibration_sha256 != FULL_FRAME_CALIBRATION_SHA256:
            raise VC003LiveMarkerValidationError("row calibration is not full-frame")
        if self.evidence_digest is not None:
            _sha256(self.evidence_digest, "evidence_digest")
        if self.candidate_digest is not None:
            _sha256(self.candidate_digest, "candidate_digest")
        status = _status(self.marker_status)
        object.__setattr__(self, "marker_status", status)
        if status == "fault" and not self.fault_code:
            raise VC003LiveMarkerValidationError("fault row requires fault_code")
        if status == "fault" and self.fault_code is not None:
            _token(self.fault_code, "fault_code")
        if status != "fault" and self.fault_code is not None:
            _token(self.fault_code, "fault_code")
        if status == "candidate" and self.candidate_digest is None:
            raise VC003LiveMarkerValidationError("candidate row requires candidate_digest")
        if status != "candidate" and self.candidate_digest is not None:
            raise VC003LiveMarkerValidationError(
                "non-candidate row must not contain candidate_digest"
            )
        if type(self.plan_suppressed) is not bool or type(self.retained) is not bool:
            raise VC003LiveMarkerValidationError("row flags must be bool")
        if self.plan_suppressed != (status != "candidate"):
            raise VC003LiveMarkerValidationError("plan_suppressed contradicts marker status")

    @property
    def marker_result_digest(self) -> str:
        return self.result_digest

    @property
    def row_kind(self) -> str:
        return "public_hash_only"

    @property
    def status(self) -> str:
        return self.marker_status

    @property
    def frame_digest(self) -> str:
        """Digest of frame identity/receipt metadata, excluding raw bytes."""

        if self._public_frame_digest is not None:
            return self._public_frame_digest
        return _digest(
            {
                "session_id": self.session_id,
                "source_id": self.source_id,
                "frame_id": self.frame_id,
                "captured_at_ns": self.captured_at_ns,
                "admitted_at_ns": self.admitted_at_ns,
                "pixel_digest": self.pixel_digest,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self._public_body())

    @property
    def selected_digest(self) -> str:
        return self.digest

    def _public_body(self) -> dict[str, Any]:
        body = {
            "schema_version": self.schema_version,
            "row_kind": self.row_kind,
            "bucket_index": self.bucket_index,
            "status": self.status,
            "generation": self.generation,
            "frame_digest": self.frame_digest,
            "pixel_digest": self.pixel_digest,
            "candidate_digest": self.candidate_digest,
            "evidence_digest": self.evidence_digest or _SHA256_ZERO,
            "result_digest": self.result_digest,
            "sample_ordinal": self.bucket_index,
            "bucket_offset_ns": self.admitted_at_ns - self.bucket_start_ns,
            "selected": True,
        }
        return body

    def to_dict(self) -> dict[str, Any]:
        body = {
            **self._public_body(),
            "row_digest": self.digest,
        }
        _assert_public_hash_only(body)
        return body

    to_hash_only_dict = to_dict
    to_hash_only = to_dict

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VC003SelectedRow:
        if not isinstance(value, Mapping):
            raise TypeError("selected row must be a mapping")
        _assert_public_hash_only(value)
        if value.get("row_kind") == "public_hash_only":
            public_allowed = {
                "schema_version",
                "row_kind",
                "bucket_index",
                "status",
                "generation",
                "frame_digest",
                "pixel_digest",
                "candidate_digest",
                "evidence_digest",
                "result_digest",
                "row_digest",
                "sample_ordinal",
                "bucket_offset_ns",
                "selected",
            }
            unknown = set(value) - public_allowed
            if unknown:
                raise VC003LiveMarkerPrivacyError(
                    f"public selected row has unknown fields: {sorted(unknown)!r}"
                )
            required = {
                "schema_version",
                "row_kind",
                "bucket_index",
                "status",
                "generation",
                "frame_digest",
                "pixel_digest",
                "candidate_digest",
                "evidence_digest",
                "result_digest",
                "row_digest",
            }
            missing = required - set(value)
            if missing:
                raise VC003LiveMarkerValidationError(
                    f"public selected row missing {sorted(missing)[0]}"
                )
            if value["schema_version"] != SCHEMA_VERSION:
                raise VC003LiveMarkerValidationError("public row schema_version mismatch")
            if value["row_kind"] != "public_hash_only":
                raise VC003LiveMarkerValidationError("public row_kind mismatch")
            if value["generation"] != GENERATION:
                raise VC003LiveMarkerValidationError("public row generation is not zero")
            if "selected" in value and value["selected"] is not True:
                raise VC003LiveMarkerValidationError("public selected flag must be true")
            raw_status = value["status"]
            if raw_status not in {"candidate", "no_candidate", "rejected", "fault"}:
                raise VC003LiveMarkerValidationError("public row status is unsupported")
            bucket = _non_negative(value.get("bucket_index", -1), "bucket_index")
            if bucket >= BUCKET_COUNT:
                raise VC003LiveMarkerValidationError("bucket index outside fixed selector")
            status = _status(raw_status)
            frame_hash = _sha256(value["frame_digest"], "frame_digest")
            pixel = _sha256(value.get("pixel_digest", ""), "pixel_digest")
            result_hash = _sha256(value.get("result_digest", ""), "result_digest")
            evidence_hash = _sha256(value.get("evidence_digest", ""), "evidence_digest")
            frame_id = _non_negative(value.get("sample_ordinal", bucket), "sample_ordinal")
            start = bucket * BUCKET_DURATION_NS
            offset = _non_negative(value.get("bucket_offset_ns", 0), "bucket_offset_ns")
            if offset >= BUCKET_DURATION_NS:
                raise VC003LiveMarkerValidationError("bucket offset outside half-open bucket")
            candidate = value.get("candidate_digest")
            candidate_hash = None if candidate is None else _sha256(candidate, "candidate_digest")
            if status == "candidate" and candidate_hash is None:
                raise VC003LiveMarkerValidationError("candidate row requires candidate_digest")
            if status != "candidate" and candidate_hash is not None:
                raise VC003LiveMarkerValidationError(
                    "non-candidate row must not contain candidate_digest"
                )
            if "row_digest" in value:
                supplied_row_digest = _sha256(value["row_digest"], "row_digest")
                digest_body: dict[str, Any] = {
                    "schema_version": SCHEMA_VERSION,
                    "row_kind": "public_hash_only",
                    "bucket_index": bucket,
                    "status": raw_status,
                    "generation": GENERATION,
                    "frame_digest": frame_hash,
                    "pixel_digest": pixel,
                    "candidate_digest": candidate_hash,
                    "evidence_digest": evidence_hash,
                    "result_digest": result_hash,
                }
                if "sample_ordinal" in value:
                    digest_body["sample_ordinal"] = frame_id
                if "bucket_offset_ns" in value:
                    digest_body["bucket_offset_ns"] = offset
                if "selected" in value:
                    digest_body["selected"] = True
                expected_row_digest = _digest(digest_body)
                # ``row_digest`` is defined over the complete public body.
                # The detached parser cannot reconstruct private identity, so
                # verify only when it was produced by this implementation.
                if supplied_row_digest != expected_row_digest:
                    raise VC003LiveMarkerValidationError("public row_digest mismatch")
            return cls(
                sample_id=f"bucket-{bucket:03d}",
                bucket_index=bucket,
                bucket_start_ns=start,
                bucket_end_ns=start + BUCKET_DURATION_NS,
                session_id="unknown",
                source_id="unknown",
                frame_id=frame_id,
                captured_at_ns=start + offset,
                admitted_at_ns=start + offset,
                checked_at_ns=start + offset,
                completed_at_ns=start + offset,
                generation=GENERATION,
                pixel_digest=pixel,
                artifact_sha256=_SHA256_ZERO,
                source_provenance_id="unknown",
                source_sequence=frame_id,
                config_digest=_SHA256_ZERO,
                calibration_sha256=FULL_FRAME_CALIBRATION_SHA256,
                marker_status=status,
                fault_code=("public_fault" if status == "fault" else None),
                evidence_digest=evidence_hash,
                candidate_digest=candidate_hash,
                result_digest=result_hash,
                state_digest=_digest({"result_digest": result_hash, "status": status}),
                plan_suppressed=status != "candidate",
                _public_frame_digest=frame_hash,
            )
        allowed = set(cls.__dataclass_fields__) - {"_public_frame_digest"}
        unknown = set(value) - allowed
        if unknown:
            raise VC003LiveMarkerPrivacyError(
                f"selected row has unknown fields: {sorted(unknown)!r}"
            )
        required = allowed - {"schema_version", "scope", "retained"}
        missing = required - set(value)
        if missing:
            raise VC003LiveMarkerValidationError(f"selected row missing {sorted(missing)[0]}")
        return cls(**dict(value))

    @classmethod
    def from_result(
        cls,
        selection: VC003BucketSelection,
        result: object,
        artifact: PixelArtifact | MemoryCASArtifact,
        *,
        checked_at_ns: int | None = None,
        completed_at_ns: int | None = None,
        config_digest: str | None = None,
        calibration_sha256: str | None = None,
        source_provenance_id: str | None = None,
        source_sequence: int | None = None,
    ) -> VC003SelectedRow:
        artifact_digest = _obj_attr(artifact, "pixel_digest", "digest", default=None)
        if artifact_digest is not None and _sha256(artifact_digest, "artifact.pixel_digest") != (
            selection.pixel_digest
        ):
            raise VC003LiveMarkerValidationError(
                "artifact pixel digest does not match selected packet"
            )
        artifact_spec = _obj_attr(artifact, "spec", "pixel_spec", default=None)
        if artifact_spec is not None and artifact_spec != FULL_FRAME_PIXEL_SPEC:
            raise VC003LiveMarkerValidationError("artifact pixel spec is not full-frame")
        artifact_length = _obj_attr(artifact, "byte_length", "length", default=None)
        if artifact_length is not None and artifact_length != FULL_FRAME_PIXEL_SPEC.length:
            raise VC003LiveMarkerValidationError("artifact byte length is not full-frame")
        artifact_session = _obj_attr(artifact, "session_id", default=None)
        if artifact_session is not None and artifact_session != selection.session_id:
            raise VC003LiveMarkerValidationError("artifact session does not match selection")
        artifact_sequence = _obj_attr(artifact, "source_sequence", default=None)
        if artifact_sequence is not None and artifact_sequence != selection.frame_id:
            raise VC003LiveMarkerValidationError("artifact sequence does not match selection")
        artifact_provenance = _obj_attr(artifact, "source_provenance_id", default=None)
        if (
            source_provenance_id is not None
            and artifact_provenance is not None
            and source_provenance_id != artifact_provenance
        ):
            raise VC003LiveMarkerValidationError(
                "artifact provenance does not match selected occurrence"
            )
        if isinstance(artifact, PixelArtifact) and (
            artifact.privacy_class != "restricted" or artifact.retention_class != "candidate"
        ):
            raise VC003LiveMarkerValidationError(
                "selected artifact must be restricted/candidate"
            )
        status = _status(_result_attr(result, "status", default=None))
        evidence = _result_attr(result, "evidence", default=None)
        for actual, expected, name in (
            (_obj_attr(evidence, "frame_id", default=None), selection.frame_id, "frame_id"),
            (_obj_attr(evidence, "session_id", default=None), selection.session_id, "session_id"),
            (_obj_attr(evidence, "source_id", default=None), selection.source_id, "source_id"),
            (
                _obj_attr(evidence, "pixel_digest", "content_hash", default=None),
                selection.pixel_digest,
                "pixel_digest",
            ),
            (
                _obj_attr(evidence, "received_at_ns", default=None),
                selection.received_at_ns,
                "received_at_ns",
            ),
        ):
            if actual is not None and actual != expected:
                raise VC003LiveMarkerValidationError(
                    f"marker evidence {name} does not match selected packet"
                )
        candidate = _result_attr(result, "candidate", "player_candidate", default=None)
        for actual, expected, name in (
            (
                _obj_attr(candidate, "source_frame_id", "frame_id", default=None),
                selection.frame_id,
                "frame_id",
            ),
            (_obj_attr(candidate, "session_id", default=None), selection.session_id, "session_id"),
            (_obj_attr(candidate, "source_id", default=None), selection.source_id, "source_id"),
            (
                _obj_attr(candidate, "pixel_digest", default=None),
                selection.pixel_digest,
                "pixel_digest",
            ),
            (_obj_attr(candidate, "generation", default=None), GENERATION, "generation"),
        ):
            if actual is not None and actual != expected:
                raise VC003LiveMarkerValidationError(
                    f"marker candidate {name} does not match selected packet"
                )
        checked = (
            _result_attr(evidence, "checked_at_ns", default=None)
            if checked_at_ns is None
            else checked_at_ns
        )
        if checked is None:
            checked = selection.received_at_ns
        completed = checked if completed_at_ns is None else completed_at_ns
        config = config_digest or _obj_attr(evidence, "config_digest", default=None)
        calibration = calibration_sha256 or _obj_attr(evidence, "calibration_sha256", default=None)
        if config is None:
            config = _SHA256_ZERO
        if calibration is None:
            calibration = FULL_FRAME_CALIBRATION_SHA256
        provenance = source_provenance_id or artifact_provenance or "unknown"
        sequence = (
            source_sequence
            if source_sequence is not None
            else _obj_attr(artifact, "source_sequence", default=selection.frame_id)
        )
        result_hash = _result_digest(result)
        suppressed = status != "candidate"
        state_hash = _digest(
            {
                "scope": VC003_LIVE_MARKER_SCOPE,
                "sample_id": selection.sample_id,
                "status": status,
                "result_digest": result_hash,
                "plan_suppressed": suppressed,
            }
        )
        artifact_hash = _obj_attr(artifact, "artifact_sha256", default=None)
        if not isinstance(artifact_hash, str):
            artifact_hash = _digest(artifact)
        return cls(
            sample_id=selection.sample_id,
            bucket_index=selection.bucket_index,
            bucket_start_ns=selection.bucket_start_ns,
            bucket_end_ns=selection.bucket_end_ns,
            session_id=selection.session_id,
            source_id=selection.source_id,
            frame_id=selection.frame_id,
            captured_at_ns=selection.captured_at_ns,
            admitted_at_ns=selection.admitted_at_ns,
            checked_at_ns=_ensure_time(checked, "checked_at_ns"),
            completed_at_ns=_ensure_time(completed, "completed_at_ns"),
            generation=GENERATION,
            pixel_digest=selection.pixel_digest,
            artifact_sha256=_sha256(artifact_hash, "artifact_sha256"),
            source_provenance_id=_token(provenance, "source_provenance_id"),
            source_sequence=_non_negative(sequence, "source_sequence"),
            config_digest=_sha256(config, "config_digest"),
            calibration_sha256=_sha256(calibration, "calibration_sha256"),
            marker_status=status,
            fault_code=_fault_code(result),
            evidence_digest=_evidence_digest(result),
            candidate_digest=_candidate_digest(result),
            result_digest=result_hash,
            state_digest=state_hash,
            plan_suppressed=suppressed,
            retained=True,
        )

    build = from_result
    from_marker_result = from_result
    from_selection = from_result


PublicSelectedRow = VC003SelectedRow
HashOnlySelectedRow = VC003SelectedRow
SelectedRow = VC003SelectedRow


def _assert_public_hash_only(value: object) -> None:
    def walk(node: object, path: str) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                key_text = str(key)
                if key_text.casefold() in _FORBIDDEN_PUBLIC_KEYS:
                    raise VC003LiveMarkerPrivacyError(
                        f"private field in public row: {path}.{key_text}"
                    )
                walk(child, f"{path}.{key_text}")
        elif isinstance(node, list | tuple):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")
        elif isinstance(node, bytes | bytearray | memoryview):
            raise VC003LiveMarkerPrivacyError(f"raw bytes in public row: {path}")
        elif isinstance(node, str) and (
            node.startswith("/")
            or node.startswith("\\\\")
            or (len(node) >= 3 and node[1] == ":" and node[2] in "\\/")
        ):
            raise VC003LiveMarkerPrivacyError(f"absolute path in public row: {path}")

    walk(value, "row")
    _canonical(value)


@dataclass(frozen=True, slots=True)
class VC003RestrictedPrivateRow:
    """Restricted companion row retaining the full marker result payload."""

    public_row: VC003SelectedRow
    result: object
    artifact: PixelArtifact | MemoryCASArtifact
    marker_coordinates: Mapping[str, Any] = field(default_factory=dict)
    artifact_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.public_row, VC003SelectedRow):
            raise TypeError("public_row must be VC003SelectedRow")
        artifact_digest = _obj_attr(self.artifact, "pixel_digest", "digest", default=None)
        if artifact_digest is not None and _sha256(artifact_digest, "artifact.pixel_digest") != (
            self.public_row.pixel_digest
        ):
            raise VC003LiveMarkerValidationError(
                "restricted artifact pixel digest does not match public row"
            )
        artifact_session = _obj_attr(self.artifact, "session_id", default=None)
        if artifact_session is not None and artifact_session != self.public_row.session_id:
            raise VC003LiveMarkerValidationError(
                "restricted artifact session does not match public row"
            )
        artifact_sequence = _obj_attr(self.artifact, "source_sequence", default=None)
        if artifact_sequence is not None and artifact_sequence != self.public_row.source_sequence:
            raise VC003LiveMarkerValidationError(
                "restricted artifact sequence does not match public row"
            )
        if not isinstance(self.marker_coordinates, Mapping):
            raise TypeError("marker_coordinates must be a mapping")
        _canonical(self.marker_coordinates)
        if isinstance(self.artifact, PixelArtifact) and (
            self.artifact.privacy_class != "restricted"
            or self.artifact.retention_class != "candidate"
        ):
            raise VC003LiveMarkerValidationError(
                "restricted artifact must be restricted/candidate"
            )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        candidate = _result_attr(self.result, "candidate", default=None)
        evidence = _result_attr(self.result, "evidence", default=None)
        marker = _obj_attr(evidence, "marker", default=None)
        candidate_body: dict[str, Any] | None = None
        if candidate is not None:
            anchor = _obj_attr(candidate, "anchor_working", "anchor", default=None)
            candidate_body = {
                "x": float(_obj_attr(anchor, "x", default=0.0)),
                "y": float(_obj_attr(anchor, "y", default=0.0)),
            }
        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "row_kind": "restricted_verifier",
            "bucket_index": self.public_row.bucket_index,
            "sample_id": self.public_row.sample_id,
            "status": self.public_row.status,
            "generation": GENERATION,
            "session_id": self.public_row.session_id,
            "source_id": self.public_row.source_id,
            "frame_id": self.public_row.frame_id,
            "source_sequence": self.public_row.source_sequence,
            "frame_digest": self.public_row.frame_digest,
            "candidate_digest": self.public_row.candidate_digest,
            "evidence_digest": self.public_row.evidence_digest or _SHA256_ZERO,
            "result_digest": self.public_row.result_digest,
            "row_digest": self.public_row.digest,
            "observed_at_ns": self.public_row.checked_at_ns,
            "captured_at_ns": self.public_row.captured_at_ns,
            "received_at_ns": self.public_row.admitted_at_ns,
            "pixel_ref": f"external://vc003/pixels/{self.public_row.pixel_digest}",
            "verifier_artifact_ref": (
                f"external://vc003/artifacts/{self.public_row.artifact_sha256}"
            ),
            "artifact_ref": f"external://vc003/occurrences/{self.public_row.artifact_sha256}",
            "privacy_class": "restricted",
        }
        if candidate_body is not None:
            body["working_candidate"] = candidate_body
        if marker is not None:
            bbox = _obj_attr(marker, "source_bbox", "bbox", default=None)
            centroid = _obj_attr(marker, "source_centroid", "centroid", default=None)
            if bbox is not None:
                body["source_bbox"] = (
                    bbox.to_dict() if callable(getattr(bbox, "to_dict", None)) else bbox
                )
            if centroid is not None:
                body["source_centroid"] = list(centroid)
            body["component_area"] = _non_negative(_obj_attr(marker, "area", default=0), "area")
            body["bright_core_pixels"] = _non_negative(
                _obj_attr(marker, "bright_core_pixels", default=0), "bright_core_pixels"
            )
        return body

    @classmethod
    def from_result(
        cls,
        public_row: VC003SelectedRow,
        result: object,
        artifact: PixelArtifact | MemoryCASArtifact,
        *,
        marker_coordinates: Mapping[str, Any] | None = None,
        artifact_path: str | None = None,
    ) -> VC003RestrictedPrivateRow:
        return cls(
            public_row=public_row,
            result=result,
            artifact=artifact,
            marker_coordinates={} if marker_coordinates is None else dict(marker_coordinates),
            artifact_path=artifact_path,
        )


RestrictedPrivateRow = VC003RestrictedPrivateRow
VC003PrivateRow = VC003RestrictedPrivateRow
RestrictedRow = VC003RestrictedPrivateRow


@dataclass(frozen=True, slots=True)
class VC003MarkerAccounting:
    """Exact marker branch counts for selected occurrences."""

    selected: int = 0
    candidate: int = 0
    no_candidate: int = 0
    fault: int = 0

    def __post_init__(self) -> None:
        for value, name in (
            (self.selected, "selected"),
            (self.candidate, "candidate"),
            (self.no_candidate, "no_candidate"),
            (self.fault, "fault"),
        ):
            _non_negative(value, name)

    @property
    def marker_fault(self) -> int:
        return self.fault

    @property
    def total(self) -> int:
        return self.candidate + self.no_candidate + self.fault

    @property
    def valid(self) -> bool:
        return (
            self.selected == self.total
            and min(self.selected, self.candidate, self.no_candidate, self.fault) >= 0
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def validate(self, expected_selected: int | None = None) -> None:
        expected = (
            self.selected
            if expected_selected is None
            else _non_negative(expected_selected, "expected_selected")
        )
        if self.selected != expected or not self.valid:
            raise VC003LiveMarkerValidationError("marker accounting does not sum to selected")

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "candidate": self.candidate,
            "no_candidate": self.no_candidate,
            "fault": self.fault,
            "marker_fault": self.fault,
            "accounted": self.total,
            "valid": self.valid,
        }

    @classmethod
    def from_results(cls, results: Iterable[object]) -> VC003MarkerAccounting:
        candidate = no_candidate = fault = 0
        for result in results:
            branch = _status(_result_attr(result, "status", default=None))
            if branch == "candidate":
                candidate += 1
            elif branch == "no_candidate":
                no_candidate += 1
            else:
                fault += 1
        return cls(
            selected=candidate + no_candidate + fault,
            candidate=candidate,
            no_candidate=no_candidate,
            fault=fault,
        )


MarkerAccounting = VC003MarkerAccounting
LiveMarkerAccounting = VC003MarkerAccounting


def validate_marker_accounting(
    rows_or_results: Iterable[VC003SelectedRow | Mapping[str, Any] | object],
    *,
    expected_selected: int | None = None,
) -> VC003MarkerAccounting:
    rows = tuple(rows_or_results)
    results: list[object] = []
    for item in rows:
        if isinstance(item, VC003SelectedRow) or (
            isinstance(item, Mapping) and "marker_status" in item
        ):
            status_value = _obj_attr(item, "marker_status", default=None)
            results.append({"status": status_value})
        else:
            results.append(item)
    accounting = VC003MarkerAccounting.from_results(results)
    accounting.validate(expected_selected)
    return accounting


validate_accounting = validate_marker_accounting


@dataclass(frozen=True, slots=True)
class VC003FailClosedSummary:
    """Small, non-sensitive summary emitted before extractor invocation."""

    code: str
    status: str = "fault"
    session_id: str | None = None
    source_id: str | None = None
    frame_id: int | None = None
    expected_size: FrameSize | None = FULL_FRAME_GEOMETRY.source_size
    actual_size: FrameSize | None = None
    expected_geometry_sha256: str = FULL_FRAME_GEOMETRY_SHA256
    actual_geometry_sha256: str | None = None
    generation: int = GENERATION
    plan_suppressed: bool = True
    extractor_invoked: bool = False
    raw_pixels_public: bool = False
    coordinates_public: bool = False
    absolute_paths_public: bool = False

    def __post_init__(self) -> None:
        _token(self.code, "code")
        if self.status != "fault" or self.generation != GENERATION:
            raise VC003LiveMarkerValidationError(
                "fail-closed summary must be fault/generation zero"
            )
        if self.session_id is not None:
            _token(self.session_id, "session_id")
        if self.source_id is not None:
            _token(self.source_id, "source_id")
        if self.frame_id is not None:
            _non_negative(self.frame_id, "frame_id")
        _sha256(self.expected_geometry_sha256, "expected_geometry_sha256")
        if self.actual_geometry_sha256 is not None:
            _sha256(self.actual_geometry_sha256, "actual_geometry_sha256")
        if self.plan_suppressed is not True or self.extractor_invoked is not False:
            raise VC003LiveMarkerValidationError("fail-closed flags are fixed")
        if (
            self.raw_pixels_public is not False
            or self.coordinates_public is not False
            or self.absolute_paths_public is not False
        ):
            raise VC003LiveMarkerValidationError("fail-closed summary privacy flags are fixed")

    @property
    def digest(self) -> str:
        return _digest(self._body_dict())

    @property
    def calibration_sha256(self) -> str:
        return FULL_FRAME_CALIBRATION_SHA256

    def to_dict(self) -> dict[str, Any]:
        body = self._body_dict()
        body["digest"] = self.digest
        return body

    def _body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": VC003_LIVE_MARKER_SCOPE,
            "status": self.status,
            "code": self.code,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "frame_id": self.frame_id,
            "expected_size": None if self.expected_size is None else self.expected_size.to_dict(),
            "actual_size": None if self.actual_size is None else self.actual_size.to_dict(),
            "expected_geometry_sha256": self.expected_geometry_sha256,
            "actual_geometry_sha256": self.actual_geometry_sha256,
            "calibration_sha256": self.calibration_sha256,
            "generation": self.generation,
            "plan_suppressed": self.plan_suppressed,
            "extractor_invoked": self.extractor_invoked,
            "raw_pixels_public": self.raw_pixels_public,
            "coordinates_public": self.coordinates_public,
            "absolute_paths_public": self.absolute_paths_public,
        }


FailClosedSummary = VC003FailClosedSummary
LiveMarkerFailClosedSummary = VC003FailClosedSummary


@dataclass(frozen=True, slots=True)
class VC003LineageValidation:
    """Hash-only validator outcome; truthiness is the validity bit."""

    valid: bool
    checked_rows: int
    failures: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.valid

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": VC003_LIVE_MARKER_SCOPE,
            "valid": self.valid,
            "checked_rows": self.checked_rows,
            "failure_count": len(self.failures),
            "failures": list(self.failures),
        }


LineageValidation = VC003LineageValidation


def _normalize_selection(value: VC003BucketSelection | Mapping[str, Any]) -> VC003BucketSelection:
    if isinstance(value, VC003BucketSelection):
        return value
    raise TypeError("lineage validation requires selector selections, not detached rows")


def _normalize_row(
    value: VC003SelectedRow | VC003RestrictedPrivateRow | Mapping[str, Any],
) -> VC003SelectedRow:
    if isinstance(value, VC003SelectedRow):
        return value
    if isinstance(value, VC003RestrictedPrivateRow):
        return value.public_row
    if isinstance(value, Mapping) and value.get("row_kind") == "restricted_verifier":
        pixel_ref = value.get("pixel_ref")
        pixel_digest_value = (
            pixel_ref.rsplit("/", 1)[-1]
            if isinstance(pixel_ref, str) and "/" in pixel_ref
            else value.get("pixel_digest")
        )
        public: dict[str, Any] = {
            "schema_version": value.get("schema_version", SCHEMA_VERSION),
            "row_kind": "public_hash_only",
            "bucket_index": value.get("bucket_index"),
            "status": value.get("status"),
            "generation": value.get("generation", GENERATION),
            "frame_digest": value.get("frame_digest"),
            "pixel_digest": pixel_digest_value,
            "candidate_digest": value.get("candidate_digest"),
            "evidence_digest": value.get("evidence_digest"),
            "result_digest": value.get("result_digest"),
            "row_digest": value.get("row_digest"),
        }
        return VC003SelectedRow.from_dict(public)
    return VC003SelectedRow.from_dict(value)


def validate_live_marker_lineage(
    selections_or_selector: FixedBucketSelector | Sequence[VC003BucketSelection],
    rows: Sequence[VC003SelectedRow | VC003RestrictedPrivateRow | Mapping[str, Any]],
    results: Sequence[object] | None = None,
    *,
    retained_store: PixelStore | None = None,
    expected_geometry: SourceGeometry = FULL_FRAME_GEOMETRY,
    expected_calibration_sha256: str = FULL_FRAME_CALIBRATION_SHA256,
    expected_config_digest: str | None = None,
    expected_generation: int = GENERATION,
    require_complete: bool = True,
) -> VC003LineageValidation:
    """Validate selected rows, branch results, buckets, and retained CAS."""

    failures: list[str] = []
    if isinstance(selections_or_selector, FixedBucketSelector):
        selector = selections_or_selector
        selections = selector.selected
        try:
            selector.validate_first_accepted()
            if require_complete:
                selector.validate_complete()
        except VC003LiveMarkerError as exc:
            failures.append(str(exc))
    else:
        selector = None
        try:
            selections = tuple(_normalize_selection(item) for item in selections_or_selector)
        except (TypeError, ValueError) as exc:
            failures.append(f"selection_invalid:{type(exc).__name__}")
            selections = ()
    normalized_rows: list[VC003SelectedRow] = []
    serialized_public_rows: set[int] = set()
    serialized_public_fields: dict[int, Mapping[str, Any]] = {}
    for item in rows:
        try:
            if isinstance(item, Mapping) and item.get("row_kind") in {
                "public_hash_only",
                "restricted_verifier",
            }:
                bucket_value = item.get("bucket_index")
                if isinstance(bucket_value, int):
                    serialized_public_rows.add(bucket_value)
                    serialized_public_fields[bucket_value] = item
            normalized_rows.append(_normalize_row(item))
        except (TypeError, ValueError) as exc:
            failures.append(f"row_invalid:{type(exc).__name__}")
    if expected_generation != GENERATION:
        failures.append("generation_expected_not_zero")
    if expected_geometry != FULL_FRAME_GEOMETRY:
        failures.append("geometry_expected_not_full_frame")
    if (
        _sha256(expected_calibration_sha256, "expected_calibration_sha256")
        != FULL_FRAME_CALIBRATION_SHA256
    ):
        failures.append("calibration_expected_not_full_frame")
    selection_by_bucket: dict[int, VC003BucketSelection] = {}
    for selection in selections:
        if selection.bucket_index in selection_by_bucket:
            failures.append(f"duplicate_selection_bucket:{selection.bucket_index}")
        selection_by_bucket[selection.bucket_index] = selection
    occurrence_keys: set[tuple[str, str, int]] = set()
    for selection in selections:
        occurrence_key = (selection.session_id, selection.source_id, selection.frame_id)
        if occurrence_key in occurrence_keys:
            failures.append(f"duplicate_occurrence:{selection.bucket_index}")
        occurrence_keys.add(occurrence_key)
    row_by_bucket: dict[int, VC003SelectedRow] = {}
    for row_value in normalized_rows:
        if row_value.bucket_index in row_by_bucket:
            failures.append(f"duplicate_bucket:{row_value.bucket_index}")
        row_by_bucket[row_value.bucket_index] = row_value
    if require_complete:
        for index in range(BUCKET_COUNT):
            if index not in selection_by_bucket:
                failures.append(f"missing_selection:{index}")
            if index not in row_by_bucket:
                failures.append(f"missing_row:{index}")
    for index in set(selection_by_bucket) ^ set(row_by_bucket):
        failures.append(f"orphan_bucket:{index}")
    for index, selection in selection_by_bucket.items():
        row = row_by_bucket.get(index)
        if row is None:
            continue
        public_fields = serialized_public_fields.get(index)
        if (
            public_fields is not None
            and public_fields.get("frame_digest") != selection.frame_digest
        ):
            failures.append(f"frame_digest_mismatch:{index}")
        for actual, expected, name in (
            (row.sample_id, selection.sample_id, "sample_id"),
            (row.frame_id, selection.frame_id, "frame_id"),
            (row.source_sequence, selection.frame_id, "source_sequence"),
            (row.captured_at_ns, selection.captured_at_ns, "captured_at_ns"),
            (row.admitted_at_ns, selection.admitted_at_ns, "admitted_at_ns"),
            (row.pixel_digest, selection.pixel_digest, "pixel_digest"),
            (row.frame_digest, selection.frame_digest, "frame_digest"),
            (row.generation, GENERATION, "generation"),
            (row.session_id, selection.session_id, "session_id"),
            (row.source_id, selection.source_id, "source_id"),
            (row.bucket_start_ns, selection.bucket_start_ns, "bucket_start_ns"),
            (row.bucket_end_ns, selection.bucket_end_ns, "bucket_end_ns"),
        ):
            if index in serialized_public_rows and name in {
                "sample_id",
                "frame_id",
                "source_sequence",
                "captured_at_ns",
                "admitted_at_ns",
                "frame_digest",
                "generation",
                "session_id",
                "source_id",
                "bucket_start_ns",
                "bucket_end_ns",
            }:
                continue
            if actual != expected:
                failures.append(f"lineage_mismatch:{name}:{index}")
        packet_geometry = _obj_attr(selection.packet, "source_geometry", "geometry", default=None)
        if packet_geometry is not None and packet_geometry != expected_geometry:
            failures.append(f"geometry_mismatch:{index}")
        packet_size = _obj_attr(selection.packet, "source_size", "frame_size", default=None)
        if packet_size is not None and packet_size != expected_geometry.source_size:
            failures.append(f"size_mismatch:{index}")
        if index not in serialized_public_rows and not row.retained:
            failures.append(f"retention_not_attested:{index}")
        if (
            expected_config_digest is not None
            and index not in serialized_public_rows
            and row.config_digest != _sha256(expected_config_digest, "expected_config_digest")
        ):
            failures.append(f"config_mismatch:{index}")
        if index not in serialized_public_rows and row.calibration_sha256 != _sha256(
            expected_calibration_sha256, "expected_calibration_sha256"
        ):
            failures.append(f"calibration_mismatch:{index}")
        if retained_store is not None and index not in serialized_public_rows:
            spec = PixelSpec(
                width=expected_geometry.source_size.width,
                height=expected_geometry.source_size.height,
            )
            try:
                retained = retained_store.read(row.pixel_digest, spec)
                if pixel_digest(spec, retained) != row.pixel_digest:
                    failures.append(f"cas_digest_mismatch:{index}")
                # PixelStore.artifact() describes the CAS object and is
                # intentionally private/persistent.  Selected retention is
                # proven by the immutable occurrence envelope instead.
                occurrence = retained_store.occurrence(
                    row.pixel_digest,
                    source_provenance_id=row.source_provenance_id,
                    session_id=row.session_id,
                    source_sequence=row.source_sequence,
                )
                if (
                    occurrence.privacy_class != "restricted"
                    or occurrence.retention_class != "candidate"
                ):
                    failures.append(f"cas_retention_mismatch:{index}")
                if occurrence.artifact_sha256 != row.artifact_sha256:
                    failures.append(f"cas_artifact_mismatch:{index}")
            except Exception as exc:
                failures.append(f"cas_missing:{index}:{type(exc).__name__}")
    if results is not None:
        if len(results) != len(normalized_rows):
            failures.append("result_count_mismatch")
        for row, result in zip(
            sorted(normalized_rows, key=lambda item: item.bucket_index), results, strict=False
        ):
            try:
                if _status(_result_attr(result, "status", default=None)) != row.marker_status:
                    failures.append(f"result_status_mismatch:{row.bucket_index}")
                if _result_digest(result) != row.result_digest:
                    failures.append(f"result_digest_mismatch:{row.bucket_index}")
                if _evidence_digest(result) != row.evidence_digest:
                    failures.append(f"evidence_digest_mismatch:{row.bucket_index}")
                if _candidate_digest(result) != row.candidate_digest:
                    failures.append(f"candidate_digest_mismatch:{row.bucket_index}")
                if (
                    row.bucket_index not in serialized_public_rows
                    and _fault_code(result) != row.fault_code
                ):
                    failures.append(f"fault_code_mismatch:{row.bucket_index}")
                evidence = _result_attr(result, "evidence", default=None)
                if evidence is not None:
                    for actual, expected, name in (
                        (_obj_attr(evidence, "frame_id", default=None), row.frame_id, "frame_id"),
                        (
                            _obj_attr(evidence, "session_id", default=None),
                            row.session_id,
                            "session_id",
                        ),
                        (
                            _obj_attr(evidence, "source_id", default=None),
                            row.source_id,
                            "source_id",
                        ),
                        (
                            _obj_attr(evidence, "pixel_digest", default=None),
                            row.pixel_digest,
                            "pixel_digest",
                        ),
                    ):
                        if actual is not None and actual != expected:
                            failures.append(f"result_lineage_mismatch:{name}:{row.bucket_index}")
            except (TypeError, ValueError) as exc:
                failures.append(f"result_invalid:{row.bucket_index}:{type(exc).__name__}")
    try:
        validate_marker_accounting(normalized_rows, expected_selected=len(normalized_rows))
    except VC003LiveMarkerError as exc:
        failures.append(f"accounting:{exc}")
    unique_failures = tuple(dict.fromkeys(failures))
    return VC003LineageValidation(
        valid=not unique_failures,
        checked_rows=len(normalized_rows),
        failures=unique_failures,
    )


validate_vc003_lineage = validate_live_marker_lineage
validate_lineage = validate_live_marker_lineage
VC003LineageValidator = validate_live_marker_lineage
LiveMarkerLineageValidator = validate_live_marker_lineage


def _marker_result_row(
    selection: VC003BucketSelection,
    result: object,
    artifact: PixelArtifact | MemoryCASArtifact,
    *,
    checked_at_ns: int,
    completed_at_ns: int,
    config: MinimapMarkerConfig,
) -> tuple[VC003SelectedRow, VC003RestrictedPrivateRow]:
    row = VC003SelectedRow.from_result(
        selection,
        result,
        artifact,
        checked_at_ns=checked_at_ns,
        completed_at_ns=completed_at_ns,
        config_digest=config.digest,
        calibration_sha256=config.calibration_sha256,
        source_provenance_id=artifact.source_provenance_id,
        source_sequence=artifact.source_sequence,
    )
    return row, VC003RestrictedPrivateRow.from_result(row, result, artifact)


class VC003LiveMarkerRunner:
    """Small runtime adapter connecting source admission to real extraction."""

    def __init__(
        self,
        source: FrameSource | VC003Source,
        *,
        source_config: VC003SourceConfig | FrameSourceConfig | None = None,
        marker_config: MinimapMarkerConfig | None = None,
        pixel_store: PixelStoreReader | None = None,
        retained_store: PixelStore | None = None,
        memory_cas: CapacityOneMemoryCAS | None = None,
        selector: FixedBucketSelector | None = None,
        measurement_start_ns: int | None = None,
        thresholds: VC003LiveMarkerThresholds | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.source = source
        self.clock = clock
        self.thresholds = VC003LiveMarkerThresholds() if thresholds is None else thresholds
        if not isinstance(self.thresholds, VC003LiveMarkerThresholds):
            raise TypeError("thresholds must be VC003LiveMarkerThresholds")
        if isinstance(source_config, FrameSourceConfig):
            adapter_config = source_config
            if adapter_config.geometry != FULL_FRAME_GEOMETRY:
                raise VC003LiveMarkerValidationError("adapter geometry must be full-frame")
        else:
            if source_config is None and isinstance(source, VC003Source):
                source_config = source.config
            adapter_config = build_frame_source_config(
                source_config, max_age_ns=self.thresholds.max_age_ns
            )
        if adapter_config.transform_version != "capture-v1":
            raise VC003LiveMarkerValidationError(
                "adapter transform must use the fixed capture-v1 calibration"
            )
        if adapter_config.calibration_hash != FULL_FRAME_CALIBRATION_SHA256:
            raise VC003LiveMarkerValidationError(
                "adapter calibration does not match full-frame contract"
            )
        if adapter_config.max_age_ns != self.thresholds.max_age_ns:
            raise VC003LiveMarkerValidationError("adapter max_age_ns does not match thresholds")
        marker = (
            default_minimap_marker_config(
                session_id=adapter_config.session_id,
                source_id=adapter_config.source_id,
                clock_domain=adapter_config.clock_domain,
                max_age_ns=self.thresholds.max_age_ns,
            )
            if marker_config is None
            else marker_config
        )
        if not isinstance(marker, MinimapMarkerConfig):
            raise TypeError("marker_config must be MinimapMarkerConfig")
        if marker.geometry != FULL_FRAME_GEOMETRY or marker.pixel_spec != FULL_FRAME_PIXEL_SPEC:
            raise VC003LiveMarkerValidationError(
                "marker config must use full-frame geometry and size"
            )
        if marker.calibration_sha256 != FULL_FRAME_CALIBRATION_SHA256:
            raise VC003LiveMarkerValidationError(
                "marker calibration does not match full-frame contract"
            )
        if marker.max_age_ns != self.thresholds.max_age_ns:
            raise VC003LiveMarkerValidationError("marker max_age_ns does not match thresholds")
        if marker.session_id is not None and marker.session_id != adapter_config.session_id:
            raise VC003LiveMarkerValidationError("marker session does not match adapter")
        if marker.source_id is not None and marker.source_id != adapter_config.source_id:
            raise VC003LiveMarkerValidationError("marker source does not match adapter")
        if marker.clock_domain is not None and marker.clock_domain != adapter_config.clock_domain:
            raise VC003LiveMarkerValidationError("marker clock does not match adapter")
        self.adapter_config = adapter_config
        self.marker_config = marker
        self.adapter = FrameSourceAdapter(source, adapter_config, clock=clock or _monotonic_ns)
        source_store = (
            pixel_store
            if pixel_store is not None
            else _obj_attr(source, "pixel_store", default=None)
        )
        if source_store is None and retained_store is not None:
            source_store = retained_store
        if source_store is None or not callable(getattr(source_store, "read", None)):
            raise TypeError("pixel_store or source.pixel_store must expose read(digest, spec)")
        self.source_store = cast(PixelStoreReader, source_store)
        self.retained_store = retained_store
        source_provenance = _obj_attr(source, "provenance", default=None)
        provenance_id = _obj_attr(source_provenance, "provenance_id", default=None)
        if provenance_id is None:
            provenance_id = _obj_attr(source_store, "provenance_id", default="vc003-live")
        self.source_provenance_id = _token(provenance_id, "source_provenance_id")
        self._retention_store = (
            retained_store
            if retained_store is not None
            else (
                cast(PixelStore, source_store)
                if callable(getattr(source_store, "put_artifact", None))
                else None
            )
        )
        self.memory_cas = CapacityOneMemoryCAS() if memory_cas is None else memory_cas
        self.read_only_store = ReadOnlyPixelStore(self.memory_cas)
        self.extractor = MinimapMarkerExtractor(marker, self.read_only_store, clock=clock)
        self.selector = selector
        self._selector_start = measurement_start_ns
        self.rows: list[VC003SelectedRow] = []
        self.private_rows: list[VC003RestrictedPrivateRow] = []
        self.results: list[MinimapMarkerResult | object] = []
        self.failures: list[VC003FailClosedSummary] = []

    def _now(self, supplied: int | None) -> int:
        if supplied is not None:
            return _ensure_time(supplied, "now_ns")
        if self.clock is not None:
            return _ensure_time(self.clock(), "now_ns")
        return _monotonic_ns()

    def _ensure_selector(self, admitted_at_ns: int) -> FixedBucketSelector:
        if self.selector is None:
            self.selector = FixedBucketSelector(
                start_at_ns=admitted_at_ns if self._selector_start is None else self._selector_start
            )
        return self.selector

    def _fail_closed(
        self, packet: object | None, code: str, actual_geometry: SourceGeometry | None = None
    ) -> VC003FailClosedSummary:
        actual_size = _obj_attr(packet, "source_size", "frame_size", default=None)
        if actual_size is not None and not isinstance(actual_size, FrameSize):
            actual_size = None
        actual_hash = None
        if actual_geometry is not None:
            actual_hash = canonical_geometry_sha256(actual_geometry)
        summary = VC003FailClosedSummary(
            code=code,
            session_id=_obj_attr(packet, "session_id", default=None),
            source_id=_obj_attr(packet, "source_id", default=None),
            frame_id=_obj_attr(packet, "frame_id", default=None),
            actual_size=actual_size,
            actual_geometry_sha256=actual_hash,
        )
        self.failures.append(summary)
        return summary

    def process_admission(
        self,
        admission: FrameAdmissionResult,
        *,
        checked_at_ns: int | None = None,
    ) -> VC003SelectedRow | VC003FailClosedSummary | None:
        if not isinstance(admission, FrameAdmissionResult):
            raise TypeError("admission must be FrameAdmissionResult")
        if not admission.accepted or admission.packet is None:
            return self._fail_closed(None, f"admission_{admission.status.value}")
        packet = admission.packet
        # This check intentionally occurs before selector/CAS/extractor work.
        if packet.source_geometry != FULL_FRAME_GEOMETRY:
            return self._fail_closed(packet, "geometry_mismatch", packet.source_geometry)
        if (
            packet.capture_health is None
            or packet.capture_health.max_age_ns != self.thresholds.max_age_ns
        ):
            return self._fail_closed(packet, "freshness_contract_mismatch", packet.source_geometry)
        if packet.image_metadata.get("pixel_spec") is not None:
            try:
                supplied_spec = PixelSpec.from_dict(
                    cast(Mapping[str, Any], packet.image_metadata["pixel_spec"])
                )
            except (TypeError, ValueError):
                return self._fail_closed(packet, "pixel_spec_mismatch", packet.source_geometry)
            if supplied_spec != FULL_FRAME_PIXEL_SPEC:
                return self._fail_closed(packet, "frame_size_changed", packet.source_geometry)
        admitted = packet.received_at_ns
        selector = self._ensure_selector(admitted)
        if not selector.consider(admission):
            return None
        source_store = self.source_store
        try:
            data = source_store.read(packet.content_hash, FULL_FRAME_PIXEL_SPEC)
            self.memory_cas.put(
                FULL_FRAME_PIXEL_SPEC,
                data,
                source_provenance_id=self.source_provenance_id,
                session_id=packet.session_id,
                source_sequence=packet.frame_id,
                expected_pixel_digest=packet.content_hash,
            )
        except Exception as exc:
            return self._fail_closed(
                packet, f"pixel_cas_{type(exc).__name__}", packet.source_geometry
            )
        now = self._now(checked_at_ns)
        if now < packet.received_at_ns:
            return self._fail_closed(packet, "timestamp_regression", packet.source_geometry)
        try:
            # The original accepted packet is passed directly.  No replacement
            # FramePacket is constructed at this boundary.
            result = self.extractor.extract(
                packet,
                now_ns=now,
                observed_at_ns=packet.received_at_ns,
                generation=GENERATION,
            )
            destination = self._retention_store
            artifact: PixelArtifact | MemoryCASArtifact
            if destination is None:
                artifact = MemoryCASArtifact(
                    pixel_digest=packet.content_hash,
                    spec=FULL_FRAME_PIXEL_SPEC,
                    byte_length=FULL_FRAME_PIXEL_SPEC.length,
                    source_provenance_id=self.source_provenance_id,
                    session_id=packet.session_id,
                    source_sequence=packet.frame_id,
                )
            else:
                artifact = self.memory_cas.retain_selected(
                    packet.content_hash,
                    destination,
                    source_provenance_id=self.source_provenance_id,
                    session_id=packet.session_id,
                    source_sequence=packet.frame_id,
                )
            selection = selector.selected[-1]
            row, private = _marker_result_row(
                selection,
                result,
                artifact,
                checked_at_ns=now,
                completed_at_ns=now,
                config=self.marker_config,
            )
        except Exception as exc:
            return self._fail_closed(packet, f"marker_{type(exc).__name__}", packet.source_geometry)
        self.results.append(result)
        self.rows.append(row)
        self.private_rows.append(private)
        return row

    def ingest(
        self,
        raw: RawFrame | None,
        received_at_ns: int | None = None,
    ) -> tuple[FrameAdmissionResult, VC003SelectedRow | VC003FailClosedSummary | None]:
        admission = self.adapter.ingest(raw, received_at_ns=received_at_ns)
        return admission, self.process_admission(admission, checked_at_ns=received_at_ns)

    admit = ingest

    def poll(
        self,
        *,
        now_ns: int | None = None,
    ) -> tuple[FrameAdmissionResult, VC003SelectedRow | VC003FailClosedSummary | None]:
        raw = self.source.read()
        admission = self.adapter.ingest(raw, received_at_ns=now_ns)
        return admission, self.process_admission(admission, checked_at_ns=now_ns)

    read = poll

    @property
    def accounting(self) -> VC003MarkerAccounting:
        return VC003MarkerAccounting.from_results(self.results)

    def validate(self, *, require_complete: bool = False) -> VC003LineageValidation:
        return validate_live_marker_lineage(
            self.selector if self.selector is not None else (),
            self.rows,
            self.results,
            retained_store=self._retention_store,
            expected_config_digest=self.marker_config.digest,
            require_complete=require_complete,
        )


LiveMarkerRunner = VC003LiveMarkerRunner
VC003LiveMarkerIntegration = VC003LiveMarkerRunner
LiveMarkerIntegration = VC003LiveMarkerRunner


def _monotonic_ns() -> int:
    from time import monotonic_ns

    return monotonic_ns()


__all__ = [
    "BUCKET_COUNT",
    "BUCKET_DURATION_NS",
    "BUCKET_SECONDS",
    "BUCKET_SPAN_NS",
    "DEFAULT_LIVE_MARKER_CONFIG",
    "DEFAULT_MARKER_CONFIG",
    "FAIL_CLOSED_SUMMARY",
    "FIXED_BUCKET_COUNT",
    "FULL_FRAME_CALIBRATION_SHA256",
    "FULL_FRAME_GEOMETRY",
    "FULL_FRAME_GEOMETRY_SHA256",
    "FULL_FRAME_HEIGHT",
    "FULL_FRAME_PIXEL_SPEC",
    "FULL_FRAME_WIDTH",
    "GENERATION",
    "MARKER_CONFIG_SEMANTIC_SHA256",
    "MAX_AGE_NS",
    "MEASUREMENT_DURATION_NS",
    "MEASUREMENT_SECONDS",
    "SCHEMA_VERSION",
    "VC003_LIVE_MARKER_CONFIG_VERSION",
    "VC003_LIVE_MARKER_SCHEMA_VERSION",
    "VC003_LIVE_MARKER_SCOPE",
    "VC003_LIVE_MARKER_TRUTH_SCOPE",
    "VC003_LIVE_MARKER_VERSION",
    "WARMUP_SECONDS",
    "CASReadOnly",
    "CapacityOneCAS",
    "CapacityOneMemoryCAS",
    "EphemeralMemoryCAS",
    "EventTape",
    "Fixed100BucketSelector",
    "FixedBucketSelector",
    "FrameAdmissionResult",
    "FrameAdmissionStatus",
    "FramePacket",
    "FrameSize",
    "FrameSourceAdapter",
    "FrameSourceConfig",
    "HashOnlySelectedRow",
    "InMemoryCapacityOneCAS",
    "LiveMarkerAccounting",
    "LiveMarkerIntegration",
    "LiveMarkerLineageValidator",
    "LiveMarkerRunner",
    "LiveMarkerThresholdConfig",
    "MarkerAccounting",
    "MemoryCASArtifact",
    "MinimapMarkerConfig",
    "MinimapMarkerExtractor",
    "MinimapMarkerResult",
    "PixelArtifact",
    "PixelSpec",
    "PixelStore",
    "PixelStoreReader",
    "PublicSelectedRow",
    "RawFrame",
    "ReadOnlyPixelStore",
    "ReadOnlyPixelStoreWrapper",
    "RestrictedPrivateRow",
    "RestrictedRow",
    "SelectedOccurrence",
    "SelectedRow",
    "SourceGeometry",
    "SourceRect",
    "ThreeSecondBucketSelector",
    "VC003BucketSelection",
    "VC003FailClosedSummary",
    "VC003LineageValidation",
    "VC003LiveMarkerConfig",
    "VC003LiveMarkerError",
    "VC003LiveMarkerGeneration",
    "VC003LiveMarkerIntegration",
    "VC003LiveMarkerRunner",
    "VC003LiveMarkerScope",
    "VC003LiveMarkerThresholdConfig",
    "VC003LiveMarkerThresholds",
    "VC003LiveMarkerValidationError",
    "VC003MarkerAccounting",
    "VC003PrivateRow",
    "VC003RestrictedPrivateRow",
    "VC003Source",
    "VC003SourceConfig",
    "build_frame_source_config",
    "default_minimap_marker_config",
    "full_frame_geometry",
    "full_frame_source_config",
    "make_frame_source_config",
    "validate_accounting",
    "validate_lineage",
    "validate_live_marker_lineage",
    "validate_marker_accounting",
    "validate_vc003_lineage",
]


# Compatibility aliases kept after __all__ so importers can discover the
# descriptive names without adding a second implementation.
VC003LiveMarkerConfig = VC003LiveMarkerThresholds
VC003LiveMarkerScope = VC003_LIVE_MARKER_SCOPE
VC003LiveMarkerGeneration = GENERATION
FAIL_CLOSED_SUMMARY = VC003FailClosedSummary
