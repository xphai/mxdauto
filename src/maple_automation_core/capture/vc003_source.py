"""Read-only VC-003 capture source with a bounded raw latest buffer.

This module is deliberately independent from OpenCV at import time.  The
``OpenCVCaptureBackend`` imports ``cv2`` only from :meth:`start`, while tests
can inject a small backend implementing :class:`CaptureBackend`.

The source owns the immutable raw bytes that are handed to the Core capture
boundary.  A successful backend sample is assigned one sequence, one SHA-256
digest, one :class:`RawFrameSpec`, and one :class:`VC003RawFrame`; those values
are constructed as a single object so a consumer cannot observe metadata from
one sample paired with bytes from another.  The producer has exactly one slot:
when that slot is full the old sample is superseded, never queued.

No input, window, network, reconnect, or backend fallback behavior belongs in
this adapter.
"""

from __future__ import annotations

import importlib
import tempfile
import threading
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from math import isclose, isfinite
from time import monotonic, monotonic_ns
from typing import Any, Protocol, TypeVar, cast

from maple_automation_core.capture.frame_source import RawFrame, canonical_calibration_sha256
from maple_automation_core.capture.pixel_store import (
    UNKNOWN_DEVICE_FINGERPRINT_SHA256,
    CaptureSourceProvenance,
    PixelSpec,
    PixelStore,
    canonical_json,
    pixel_digest,
    validate_pixels,
)
from maple_automation_core.domain.frame import FrameSize, SourceGeometry, SourceRect

_DEFAULT_SOURCE_ID = "capture-card-primary"
_DEFAULT_SESSION_ID = "vc003-session"
_DEFAULT_CLOCK_DOMAIN = "monotonic"
_DEFAULT_TRANSFORM_VERSION = "capture-v1"
_DEFAULT_DEVICE_NAME = "VC-003 Video"
_DEFAULT_WIDTH = 1920
_DEFAULT_HEIGHT = 1080
_DEFAULT_FPS = 30.0
_NEGOTIATED_FPS_ABS_TOLERANCE = 0.001
_SUPPORTED_BACKENDS = frozenset({"dshow", "msmf"})
RawBytes = bytes

# PixelStore is the authority for Pixel V1 layout and its domain-separated
# digest.  Keep the source-facing name so callers can use the raw-source
# vocabulary without creating a second pixel contract.
RawFrameSpec = PixelSpec
SampleT = TypeVar("SampleT")
_DEFAULT_RAW_SPEC = RawFrameSpec(width=1, height=1)


def _negotiated_fps_matches(actual: float, requested: float) -> bool:
    """Accept only sub-millihertz backend reporting noise around the requested rate."""

    return isclose(
        float(actual),
        float(requested),
        rel_tol=0.0,
        abs_tol=_NEGOTIATED_FPS_ABS_TOLERANCE,
    )


def _spec_from_value(value: RawFrameSpec | Mapping[str, Any] | None) -> RawFrameSpec:
    if value is None:
        return RawFrameSpec()
    if isinstance(value, RawFrameSpec):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("spec must be PixelSpec or a mapping.")
    try:
        for field_name, expected in (
            ("color_space", "BGR"),
            ("layout", "HWC"),
            ("version", "pixel-v1"),
        ):
            if field_name in value and value[field_name] != expected:
                raise ValueError(f"{field_name} must be exactly {expected!r}.")
        pixel_format = value.get("pixel_format", value.get("format", "BGR8"))
        if isinstance(pixel_format, str) and pixel_format.casefold() == "bgr8":
            pixel_format = "BGR8"
        stride = value.get("stride", value.get("row_stride"))
        length = value.get("length", value.get("byte_length"))
        return RawFrameSpec(
            width=value["width"],
            height=value["height"],
            channels=value.get("channels", 3),
            pixel_format=pixel_format,
            dtype=value.get("dtype", "uint8"),
            stride=stride,
            length=length,
        )
    except KeyError as exc:
        raise ValueError(f"spec missing key: {exc.args[0]}") from exc


def _ensure_positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _ensure_non_negative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


def _ensure_non_empty_str(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value


def _is_finite_number(value: object) -> bool:
    """Return whether a non-boolean numeric value is finite."""

    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        try:
            return isfinite(float(value))
        except OverflowError:
            return False
    return isinstance(value, float) and isfinite(value)


def _bounded_wait_seconds(value: object, name: str = "timeout") -> float:
    """Validate a timeout before passing it to a threading primitive.

    ``threading.Condition.wait`` and ``Event.wait`` raise ``OverflowError``
    when a finite value exceeds ``threading.TIMEOUT_MAX``.  Keep that platform
    detail at the API boundary so every wait receives a bounded float.
    """

    if not isinstance(value, int | float) or not _is_finite_number(value):
        raise ValueError(f"{name} must be a non-negative number.")
    seconds = float(value)
    if seconds < 0:
        raise ValueError(f"{name} must be a non-negative number.")
    if seconds > threading.TIMEOUT_MAX:
        raise ValueError(f"{name} must be <= threading.TIMEOUT_MAX.")
    return seconds


def _resolve_wait_timeout(
    timeout: object,
    *,
    timeout_ms: object | None = None,
    timeout_s: object | None = None,
    timeout_ns: object | None = None,
    deadline_ns: object | None = None,
) -> float:
    """Resolve one timeout spelling and enforce the threading upper bound."""

    specified = sum(value is not None for value in (timeout_ms, timeout_s, timeout_ns, deadline_ns))
    if specified > 1:
        raise ValueError("only one timeout/deadline argument may be supplied.")
    if timeout_ms is not None:
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int | float)
            or not _is_finite_number(timeout_ms)
        ):
            raise ValueError("timeout_ms must be a number.")
        timeout = float(timeout_ms) / 1000.0
        return _bounded_wait_seconds(timeout, "timeout_ms")
    if timeout_s is not None:
        return _bounded_wait_seconds(timeout_s, "timeout_s")
    if timeout_ns is not None:
        if (
            isinstance(timeout_ns, bool)
            or not isinstance(timeout_ns, int)
            or not _is_finite_number(timeout_ns)
        ):
            raise ValueError("timeout_ns must be an integer.")
        timeout = float(timeout_ns) / 1_000_000_000.0
        return _bounded_wait_seconds(timeout, "timeout_ns")
    if deadline_ns is not None:
        if (
            isinstance(deadline_ns, bool)
            or not isinstance(deadline_ns, int)
            or not _is_finite_number(deadline_ns)
        ):
            raise ValueError("deadline_ns must be an integer.")
        remaining_ns = deadline_ns - monotonic_ns()
        if remaining_ns <= 0:
            return 0.0
        timeout = float(remaining_ns) / 1_000_000_000.0
        return _bounded_wait_seconds(timeout, "deadline_ns")
    return _bounded_wait_seconds(timeout)


@dataclass(frozen=True, slots=True)
class BackendFrame:
    """Optional explicit backend payload used by fake and real backends."""

    data: Any
    spec: RawFrameSpec | Mapping[str, Any] | None = None
    captured_at_ns: int | None = None


@dataclass(frozen=True, slots=True)
class NegotiatedCaptureFacts:
    """Measured backend facts frozen immediately after a successful open."""

    width: int
    height: int
    fps: float
    fourcc: str
    backend: str
    backend_api: str
    backend_version: str

    def __post_init__(self) -> None:
        _ensure_positive_int(self.width, "width")
        _ensure_positive_int(self.height, "height")
        if (
            not isinstance(self.fps, int | float)
            or not _is_finite_number(self.fps)
            or self.fps <= 0
        ):
            raise ValueError("fps must be a positive number.")
        for value, name in (
            (self.fourcc, "fourcc"),
            (self.backend, "backend"),
            (self.backend_api, "backend_api"),
            (self.backend_version, "backend_version"),
        ):
            _ensure_non_empty_str(value, name)

    def to_format_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "fourcc": self.fourcc,
            "backend": self.backend,
            "channels": 3,
            "pixel_format": "BGR8",
            "dtype": "uint8",
            "stride": self.width * 3,
            "length": self.width * self.height * 3,
        }


@dataclass(frozen=True, slots=True)
class VC003RawFrame(RawFrame):
    """A Core ``RawFrame`` carrying its immutable raw bytes.

    The base contract remains usable by :class:`FrameSourceAdapter`, while
    this subtype exposes the bytes and layout needed by G1-FRM-001B1.
    """

    raw_bytes: bytes = b""
    spec: RawFrameSpec = _DEFAULT_RAW_SPEC

    def __post_init__(self) -> None:
        # Explicit base dispatch avoids the zero-argument ``super`` closure
        # issue that can occur with slotted dataclass inheritance.
        RawFrame.__post_init__(self)
        if not isinstance(self.raw_bytes, bytes):
            object.__setattr__(self, "raw_bytes", bytes(self.raw_bytes))
        if not isinstance(self.spec, RawFrameSpec):
            object.__setattr__(self, "spec", _spec_from_value(self.spec))
        if len(self.raw_bytes) != self.spec.byte_length:
            raise ValueError(
                "raw_bytes length must equal spec.byte_length "
                f"({len(self.raw_bytes)} != {self.spec.byte_length})."
            )
        expected_hash = pixel_digest(self.spec, self.raw_bytes)
        if self.content_hash != expected_hash:
            raise ValueError("content_hash must be the canonical Pixel V1 digest of raw_bytes.")
        if self.actual_source_size != FrameSize(width=self.spec.width, height=self.spec.height):
            raise ValueError("source_size must match spec dimensions.")

    @property
    def bytes(self) -> RawBytes:
        return self.raw_bytes

    @property
    def image_bytes(self) -> RawBytes:
        return self.raw_bytes

    @property
    def frame_bytes(self) -> RawBytes:
        return self.raw_bytes

    @property
    def sequence(self) -> int:
        return self.frame_id

    @property
    def hash(self) -> str:
        return self.content_hash

    @property
    def frame_spec(self) -> RawFrameSpec:
        return self.spec


# Descriptive aliases make the sample type discoverable for integrations that
# use "raw sample" terminology rather than the existing RawFrame contract.
RawCaptureFrame = VC003RawFrame
RawCaptureSample = VC003RawFrame
RawFrameSample = VC003RawFrame
VC003Sample = VC003RawFrame


class CaptureBackend(Protocol):
    """Minimal backend contract; implementations must be read-only."""

    @property
    def device_name(self) -> str:
        """Return the de-identified configured device name."""

    @property
    def negotiated_facts(self) -> NegotiatedCaptureFacts:
        """Return the currently measured capture properties."""

    @property
    def device_fingerprint_sha256(self) -> str:
        """Return a de-identified measured/controlled device fingerprint."""

    def start(self) -> None:
        """Open the selected capture device."""

    def read(self) -> Any | None:
        """Return a decoded frame, ``None`` when not ready, or raise ``EOFError``."""

    def stop(self) -> None:
        """Release the capture device and unblock a pending read."""


VC003Backend = CaptureBackend


class VC003SourceError(RuntimeError):
    """Fatal read/decode error surfaced by :meth:`VC003Source.read`."""


@dataclass(frozen=True, slots=True)
class VC003SourceConfig:
    """Fixed physical and logical settings for a VC-003 source."""

    source_id: str = _DEFAULT_SOURCE_ID
    session_id: str = _DEFAULT_SESSION_ID
    clock_domain: str = _DEFAULT_CLOCK_DOMAIN
    transform_version: str = _DEFAULT_TRANSFORM_VERSION
    device_name: str = _DEFAULT_DEVICE_NAME
    device_index: int = 0
    backend: str = "dshow"
    width: int = _DEFAULT_WIDTH
    height: int = _DEFAULT_HEIGHT
    fps: float = _DEFAULT_FPS
    pixel_format: str = "mjpg"
    source_geometry: SourceGeometry | None = None
    poll_interval_s: float = 0.001

    def __post_init__(self) -> None:
        _ensure_non_empty_str(self.source_id, "source_id")
        _ensure_non_empty_str(self.session_id, "session_id")
        _ensure_non_empty_str(self.clock_domain, "clock_domain")
        _ensure_non_empty_str(self.transform_version, "transform_version")
        _ensure_non_empty_str(self.device_name, "device_name")
        _ensure_non_negative_int(self.device_index, "device_index")
        if self.backend not in _SUPPORTED_BACKENDS:
            raise ValueError(
                f"backend must be one of {sorted(_SUPPORTED_BACKENDS)}, got {self.backend!r}."
            )
        _ensure_positive_int(self.width, "width")
        _ensure_positive_int(self.height, "height")
        if (
            not isinstance(self.fps, int | float)
            or not _is_finite_number(self.fps)
            or self.fps <= 0
        ):
            raise ValueError("fps must be a positive number.")
        _ensure_non_empty_str(self.pixel_format, "pixel_format")
        _bounded_wait_seconds(self.poll_interval_s, "poll_interval_s")
        if self.source_geometry is not None:
            if not isinstance(self.source_geometry, SourceGeometry):
                raise TypeError("source_geometry must be SourceGeometry.")
            expected = FrameSize(width=self.width, height=self.height)
            if self.source_geometry.source_size != expected:
                raise ValueError("source_geometry.source_size must match width and height.")

    @property
    def frame_spec(self) -> RawFrameSpec:
        return RawFrameSpec(width=self.width, height=self.height, pixel_format="BGR8")

    @property
    def geometry(self) -> SourceGeometry:
        if self.source_geometry is not None:
            return self.source_geometry
        return SourceGeometry(
            source_size=FrameSize(width=self.width, height=self.height),
            content_rect=SourceRect(x=0, y=0, width=self.width, height=self.height),
            working_size=FrameSize(width=self.width, height=self.height),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "session_id": self.session_id,
            "clock_domain": self.clock_domain,
            "transform_version": self.transform_version,
            "device_name": self.device_name,
            "device_index": self.device_index,
            "backend": self.backend,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "pixel_format": self.pixel_format,
            "source_geometry": (
                None if self.source_geometry is None else self.source_geometry.to_dict()
            ),
            "poll_interval_s": self.poll_interval_s,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VC003SourceConfig:
        if not isinstance(value, Mapping):
            raise TypeError("VC003SourceConfig payload must be a mapping.")
        geometry_value = value.get("source_geometry")
        geometry = (
            None
            if geometry_value is None
            else SourceGeometry.from_dict(cast(Mapping[str, Any], geometry_value))
        )
        return cls(
            source_id=value.get("source_id", _DEFAULT_SOURCE_ID),
            session_id=value.get("session_id", _DEFAULT_SESSION_ID),
            clock_domain=value.get("clock_domain", _DEFAULT_CLOCK_DOMAIN),
            transform_version=value.get("transform_version", _DEFAULT_TRANSFORM_VERSION),
            device_name=value.get("device_name", _DEFAULT_DEVICE_NAME),
            device_index=value.get("device_index", 0),
            backend=value.get("backend", "dshow"),
            width=value.get("width", _DEFAULT_WIDTH),
            height=value.get("height", _DEFAULT_HEIGHT),
            fps=value.get("fps", _DEFAULT_FPS),
            pixel_format=value.get("pixel_format", "mjpg"),
            source_geometry=geometry,
            poll_interval_s=value.get("poll_interval_s", 0.001),
        )


VC003Config = VC003SourceConfig
VC003CaptureConfig = VC003SourceConfig
VC003AdapterConfig = VC003SourceConfig


def _requested_format(config: VC003SourceConfig) -> dict[str, Any]:
    return {
        "width": config.width,
        "height": config.height,
        "fps": config.fps,
        "fourcc": config.pixel_format.upper(),
        "backend": config.backend,
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": config.width * 3,
        "length": config.width * config.height * 3,
    }


def _default_source_provenance(config: VC003SourceConfig) -> CaptureSourceProvenance:
    """Build a strict placeholder-bound provenance for offline/fake adapters.

    Hardware evidence injects a fully bound record with real source/tool/lock
    hashes.  The fallback still binds session/config/calibration and never
    invents an upstream queue depth or device timestamp.
    """

    format_record = _requested_format(config)
    unmeasured_format = {
        "width": 1,
        "height": 1,
        "fps": 1.0,
        "fourcc": "UNMEASURED",
        "backend": config.backend,
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": 3,
        "length": 3,
    }
    return CaptureSourceProvenance(
        source_id=config.source_id,
        session_id=config.session_id,
        requested=format_record,
        negotiated=unmeasured_format,
        backend=config.backend,
        backend_version="unbound-offline",
        timestamp_origin="host_monotonic_post_retrieve",
        upstream_queue="unknown",
        config_sha256=sha256(canonical_json(config.to_dict())).hexdigest(),
        calibration_sha256=canonical_calibration_sha256(
            config.geometry,
            config.transform_version,
        ),
    )


@dataclass(frozen=True, slots=True)
class RawLatestStatus:
    """Atomic accounting snapshot for one raw latest counter epoch."""

    epoch: int
    session_id: str | None
    produced: int
    delivered: int
    superseded: int
    pending: int
    in_flight: int
    discarded_on_reset: int
    discarded_on_error: int
    max_depth: int
    consumer_bound: bool
    producer_bound: bool = False
    last_produced_sequence: int | None = None
    last_delivered_sequence: int | None = None

    @property
    def accounted(self) -> int:
        return (
            self.delivered
            + self.superseded
            + self.pending
            + self.in_flight
            + self.discarded_on_reset
            + self.discarded_on_error
        )

    @property
    def accounting_holds(self) -> bool:
        return self.produced == self.accounted

    @property
    def max_queue_depth(self) -> int:
        return self.max_depth

    @property
    def dropped(self) -> int:
        return self.superseded

    @property
    def counter_epoch(self) -> int:
        return self.epoch

    @property
    def produced_count(self) -> int:
        return self.produced

    @property
    def delivered_count(self) -> int:
        return self.delivered

    @property
    def superseded_count(self) -> int:
        return self.superseded

    @property
    def pending_count(self) -> int:
        return self.pending

    @property
    def discarded_on_reset_count(self) -> int:
        return self.discarded_on_reset

    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "session_id": self.session_id,
            "produced": self.produced,
            "delivered": self.delivered,
            "superseded": self.superseded,
            "pending": self.pending,
            "in_flight": self.in_flight,
            "discarded_on_reset": self.discarded_on_reset,
            "discarded_on_error": self.discarded_on_error,
            "max_depth": self.max_depth,
            "consumer_bound": self.consumer_bound,
            "producer_bound": self.producer_bound,
            "last_produced_sequence": self.last_produced_sequence,
            "last_delivered_sequence": self.last_delivered_sequence,
            "accounted": self.accounted,
            "accounting_holds": self.accounting_holds,
        }


class RawLatestSlot[SampleT]:
    """Synchronous capacity-one slot for Core-owned raw samples.

    The slot is intentionally independent from a capture backend.  It is
    useful for deterministic pressure testing and is the exact slot used by
    :class:`VC003Source`.  ``take`` binds the first caller as the sole logical
    consumer; a second consumer receives a stable ``RuntimeError``.
    """

    def __init__(self, session_id: str | None = None) -> None:
        if session_id is not None:
            _ensure_non_empty_str(session_id, "session_id")
        self._condition = threading.Condition(threading.RLock())
        self._pending: SampleT | None = None
        self._reserved: SampleT | None = None
        self._reserved_for_controller = False
        self._epoch = 0
        self._session_id = session_id
        self._produced = 0
        self._delivered = 0
        self._superseded = 0
        self._discarded_on_reset = 0
        self._discarded_on_error = 0
        self._max_depth = 0
        self._consumer_thread_id: int | None = None
        self._producer_thread_id: int | None = None
        self._last_reset_status: RawLatestStatus | None = None
        self._closed = False
        self._requires_reset = False
        self._last_produced_sequence: int | None = None
        self._last_delivered_sequence: int | None = None

    def _snapshot_locked(self) -> RawLatestStatus:
        return RawLatestStatus(
            epoch=self._epoch,
            session_id=self._session_id,
            produced=self._produced,
            delivered=self._delivered,
            superseded=self._superseded,
            pending=0 if self._pending is None else 1,
            in_flight=0 if self._reserved is None else 1,
            discarded_on_reset=self._discarded_on_reset,
            discarded_on_error=self._discarded_on_error,
            max_depth=self._max_depth,
            consumer_bound=self._consumer_thread_id is not None,
            producer_bound=self._producer_thread_id is not None,
            last_produced_sequence=self._last_produced_sequence,
            last_delivered_sequence=self._last_delivered_sequence,
        )

    def status(self) -> RawLatestStatus:
        with self._condition:
            return self._snapshot_locked()

    snapshot = status

    @property
    def last_reset_status(self) -> RawLatestStatus | None:
        with self._condition:
            return self._last_reset_status

    @property
    def pending(self) -> SampleT | None:
        with self._condition:
            return self._pending

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def start(self, session_id: str | None = None) -> None:
        """Open the slot for a source restart without changing its epoch."""

        with self._condition:
            if self._requires_reset:
                raise RuntimeError("raw latest slot requires reset before restart.")
            if session_id is not None:
                _ensure_non_empty_str(session_id, "session_id")
                if self._session_id is not None and self._session_id != session_id:
                    raise RuntimeError("raw latest slot session change requires reset.")
                self._session_id = session_id
            self._closed = False
            self._condition.notify_all()

    def _bind_producer_locked(self) -> None:
        current = threading.get_ident()
        if self._producer_thread_id is None:
            self._producer_thread_id = current
        elif self._producer_thread_id != current:
            raise RuntimeError("raw latest slot supports one producer thread.")

    def publish(self, sample: SampleT) -> RawLatestStatus:
        if sample is None:
            raise ValueError("sample must not be None.")
        with self._condition:
            if self._closed:
                raise RuntimeError("raw latest slot is stopped.")
            sample_session = getattr(sample, "session_id", None)
            if self._session_id is not None and sample_session != self._session_id:
                raise RuntimeError("raw latest slot rejected an old or mismatched session sample.")
            self._bind_producer_locked()
            self._produced += 1
            if self._pending is not None:
                self._superseded += 1
            self._pending = sample
            sequence = getattr(sample, "sequence", None)
            self._last_produced_sequence = sequence if isinstance(sequence, int) else None
            self._max_depth = max(self._max_depth, 1)
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
            return snapshot

    put = publish

    def _bind_consumer_locked(self) -> None:
        current = threading.get_ident()
        if self._consumer_thread_id is None:
            self._consumer_thread_id = current
        elif self._consumer_thread_id != current:
            raise RuntimeError("raw latest slot supports one consumer thread.")

    def take(
        self,
        timeout: float = 0.0,
        *,
        timeout_ms: float | None = None,
        timeout_s: float | None = None,
        timeout_ns: int | None = None,
        deadline_ns: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SampleT | None:
        timeout = _resolve_wait_timeout(
            timeout,
            timeout_ms=timeout_ms,
            timeout_s=timeout_s,
            timeout_ns=timeout_ns,
            deadline_ns=deadline_ns,
        )
        if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
            raise TypeError("cancel_event must provide is_set().")
        sample = self.reserve(
            timeout,
            cancel_event=cancel_event,
        )
        if sample is not None:
            self.commit_reserved(sample)
        return sample

    def reserve(
        self,
        timeout: float = 0.0,
        *,
        timeout_ms: float | None = None,
        timeout_s: float | None = None,
        timeout_ns: int | None = None,
        deadline_ns: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SampleT | None:
        """Reserve one sample without counting it delivered until explicit commit."""

        timeout = _resolve_wait_timeout(
            timeout,
            timeout_ms=timeout_ms,
            timeout_s=timeout_s,
            timeout_ns=timeout_ns,
            deadline_ns=deadline_ns,
        )
        if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
            raise TypeError("cancel_event must provide is_set().")
        with self._condition:
            self._bind_consumer_locked()
            if self._reserved is not None:
                raise RuntimeError("raw latest slot already has a consumer reservation.")
            deadline = monotonic() + float(timeout)
            while self._pending is None and not self._closed:
                if cancel_event is not None and cancel_event.is_set():
                    return None
                if not timeout:
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(remaining, 0.050))
            sample = self._pending
            if sample is None:
                return None
            self._pending = None
            self._reserved = sample
            self._reserved_for_controller = False
            self._condition.notify_all()
            return sample

    def _reserve_for_final_drain(self) -> SampleT | None:
        """Controller reservation after producer quiescence; does not rebind consumer."""

        with self._condition:
            if not self._closed:
                raise RuntimeError("controller final drain requires a stopped raw latest slot.")
            if self._reserved is not None:
                raise RuntimeError("raw latest slot already has a consumer reservation.")
            sample = self._pending
            if sample is None:
                return None
            self._pending = None
            self._reserved = sample
            self._reserved_for_controller = True
            self._condition.notify_all()
            return sample

    def commit_reserved(self, sample: SampleT) -> RawLatestStatus:
        with self._condition:
            self._bind_consumer_locked()
            if self._reserved_for_controller:
                raise RuntimeError("controller reservation requires the internal final-drain path.")
            if self._reserved is not sample:
                raise RuntimeError("raw latest slot reservation identity mismatch.")
            self._reserved = None
            self._reserved_for_controller = False
            self._delivered += 1
            sequence = getattr(sample, "sequence", None)
            self._last_delivered_sequence = sequence if isinstance(sequence, int) else None
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
            return snapshot

    def discard_reserved_on_error(self, sample: SampleT) -> RawLatestStatus:
        with self._condition:
            self._bind_consumer_locked()
            if self._reserved_for_controller:
                raise RuntimeError("controller reservation requires the internal final-drain path.")
            if self._reserved is not sample:
                raise RuntimeError("raw latest slot reservation identity mismatch.")
            self._reserved = None
            self._reserved_for_controller = False
            self._discarded_on_error += 1
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
            return snapshot

    def discard_reserved_on_reset(self, sample: SampleT) -> RawLatestStatus:
        """Discard a consumer reservation while sealing the current epoch."""

        with self._condition:
            self._bind_consumer_locked()
            if self._reserved_for_controller:
                raise RuntimeError("controller reservation requires the internal final-drain path.")
            if self._reserved is not sample:
                raise RuntimeError("raw latest slot reservation identity mismatch.")
            self._reserved = None
            self._reserved_for_controller = False
            self._discarded_on_reset += 1
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
            return snapshot

    def _commit_final_drain(self, sample: SampleT) -> RawLatestStatus:
        with self._condition:
            if self._reserved is not sample or not self._reserved_for_controller:
                raise RuntimeError("final-drain reservation identity mismatch.")
            self._reserved = None
            self._reserved_for_controller = False
            self._delivered += 1
            sequence = getattr(sample, "sequence", None)
            self._last_delivered_sequence = sequence if isinstance(sequence, int) else None
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
            return snapshot

    def _discard_final_drain_on_error(self, sample: SampleT) -> RawLatestStatus:
        with self._condition:
            if self._reserved is not sample or not self._reserved_for_controller:
                raise RuntimeError("final-drain reservation identity mismatch.")
            self._reserved = None
            self._reserved_for_controller = False
            self._discarded_on_error += 1
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
            return snapshot

    read = take
    consume = take

    def stop(self) -> RawLatestStatus:
        """Close publication while leaving one pending sample for final drain."""

        with self._condition:
            self._closed = True
            self._requires_reset = True
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
            return snapshot

    def reset(self, new_session_id: str | None = None) -> RawLatestStatus:
        """Seal the active epoch and start a zeroed epoch."""

        with self._condition:
            if new_session_id is not None:
                _ensure_non_empty_str(new_session_id, "new_session_id")
                if self._session_id is not None and new_session_id == self._session_id:
                    raise ValueError("new_session_id must differ from the active session_id.")
            elif self._session_id is not None:
                raise ValueError("a session-bound raw latest slot reset requires new_session_id.")
            if self._pending is not None:
                self._discarded_on_reset += 1
                self._pending = None
            if self._reserved is not None:
                self._discarded_on_reset += 1
                self._reserved = None
            self._reserved_for_controller = False
            sealed = self._snapshot_locked()
            self._last_reset_status = sealed
            self._epoch += 1
            self._session_id = new_session_id
            self._produced = 0
            self._delivered = 0
            self._superseded = 0
            self._discarded_on_reset = 0
            self._discarded_on_error = 0
            self._max_depth = 0
            self._closed = False
            self._requires_reset = False
            self._last_produced_sequence = None
            self._last_delivered_sequence = None
            self._producer_thread_id = None
            self._condition.notify_all()
            return sealed


RawLatestMetrics = RawLatestStatus
RawLatestSlotStatus = RawLatestStatus


@dataclass(frozen=True, slots=True)
class VC003Status:
    """Immutable source status snapshot and accounting evidence."""

    epoch: int
    lifecycle: str
    session_id: str
    produced: int
    delivered: int
    superseded: int
    pending: int
    in_flight: int
    discarded_on_reset: int
    discarded_on_error: int
    max_depth: int
    next_sequence: int
    last_sequence: int | None
    last_delivered_sequence: int | None
    last_content_hash: str | None
    backend: str
    device_name: str
    error: str | None
    thread_alive: bool
    start_thread_alive: bool
    backend_stop_thread_alive: bool
    drain_thread_alive: bool
    consumer_bound: bool
    final_drain_performed: bool
    final_drain_sequence: int | None
    read_attempts: int
    no_frame_count: int
    read_failure_count: int
    decode_rejection_count: int
    eof_count: int
    reconnect_count: int
    backend_fallback_count: int

    @property
    def accounted(self) -> int:
        return (
            self.delivered
            + self.superseded
            + self.pending
            + self.in_flight
            + self.discarded_on_reset
            + self.discarded_on_error
        )

    @property
    def residual_worker_count(self) -> int:
        return sum(
            (
                self.thread_alive,
                self.start_thread_alive,
                self.backend_stop_thread_alive,
                self.drain_thread_alive,
            )
        )

    @property
    def accounting_holds(self) -> bool:
        return self.produced == self.accounted

    @property
    def max_queue_depth(self) -> int:
        return self.max_depth

    @property
    def dropped(self) -> int:
        return self.superseded

    @property
    def counter_epoch(self) -> int:
        return self.epoch

    @property
    def produced_count(self) -> int:
        return self.produced

    @property
    def delivered_count(self) -> int:
        return self.delivered

    @property
    def superseded_count(self) -> int:
        return self.superseded

    @property
    def pending_count(self) -> int:
        return self.pending

    @property
    def discarded_on_reset_count(self) -> int:
        return self.discarded_on_reset

    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "lifecycle": self.lifecycle,
            "session_id": self.session_id,
            "produced": self.produced,
            "delivered": self.delivered,
            "superseded": self.superseded,
            "pending": self.pending,
            "in_flight": self.in_flight,
            "discarded_on_reset": self.discarded_on_reset,
            "discarded_on_error": self.discarded_on_error,
            "max_depth": self.max_depth,
            "next_sequence": self.next_sequence,
            "last_sequence": self.last_sequence,
            "last_delivered_sequence": self.last_delivered_sequence,
            "last_content_hash": self.last_content_hash,
            "backend": self.backend,
            "device_name": self.device_name,
            "error": self.error,
            "thread_alive": self.thread_alive,
            "start_thread_alive": self.start_thread_alive,
            "backend_stop_thread_alive": self.backend_stop_thread_alive,
            "drain_thread_alive": self.drain_thread_alive,
            "residual_worker_count": self.residual_worker_count,
            "consumer_bound": self.consumer_bound,
            "final_drain_performed": self.final_drain_performed,
            "final_drain_sequence": self.final_drain_sequence,
            "read_attempts": self.read_attempts,
            "no_frame_count": self.no_frame_count,
            "read_failure_count": self.read_failure_count,
            "decode_rejection_count": self.decode_rejection_count,
            "eof_count": self.eof_count,
            "reconnect_count": self.reconnect_count,
            "backend_fallback_count": self.backend_fallback_count,
            "accounted": self.accounted,
            "accounting_holds": self.accounting_holds,
        }


BackendFactory = Callable[[VC003SourceConfig], CaptureBackend]
Clock = Callable[[], int]


class OpenCVCaptureBackend:
    """Lazy OpenCV backend for one explicitly selected API.

    The backend never probes another API after an open failure.  The source
    therefore has no silent backend fallback.
    """

    def __init__(self, config: VC003SourceConfig) -> None:
        self.config = config
        self._capture: Any | None = None
        self._cv2: Any | None = None
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._negotiated_facts: NegotiatedCaptureFacts | None = None

    @property
    def device_name(self) -> str:
        return self.config.device_name

    @property
    def device_fingerprint_sha256(self) -> str:
        # Generic OpenCV does not expose a stable hardware identifier.  B2
        # therefore injects a controlled backend wrapper whose measured
        # fingerprint matches external provenance; this placeholder can only
        # satisfy the explicitly offline/default profile.
        return UNKNOWN_DEVICE_FINGERPRINT_SHA256

    @property
    def backend_name(self) -> str:
        return self.config.backend

    @property
    def negotiated_facts(self) -> NegotiatedCaptureFacts | None:
        if self._capture is not None and not self._stopped:
            return self._measure_negotiated_facts()
        return self._negotiated_facts

    def _measure_negotiated_facts(self) -> NegotiatedCaptureFacts:
        capture = self._capture
        cv2 = self._cv2
        if capture is None or cv2 is None:
            raise RuntimeError("OpenCV backend is not open.")
        getter = getattr(capture, "get", None)
        if not callable(getter):
            raise RuntimeError("OpenCV backend does not expose negotiated capture properties.")
        frame_width = getattr(cv2, "CAP_PROP_FRAME_WIDTH", 3)
        frame_height = getattr(cv2, "CAP_PROP_FRAME_HEIGHT", 4)
        frame_fps = getattr(cv2, "CAP_PROP_FPS", 5)
        fourcc_property = getattr(cv2, "CAP_PROP_FOURCC", 6)
        actual_width = getter(frame_width)
        actual_height = getter(frame_height)
        actual_fps = getter(frame_fps)
        actual_fourcc_value = getter(fourcc_property)
        numeric = (actual_width, actual_height, actual_fps, actual_fourcc_value)
        if any(
            not isinstance(value, int | float) or not _is_finite_number(value) or value <= 0
            for value in numeric
        ):
            raise RuntimeError("OpenCV backend returned incomplete negotiated capture properties.")
        for value, name in (
            (actual_width, "width"),
            (actual_height, "height"),
            (actual_fourcc_value, "fourcc"),
        ):
            if not float(cast(int | float, value)).is_integer():
                raise RuntimeError(f"OpenCV backend returned a non-integral negotiated {name}.")
        fourcc_code = int(actual_fourcc_value)
        actual_fourcc = (
            "".join(chr((fourcc_code >> (8 * index)) & 0xFF) for index in range(4))
            .rstrip("\x00 ")
            .upper()
        )
        backend_name_fn = getattr(capture, "getBackendName", None)
        if not callable(backend_name_fn):
            raise RuntimeError("OpenCV backend does not report a negotiated backend API.")
        try:
            reported_backend_api = backend_name_fn()
        except Exception as exc:
            raise RuntimeError("OpenCV backend failed to report a negotiated backend API.") from exc
        if not isinstance(reported_backend_api, str) or not reported_backend_api.strip():
            raise RuntimeError("OpenCV backend reported an empty negotiated backend API.")
        backend_api = reported_backend_api.strip().lower()
        return NegotiatedCaptureFacts(
            width=int(actual_width),
            height=int(actual_height),
            fps=float(actual_fps),
            fourcc=actual_fourcc,
            backend=self.config.backend,
            backend_api=backend_api,
            backend_version=str(getattr(cv2, "__version__", "unknown")),
        )

    def start(self) -> None:
        if self._capture is not None:
            return
        cv2 = importlib.import_module("cv2")
        self._cv2 = cv2
        backend_api = {
            "dshow": getattr(cv2, "CAP_DSHOW", 700),
            "msmf": getattr(cv2, "CAP_MSMF", 1400),
            "any": getattr(cv2, "CAP_ANY", 0),
        }[self.config.backend]
        capture = cv2.VideoCapture(self.config.device_index, backend_api)
        if capture is None or not bool(capture.isOpened()):
            if capture is not None:
                release = getattr(capture, "release", None)
                if callable(release):
                    release()
            raise RuntimeError(
                f"VC-003 open failed: device={self.config.device_name!r}, "
                f"index={self.config.device_index}, backend={self.config.backend!r}."
            )
        self._capture = capture
        self._stopped = False

        # MJPG is a device-side request; OpenCV exposes decoded BGR8 frames.
        # DirectShow may reset FourCC when dimensions or FPS change, so apply
        # the compression request last and then freeze the measured contract.
        frame_width = getattr(cv2, "CAP_PROP_FRAME_WIDTH", 3)
        frame_height = getattr(cv2, "CAP_PROP_FRAME_HEIGHT", 4)
        frame_fps = getattr(cv2, "CAP_PROP_FPS", 5)
        capture.set(frame_width, self.config.width)
        capture.set(frame_height, self.config.height)
        capture.set(frame_fps, float(self.config.fps))
        if self.config.pixel_format.lower() in {"mjpg", "mjpeg"}:
            fourcc_fn = getattr(cv2, "VideoWriter_fourcc", None)
            if callable(fourcc_fn):
                capture.set(getattr(cv2, "CAP_PROP_FOURCC", 6), fourcc_fn(*"MJPG"))

        # Freeze actual negotiated properties.  A missing/zero value is not
        # evidence and therefore fails the strict hardware path.
        try:
            measured = self._measure_negotiated_facts()
        except Exception:
            self.stop()
            raise
        if measured.width != self.config.width or measured.height != self.config.height:
            self.stop()
            raise RuntimeError(
                "VC-003 negotiated dimensions "
                f"{measured.width!r}x{measured.height!r}, expected "
                f"{self.config.width}x{self.config.height}."
            )
        if not _negotiated_fps_matches(measured.fps, float(self.config.fps)):
            self.stop()
            raise RuntimeError(
                f"VC-003 negotiated fps {measured.fps!r}, expected {self.config.fps!r}."
            )
        expected_fourcc = (
            "MJPG"
            if self.config.pixel_format.lower() in {"mjpg", "mjpeg"}
            else (self.config.pixel_format.upper())
        )
        if measured.fourcc != expected_fourcc:
            self.stop()
            raise RuntimeError(
                f"VC-003 negotiated FourCC {measured.fourcc!r}, expected {expected_fourcc!r}."
            )
        if measured.backend_api != self.config.backend:
            self.stop()
            raise RuntimeError(
                f"VC-003 opened backend API {measured.backend_api!r}, "
                f"expected {self.config.backend!r}."
            )
        self._negotiated_facts = measured

    def read(self) -> Any | None:
        capture = self._capture
        if capture is None or self._stopped:
            return None
        result = capture.read()
        if not isinstance(result, tuple) or len(result) != 2:
            raise RuntimeError("OpenCV backend returned an invalid read result.")
        ok, frame = result
        if not bool(ok) or frame is None:
            raise RuntimeError("OpenCV backend failed to retrieve a decoded frame.")
        return frame

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            capture = self._capture
            self._capture = None
            if capture is not None:
                release = getattr(capture, "release", None)
                if callable(release):
                    release()


OpenCVBackend = OpenCVCaptureBackend


def make_opencv_backend(config: VC003SourceConfig) -> OpenCVCaptureBackend:
    """Return an OpenCV backend without importing ``cv2`` yet."""

    return OpenCVCaptureBackend(config)


create_opencv_backend = make_opencv_backend


def _payload_parts(payload: Any) -> tuple[Any, RawFrameSpec | Mapping[str, Any] | None, int | None]:
    if isinstance(payload, BackendFrame):
        return payload.data, payload.spec, payload.captured_at_ns
    if isinstance(payload, VC003RawFrame):
        return payload.raw_bytes, payload.spec, payload.captured_at_ns
    if isinstance(payload, Mapping):
        data_key = next(
            (
                key
                for key in ("raw_bytes", "image_bytes", "bytes", "data", "frame")
                if key in payload
            ),
            None,
        )
        if data_key is None:
            raise ValueError("backend mapping must contain frame bytes/data.")
        return payload[data_key], payload.get("spec"), payload.get("captured_at_ns")
    if isinstance(payload, tuple) and len(payload) in {2, 3}:
        data = payload[0]
        second = payload[1]
        if isinstance(second, RawFrameSpec | Mapping):
            timestamp = payload[2] if len(payload) == 3 else None
            return data, second, timestamp
        if isinstance(second, int) and len(payload) == 2:
            return data, None, second
    return payload, None, None


def _coerce_bytes_and_spec(
    data: Any,
    config: VC003SourceConfig,
    explicit_spec: RawFrameSpec | Mapping[str, Any] | None,
) -> tuple[bytes, RawFrameSpec]:
    spec: RawFrameSpec | None = None
    if explicit_spec is not None:
        spec = _spec_from_value(explicit_spec)

    if isinstance(data, bytes):
        raw_bytes = data
    elif isinstance(data, bytearray | memoryview):
        raw_bytes = bytes(data)
    else:
        shape_value = getattr(data, "shape", None)
        dtype_value = getattr(data, "dtype", None)
        if shape_value is not None and hasattr(data, "tobytes"):
            shape = tuple(int(value) for value in shape_value)
            if len(shape) == 2:
                height, width = shape
                channels = 1
            elif len(shape) == 3:
                height, width, channels = shape
            else:
                raise ValueError("decoded frame must be a 2-D or 3-D image.")
            if spec is None:
                spec = RawFrameSpec(
                    width=width,
                    height=height,
                    channels=channels,
                    pixel_format="BGR8",
                    dtype=str(dtype_value or "uint8"),
                )
            raw_bytes = bytes(data.tobytes(order="C"))
        elif hasattr(data, "tobytes"):
            raw_bytes = bytes(data.tobytes())
        else:
            raise TypeError("backend frame must expose bytes or tobytes().")

    if spec is None:
        spec = config.frame_spec
    if spec.width != config.width or spec.height != config.height:
        raise ValueError(
            "decoded frame dimensions do not match configured VC-003 dimensions "
            f"({spec.width}x{spec.height} != {config.width}x{config.height})."
        )
    if spec.dtype != "uint8":
        raise ValueError(f"Core raw bytes require uint8 data, got {spec.dtype!r}.")
    try:
        canonical_bytes = validate_pixels(spec, raw_bytes)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"decoded frame does not match Pixel V1 spec: {exc}") from exc
    return canonical_bytes, spec


class VC003Source:
    """Single-consumer, capacity-one VC-003 raw latest source.

    ``start`` is the only method that opens a backend.  ``read`` is
    non-blocking by default and returns the latest pending ``VC003RawFrame``;
    an optional timeout is useful for a consumer loop.  ``reset`` flushes the
    slot before binding subsequent frames to a different session ID.
    """

    def __init__(
        self,
        config: VC003SourceConfig | None = None,
        *,
        backend_factory: BackendFactory | None = None,
        backend: CaptureBackend | None = None,
        factory: BackendFactory | None = None,
        raw_latest_slot: RawLatestSlot[VC003RawFrame] | None = None,
        pixel_store: PixelStore | None = None,
        provenance: CaptureSourceProvenance | None = None,
        clock: Clock = monotonic_ns,
    ) -> None:
        if backend is not None and (backend_factory is not None or factory is not None):
            raise ValueError("provide backend or backend_factory, not both.")
        if backend_factory is not None and factory is not None:
            raise ValueError("provide backend_factory or factory, not both.")
        actual_factory = backend_factory if backend_factory is not None else factory
        if not callable(clock):
            raise TypeError("clock must be callable.")
        self.config = VC003SourceConfig() if config is None else config
        if not isinstance(self.config, VC003SourceConfig):
            raise TypeError("config must be VC003SourceConfig.")
        if backend is not None:
            self._backend_factory: BackendFactory = lambda _config: backend
        elif actual_factory is None:
            self._backend_factory = cast(BackendFactory, make_opencv_backend)
        else:
            self._backend_factory = actual_factory
        self._singleton_backend = backend is not None
        self._clock = clock
        self._temporary_cas: tempfile.TemporaryDirectory[str] | None = None
        if pixel_store is None:
            # Fake/offline integrations still receive the same verified-CAS
            # guarantee as a production source.  Hardware runs inject their
            # restricted persistent store explicitly; the default remains
            # private and is removed with the source object.
            self._temporary_cas = tempfile.TemporaryDirectory(prefix="maple-vc003-private-cas-")
            self._pixel_store = PixelStore(self._temporary_cas.name)
        elif isinstance(pixel_store, PixelStore):
            self._pixel_store = pixel_store
        else:
            raise TypeError("pixel_store must be PixelStore.")
        expected_config_hash = sha256(canonical_json(self.config.to_dict())).hexdigest()
        expected_calibration_hash = canonical_calibration_sha256(
            self.config.geometry,
            self.config.transform_version,
        )
        actual_provenance = (
            _default_source_provenance(self.config) if provenance is None else provenance
        )
        if not isinstance(actual_provenance, CaptureSourceProvenance):
            raise TypeError("provenance must be CaptureSourceProvenance.")
        if (
            actual_provenance.source_id != self.config.source_id
            or actual_provenance.session_id != self.config.session_id
            or actual_provenance.backend != self.config.backend
            or actual_provenance.config_sha256 != expected_config_hash
            or actual_provenance.calibration_sha256 != expected_calibration_hash
            or dict(actual_provenance.requested) != _requested_format(self.config)
        ):
            raise ValueError("provenance must bind the exact source session/config/calibration.")
        self._provenance = actual_provenance
        self._provenance_external = provenance is not None
        self._condition = threading.Condition(threading.RLock())
        self._consumer_transaction_lock = threading.Lock()
        self._backend_stop_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._stop_controller_lock = threading.Lock()
        self._slot = (
            RawLatestSlot(session_id=self.config.session_id)
            if raw_latest_slot is None
            else raw_latest_slot
        )
        self._backend: CaptureBackend | None = None
        self._used_backend_refs: list[weakref.ReferenceType[CaptureBackend]] = []
        self._backend_stopped = True
        self._thread: threading.Thread | None = None
        self._start_thread: threading.Thread | None = None
        self._backend_stop_thread: threading.Thread | None = None
        self._drain_thread: threading.Thread | None = None
        self._drain_result: VC003RawFrame | None = None
        self._drain_error: BaseException | None = None
        self._backend_stop_failures: list[BaseException] = []
        self._start_done = threading.Event()
        self._start_error: BaseException | None = None
        self._attempt_token = 0
        self._stop_event = threading.Event()
        self._lifecycle = "created"
        self._error: str | None = None
        # A producer can discover a fatal read/decode error before its
        # ``finally`` cleanup has returned.  Keep the root cause and stop
        # signal immediately, but expose the terminal lifecycle only after
        # the producer thread has actually exited (see ``_snapshot_locked``).
        self._terminal_error_pending = False
        # Reset invalidates a consumer transaction before waiting for its CAS
        # work to quiesce, so old-session data is accounted in the sealed epoch.
        self._reset_pending = False
        self._session_id = self.config.session_id
        self._generation = 0
        self._next_sequence = 1
        self._last_captured_at_ns: int | None = None
        self._last_sequence: int | None = None
        self._last_delivered_sequence: int | None = None
        self._last_content_hash: str | None = None
        self._negotiated_facts: NegotiatedCaptureFacts | None = None
        self._read_attempts = 0
        self._no_frame_count = 0
        self._read_failure_count = 0
        self._decode_rejection_count = 0
        self._eof_count = 0
        # Reconnect and backend fallback are intentionally absent.  Explicit
        # zero counters make that negative evidence machine-readable.
        self._reconnect_count = 0
        self._backend_fallback_count = 0
        self._final_drain_performed = False
        self._final_drain_sequence: int | None = None

    @property
    def session_id(self) -> str:
        with self._condition:
            return self._session_id

    @property
    def is_running(self) -> bool:
        with self._condition:
            return self._lifecycle == "running" and bool(
                self._thread is not None and self._thread.is_alive()
            )

    @property
    def max_depth(self) -> int:
        return self._slot.status().max_depth

    @property
    def thread(self) -> threading.Thread | None:
        with self._condition:
            return self._thread

    @property
    def raw_latest_slot(self) -> RawLatestSlot[VC003RawFrame]:
        return self._slot

    @property
    def pixel_store(self) -> PixelStore:
        """Return the store that must resolve every frame delivered by ``read``."""

        return self._pixel_store

    @property
    def provenance(self) -> CaptureSourceProvenance:
        return self._provenance

    @property
    def negotiated_facts(self) -> NegotiatedCaptureFacts | None:
        return self._negotiated_facts

    @property
    def raw_slot(self) -> RawLatestSlot[VC003RawFrame]:
        return self._slot

    @property
    def slot(self) -> RawLatestSlot[VC003RawFrame]:
        return self._slot

    @property
    def last_reset_status(self) -> RawLatestStatus | None:
        return self._slot.last_reset_status

    def _snapshot_locked(self) -> VC003Status:
        thread = self._thread
        thread_alive = bool(thread is not None and thread.is_alive())
        start_thread = self._start_thread
        start_thread_alive = bool(start_thread is not None and start_thread.is_alive())
        backend_stop_thread = self._backend_stop_thread
        backend_stop_thread_alive = bool(
            backend_stop_thread is not None and backend_stop_thread.is_alive()
        )
        drain_thread = self._drain_thread
        drain_thread_alive = bool(drain_thread is not None and drain_thread.is_alive())
        slot_status = self._slot.status()
        lifecycle = self._lifecycle
        if lifecycle == "running" and self._terminal_error_pending and not thread_alive:
            lifecycle = "error"
        return VC003Status(
            epoch=slot_status.epoch,
            lifecycle=lifecycle,
            session_id=self._session_id,
            produced=slot_status.produced,
            delivered=slot_status.delivered,
            superseded=slot_status.superseded,
            pending=slot_status.pending,
            in_flight=slot_status.in_flight,
            discarded_on_reset=slot_status.discarded_on_reset,
            discarded_on_error=slot_status.discarded_on_error,
            max_depth=slot_status.max_depth,
            next_sequence=self._next_sequence,
            last_sequence=self._last_sequence,
            last_delivered_sequence=self._last_delivered_sequence,
            last_content_hash=self._last_content_hash,
            backend=self.config.backend,
            device_name=self.config.device_name,
            error=self._error,
            thread_alive=thread_alive,
            start_thread_alive=start_thread_alive,
            backend_stop_thread_alive=backend_stop_thread_alive,
            drain_thread_alive=drain_thread_alive,
            consumer_bound=slot_status.consumer_bound,
            final_drain_performed=self._final_drain_performed,
            final_drain_sequence=self._final_drain_sequence,
            read_attempts=self._read_attempts,
            no_frame_count=self._no_frame_count,
            read_failure_count=self._read_failure_count,
            decode_rejection_count=self._decode_rejection_count,
            eof_count=self._eof_count,
            reconnect_count=self._reconnect_count,
            backend_fallback_count=self._backend_fallback_count,
        )

    def status(self) -> VC003Status:
        with self._condition:
            return self._snapshot_locked()

    get_status = status
    snapshot = status

    def describe(self) -> dict[str, Any]:
        """Return portable configuration facts without opening the device."""

        return {
            "read_only": True,
            "source_id": self.config.source_id,
            "device_name": self.config.device_name,
            "device_index": self.config.device_index,
            "backend": self.config.backend,
            "requested_width": self.config.width,
            "requested_height": self.config.height,
            "requested_fps": self.config.fps,
            "wire_pixel_format": self.config.pixel_format,
            "raw_spec": self.config.frame_spec.to_dict(),
            "timestamp_origin": "host_monotonic_post_retrieve",
            "upstream_queue": "unknown",
        }

    def configure(self, config: VC003SourceConfig) -> None:
        """Bind a new source configuration before the next start."""

        if not isinstance(config, VC003SourceConfig):
            raise TypeError("config must be VC003SourceConfig.")
        with self._consumer_transaction_lock, self._condition:
            workers = (
                self._start_thread,
                self._thread,
                self._backend_stop_thread,
                self._drain_thread,
            )
            if (
                self._lifecycle != "created"
                or self._backend is not None
                or self._error is not None
                or any(worker is not None and worker.is_alive() for worker in workers)
            ):
                raise RuntimeError("configure is only valid before the first capture attempt.")
            if self._provenance_external:
                raise RuntimeError("externally bound provenance freezes the initial configuration.")
            if config.session_id != self._session_id:
                self._slot.reset(config.session_id)
            self.config = config
            self._provenance = _default_source_provenance(config)
            self._provenance_external = False
            self._session_id = config.session_id
            self._generation += 1
            self._next_sequence = 1
            self._last_captured_at_ns = None
            self._last_sequence = None
            self._last_delivered_sequence = None
            self._last_content_hash = None
            self._read_attempts = 0
            self._no_frame_count = 0
            self._read_failure_count = 0
            self._decode_rejection_count = 0
            self._eof_count = 0
            self._reconnect_count = 0
            self._backend_fallback_count = 0
            self._error = None
            self._terminal_error_pending = False
            self._reset_pending = False
            self._lifecycle = "created"
            self._condition.notify_all()

    def _set_error(self, error: BaseException | str) -> None:
        self._set_attempt_error(self._attempt_token, error)

    def _set_attempt_error(
        self,
        token: int,
        error: BaseException | str,
        *,
        defer_until_worker_exit: bool = False,
    ) -> None:
        message = str(error) if isinstance(error, BaseException) else error
        with self._condition:
            if token != self._attempt_token:
                return
            if self._error is None:
                self._error = message or type(error).__name__
            if defer_until_worker_exit and self._thread is threading.current_thread():
                self._terminal_error_pending = True
            else:
                self._lifecycle = "error"
            self._stop_event.set()
            self._condition.notify_all()

    def _stop_backend_once(self, timeout_s: float = 2.0, *, token: int | None = None) -> bool:
        """Request backend shutdown without blocking beyond ``timeout_s``."""

        timeout_s = _bounded_wait_seconds(timeout_s, "timeout_s")
        active_token = self._attempt_token if token is None else token
        with self._backend_stop_lock:
            with self._condition:
                if active_token != self._attempt_token:
                    return False
                if self._backend_stopped:
                    return True
                backend = self._backend
                helper = self._backend_stop_thread
            if backend is None:
                return True
            if helper is None:
                self._backend_stop_failures = []

                def close_backend() -> None:
                    try:
                        backend.stop()
                    except BaseException as exc:
                        self._backend_stop_failures.append(exc)
                    finally:
                        with self._condition:
                            if active_token == self._attempt_token:
                                self._backend_stopped = not self._backend_stop_failures
                                self._condition.notify_all()

                helper = threading.Thread(
                    target=close_backend,
                    name="maple-vc003-backend-stop",
                    daemon=True,
                )
                self._backend_stop_thread = helper
                helper.start()
        helper.join(timeout=timeout_s)
        if helper.is_alive():
            self._set_attempt_error(
                active_token,
                "backend stop did not complete within 2 seconds",
                defer_until_worker_exit=True,
            )
            return False
        if self._backend_stop_failures:
            self._set_attempt_error(
                active_token,
                self._backend_stop_failures[0],
                defer_until_worker_exit=True,
            )
            return False
        return True

    def _open_backend(self, token: int) -> None:
        """Create/open one backend attempt and signal its synchronous caller."""

        backend: CaptureBackend | None = None
        error: BaseException | None = None
        try:
            backend = self._backend_factory(self.config)
            with self._condition:
                live_refs: list[weakref.ReferenceType[CaptureBackend]] = []
                reused = False
                for backend_ref in self._used_backend_refs:
                    previous = backend_ref()
                    if previous is not None:
                        live_refs.append(backend_ref)
                        reused = reused or backend is previous
                if reused:
                    raise RuntimeError(
                        "capture recovery requires a newly created backend instance."
                    )
                try:
                    live_refs.append(weakref.ref(backend))
                except TypeError as exc:
                    raise TypeError(
                        "capture backend instances must support weak references."
                    ) from exc
                self._used_backend_refs = live_refs
            for method in ("start", "read", "stop"):
                if not callable(getattr(backend, method, None)):
                    raise TypeError(f"capture backend must implement {method}().")
            with self._condition:
                if token != self._attempt_token:
                    return
                self._backend = backend
                self._backend_stopped = False
                cancelled = self._stop_event.is_set()
            if not cancelled:
                backend.start()
                negotiated = self._read_backend_contract(backend)
                negotiated_format = negotiated.to_format_dict()
                if self._provenance_external:
                    if (
                        dict(self._provenance.negotiated) != negotiated_format
                        or self._provenance.backend_version != negotiated.backend_version
                    ):
                        raise RuntimeError(
                            "measured backend facts contradict externally bound provenance."
                        )
                else:
                    provenance_payload = self._provenance.to_dict()
                    provenance_payload["negotiated"] = negotiated_format
                    provenance_payload["backend_version"] = negotiated.backend_version
                    self._provenance = CaptureSourceProvenance.from_dict(provenance_payload)
                self._negotiated_facts = negotiated
        except BaseException as exc:
            error = exc
        finally:
            with self._condition:
                if token == self._attempt_token:
                    self._start_error = error
                    self._start_done.set()
                    cancelled = self._stop_event.is_set()
                    self._condition.notify_all()
                else:
                    cancelled = True
            if backend is not None and cancelled and error is None:
                self._stop_backend_once(2.0, token=token)

    def _read_backend_contract(self, backend: CaptureBackend) -> NegotiatedCaptureFacts:
        reported_name = getattr(backend, "device_name", None)
        if not isinstance(reported_name, str) or not reported_name.strip():
            raise TypeError("capture backend must report a non-empty device_name.")
        if reported_name != self.config.device_name:
            raise RuntimeError(
                "capture backend selected an unexpected device name "
                f"({reported_name!r} != {self.config.device_name!r})."
            )
        fingerprint = getattr(backend, "device_fingerprint_sha256", None)
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or fingerprint.lower() != fingerprint
        ):
            raise TypeError("capture backend must report a lowercase SHA-256 fingerprint.")
        try:
            bytes.fromhex(fingerprint)
        except ValueError as exc:
            raise TypeError("capture backend fingerprint must be hexadecimal SHA-256.") from exc
        if fingerprint != self._provenance.physical_device_fingerprint_sha256:
            raise RuntimeError("capture backend device fingerprint contradicts provenance.")
        negotiated = getattr(backend, "negotiated_facts", None)
        if not isinstance(negotiated, NegotiatedCaptureFacts):
            raise TypeError("backend must report NegotiatedCaptureFacts after start().")
        expected_fourcc = (
            "MJPG"
            if self.config.pixel_format.lower() in {"mjpg", "mjpeg"}
            else self.config.pixel_format.upper()
        )
        if (
            negotiated.width != self.config.width
            or negotiated.height != self.config.height
            or not _negotiated_fps_matches(negotiated.fps, float(self.config.fps))
            or negotiated.fourcc != expected_fourcc
            or negotiated.backend != self.config.backend
            or negotiated.backend_api.strip().lower() != self.config.backend
        ):
            raise RuntimeError("backend negotiated facts contradict source config.")
        return negotiated

    def start(self) -> None:
        """Open the configured backend and start its producer thread."""

        deadline = monotonic() + 2.0
        with self._start_lock:
            with self._condition:
                if (
                    self._lifecycle == "running"
                    and self._thread is not None
                    and self._thread.is_alive()
                ):
                    return
                if self._lifecycle == "error" or self._error is not None:
                    raise RuntimeError("reset with a new session is required after a source error.")
                workers = (
                    self._start_thread,
                    self._thread,
                    self._backend_stop_thread,
                    self._drain_thread,
                )
                if any(worker is not None and worker.is_alive() for worker in workers):
                    raise RuntimeError("a capture lifecycle worker is still active.")
                if self._lifecycle == "stopped":
                    raise RuntimeError("reset with a new session is required after stop.")
                self._attempt_token += 1
                token = self._attempt_token
                self._stop_event = threading.Event()
                self._start_done = threading.Event()
                self._start_error = None
                self._backend = None
                self._backend_stopped = False
                self._backend_stop_thread = None
                self._backend_stop_failures = []
                self._drain_thread = None
                self._drain_result = None
                self._drain_error = None
                self._error = None
                self._terminal_error_pending = False
                self._reset_pending = False
                self._lifecycle = "starting"
                self._final_drain_performed = False
                self._final_drain_sequence = None
                self._slot.start(self._session_id)
                opener = threading.Thread(
                    target=self._open_backend,
                    args=(token,),
                    name="maple-vc003-backend-start",
                    daemon=True,
                )
                self._start_thread = opener
                opener.start()

            if not self._start_done.wait(max(0.0, deadline - monotonic())):
                self._set_attempt_error(token, "backend start did not complete within 2 seconds")
                self._stop_backend_once(max(0.0, deadline - monotonic()), token=token)
                raise RuntimeError("backend start did not complete within 2 seconds")

            with self._condition:
                start_error = self._start_error
                cancelled = self._stop_event.is_set() or self._lifecycle in {"stopping", "stopped"}
                backend = self._backend
            if start_error is not None:
                self._set_attempt_error(
                    token,
                    f"backend start failed: {type(start_error).__name__}: {start_error}",
                )
                self._stop_backend_once(max(0.0, deadline - monotonic()), token=token)
                raise start_error
            if cancelled or backend is None:
                self._stop_backend_once(max(0.0, deadline - monotonic()), token=token)
                raise RuntimeError("capture start was cancelled by lifecycle stop.")
            with self._condition:
                if token != self._attempt_token or self._stop_event.is_set():
                    raise RuntimeError("capture start attempt is no longer active.")
                self._lifecycle = "running"
                thread = threading.Thread(
                    target=self._run,
                    args=(backend, token, self._stop_event),
                    name="maple-vc003-capture",
                    daemon=True,
                )
                self._thread = thread
                thread.start()

    def _run(
        self,
        backend: CaptureBackend,
        token: int,
        stop_event: threading.Event,
    ) -> None:
        try:
            while not stop_event.is_set():
                # Snapshot the session generation immediately before a read.
                # A reset that races this read then invalidates the payload at
                # publish time; the next loop observes the new generation.
                with self._condition:
                    generation = self._generation
                    if token == self._attempt_token:
                        self._read_attempts += 1
                    frozen_facts = self._negotiated_facts
                try:
                    before_read_facts = self._read_backend_contract(backend)
                    if frozen_facts is None or before_read_facts != frozen_facts:
                        raise RuntimeError(
                            "backend identity or negotiated capture facts drifted during session."
                        )
                except Exception as exc:
                    self._set_attempt_error(
                        token,
                        f"backend format/identity drift: {type(exc).__name__}: {exc}",
                        defer_until_worker_exit=True,
                    )
                    break
                try:
                    payload = backend.read()
                except EOFError as exc:
                    with self._condition:
                        if token == self._attempt_token:
                            self._eof_count += 1
                            self._read_failure_count += 1
                    self._set_attempt_error(
                        token,
                        f"backend EOF: {type(exc).__name__}: {exc}",
                        defer_until_worker_exit=True,
                    )
                    break
                except Exception as exc:
                    with self._condition:
                        if token == self._attempt_token:
                            self._read_failure_count += 1
                    self._set_attempt_error(
                        token,
                        f"backend read failed: {type(exc).__name__}: {exc}",
                        defer_until_worker_exit=True,
                    )
                    break
                if payload is None:
                    with self._condition:
                        if token == self._attempt_token:
                            self._no_frame_count += 1
                    interval = float(self.config.poll_interval_s)
                    if interval > 0:
                        stop_event.wait(interval)
                    continue
                # The frozen timing origin is the first host operation after
                # a successful retrieval.  Contract/fourcc queries and pixel
                # copying happen only after this monotonic snapshot.
                captured_at_ns = self._clock()
                try:
                    current_facts = self._read_backend_contract(backend)
                    if frozen_facts is None or current_facts != frozen_facts:
                        raise RuntimeError(
                            "backend identity or negotiated capture facts drifted during session."
                        )
                except Exception as exc:
                    self._set_attempt_error(
                        token,
                        f"backend format/identity drift: {type(exc).__name__}: {exc}",
                        defer_until_worker_exit=True,
                    )
                    break
                try:
                    data, explicit_spec, payload_timestamp = _payload_parts(payload)
                    raw_bytes, spec = _coerce_bytes_and_spec(data, self.config, explicit_spec)
                    if isinstance(captured_at_ns, bool) or not isinstance(captured_at_ns, int):
                        raise ValueError("captured_at_ns must be an integer.")
                    _ensure_non_negative_int(captured_at_ns, "captured_at_ns")
                    if payload_timestamp is not None:
                        if isinstance(payload_timestamp, bool) or not isinstance(
                            payload_timestamp, int
                        ):
                            raise ValueError("backend captured_at_ns must be an integer.")
                        _ensure_non_negative_int(
                            payload_timestamp,
                            "backend captured_at_ns",
                        )
                except Exception as exc:
                    with self._condition:
                        if token == self._attempt_token:
                            self._decode_rejection_count += 1
                    self._set_attempt_error(
                        token,
                        f"backend frame rejected: {type(exc).__name__}: {exc}",
                        defer_until_worker_exit=True,
                    )
                    break
                try:
                    self._publish(
                        generation,
                        captured_at_ns,
                        raw_bytes,
                        spec,
                        backend_timestamp_ns=payload_timestamp,
                    )
                except Exception as exc:
                    self._set_attempt_error(
                        token,
                        f"raw sample publish failed: {type(exc).__name__}: {exc}",
                        defer_until_worker_exit=True,
                    )
                    break
        finally:
            self._stop_backend_once(token=token)
            with self._condition:
                if token == self._attempt_token and self._thread is threading.current_thread():
                    if self._lifecycle == "running" and self._error is None:
                        self._lifecycle = "stopped"
                    self._condition.notify_all()

    def _publish(
        self,
        generation: int,
        captured_at_ns: int,
        raw_bytes: bytes,
        spec: RawFrameSpec,
        *,
        backend_timestamp_ns: int | None = None,
    ) -> None:
        with self._condition:
            # A stop may race an in-flight backend read.  Publish that
            # already-retrieved frame so normal stop can perform the promised
            # final drain; a reset invalidates it before the new epoch opens.
            if generation != self._generation or self._reset_pending:
                self._condition.notify_all()
                return
            if self._last_captured_at_ns is not None and captured_at_ns < self._last_captured_at_ns:
                raise ValueError("captured_at_ns moved backwards within the source session.")
            sequence = self._next_sequence
            self._next_sequence += 1
            content_hash = pixel_digest(spec, raw_bytes)
            metadata = {
                "backend": self.config.backend,
                "device_name": self.config.device_name,
                "device_index": self.config.device_index,
                "wire_pixel_format": self.config.pixel_format,
                "raw_spec": spec.to_dict(),
            }
            if backend_timestamp_ns is not None:
                metadata["backend_timestamp"] = {
                    "value_ns": backend_timestamp_ns,
                    "timing_truth": False,
                }
            sample = VC003RawFrame(
                source_id=self.config.source_id,
                session_id=self._session_id,
                frame_id=sequence,
                captured_at_ns=captured_at_ns,
                clock_domain=self.config.clock_domain,
                transform_version=self.config.transform_version,
                # Leave geometry absent unless the physical source was
                # explicitly bound to one.  FrameSourceAdapter then applies
                # its own canonical crop/resize configuration without a
                # synthetic full-frame geometry causing a mismatch.
                source_geometry=self.config.source_geometry,
                content_hash=content_hash,
                image_ref=f"cas://sha256/{content_hash}",
                source_size=FrameSize(width=spec.width, height=spec.height),
                image_metadata=metadata,
                received_at_ns=captured_at_ns,
                raw_bytes=raw_bytes,
                spec=spec,
            )
            self._slot.publish(sample)
            self._last_captured_at_ns = captured_at_ns
            self._last_sequence = sequence
            self._last_content_hash = content_hash
            self._condition.notify_all()

    def read(
        self,
        timeout: float = 0.0,
        *,
        timeout_ms: float | None = None,
        timeout_s: float | None = None,
        timeout_ns: int | None = None,
        deadline_ns: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> VC003RawFrame | None:
        """Deliver the latest pending raw frame to the bound consumer.

        ``timeout`` is in seconds.  ``timeout_ms`` is accepted as a convenient
        integration spelling and takes precedence when supplied.
        """

        timeout = _resolve_wait_timeout(
            timeout,
            timeout_ms=timeout_ms,
            timeout_s=timeout_s,
            timeout_ns=timeout_ns,
            deadline_ns=deadline_ns,
        )
        if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
            raise TypeError("cancel_event must provide is_set().")
        with self._consumer_transaction_lock:
            return self._read_verified(float(timeout), cancel_event)

    def _read_verified(
        self,
        timeout: float,
        cancel_event: threading.Event | None,
        *,
        controller_final_drain: bool = False,
    ) -> VC003RawFrame | None:
        with self._condition:
            if self._error is not None:
                raise VC003SourceError(f"VC-003 source error: {self._error}")
            if self._reset_pending:
                return None
            if timeout:
                end = monotonic() + timeout
                while (
                    self._slot.pending is None
                    and not self._stop_event.is_set()
                    and (cancel_event is None or not cancel_event.is_set())
                ):
                    remaining = end - monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
            if self._error is not None:
                raise VC003SourceError(f"VC-003 source error: {self._error}")
            if cancel_event is not None and cancel_event.is_set():
                return None
            sample = (
                self._slot._reserve_for_final_drain()
                if controller_final_drain
                else self._slot.reserve()
            )
            generation = self._generation
            self._condition.notify_all()
        if sample is None:
            return None

        # Pixel hashing/storage deliberately runs outside the source condition
        # so the producer can keep draining the backend into the capacity-one
        # raw slot while the logical consumer verifies this delivered sample.
        try:
            artifact = self._pixel_store.put_artifact(
                sample.spec,
                sample.raw_bytes,
                privacy_class="restricted",
                retention_class=("ephemeral" if self._temporary_cas is not None else "candidate"),
                source_provenance_id=self._provenance.provenance_id,
                session_id=sample.session_id or self._session_id,
                source_sequence=sample.sequence,
            )
            resolved = self._pixel_store.read(artifact.pixel_digest, sample.spec)
            if artifact.ref != sample.image_ref or resolved != sample.raw_bytes:
                raise ValueError("verified CAS artifact does not match raw sample")
        except Exception as exc:
            with self._condition:
                reset_pending = self._reset_pending
            if reset_pending:
                self._slot.discard_reserved_on_reset(sample)
                return None
            if controller_final_drain:
                self._slot._discard_final_drain_on_error(sample)
            else:
                self._slot.discard_reserved_on_error(sample)
            with self._condition:
                if self._error is None:
                    self._error = f"pixel CAS admission failed: {type(exc).__name__}: {exc}"
                self._lifecycle = "error"
                self._stop_event.set()
                self._condition.notify_all()
            self._stop_backend_once()
            raise VC003SourceError(f"VC-003 source error: {self._error}") from exc
        delivered_sample = replace(
            sample,
            image_metadata={
                **dict(sample.image_metadata),
                "pixel_artifact_sha256": artifact.artifact_sha256,
                "source_provenance_id": artifact.source_provenance_id,
            },
        )
        with self._condition:
            # Reset/configure take the same consumer transaction lock.  This
            # check is defensive evidence that no old-session sample crosses
            # a generation boundary before return.
            reset_pending = self._reset_pending
            if (
                reset_pending
                or generation != self._generation
                or sample.session_id != self._session_id
            ):
                if reset_pending:
                    self._slot.discard_reserved_on_reset(sample)
                elif controller_final_drain:
                    self._slot._discard_final_drain_on_error(sample)
                else:
                    self._slot.discard_reserved_on_error(sample)
                return None
            if controller_final_drain:
                self._slot._commit_final_drain(sample)
            else:
                self._slot.commit_reserved(sample)
            self._last_delivered_sequence = sample.sequence
            self._condition.notify_all()
        return delivered_sample

    def _run_final_drain(self, token: int) -> None:
        """Drain the final pending sample without rebinding the logical consumer."""

        result: VC003RawFrame | None = None
        error: BaseException | None = None
        try:
            with self._consumer_transaction_lock:
                result = self._read_verified(
                    0.0,
                    None,
                    controller_final_drain=True,
                )
        except BaseException as exc:
            error = exc
        finally:
            with self._condition:
                if token == self._attempt_token:
                    self._drain_result = result
                    self._drain_error = error
                    self._condition.notify_all()

    def stop(self) -> VC003RawFrame | None:
        """Single-flight stop/final-drain controller with one absolute deadline."""

        deadline = monotonic() + 1.95
        if not self._stop_controller_lock.acquire(timeout=max(0.0, deadline - monotonic())):
            self._set_error("concurrent stop controller did not complete within 2 seconds")
            return None
        try:
            return self._stop_once(deadline)
        finally:
            self._stop_controller_lock.release()

    def _stop_for_reset(self) -> None:
        """Stop lifecycle workers for reset without consuming the old slot."""

        deadline = monotonic() + 1.95
        if not self._stop_controller_lock.acquire(timeout=max(0.0, deadline - monotonic())):
            self._set_error("concurrent stop controller did not complete within 2 seconds")
            raise RuntimeError("reset requires a quiescent stop controller.")
        try:
            with self._condition:
                self._reset_pending = True
                self._stop_event.set()
                self._condition.notify_all()
            self._stop_once(deadline, final_drain=False)
        except BaseException:
            with self._condition:
                self._reset_pending = False
                self._condition.notify_all()
            raise
        finally:
            self._stop_controller_lock.release()

    def _stop_once(
        self,
        deadline: float,
        *,
        final_drain: bool = True,
    ) -> VC003RawFrame | None:
        """Stop capture and join lifecycle workers within the deadline."""

        with self._condition:
            token = self._attempt_token
            thread = self._thread
            start_thread = self._start_thread
            stop_thread = self._backend_stop_thread
            drain_thread = self._drain_thread
            workers_quiescent = (
                (thread is None or not thread.is_alive())
                and (start_thread is None or not start_thread.is_alive())
                and (stop_thread is None or not stop_thread.is_alive())
                and (drain_thread is None or not drain_thread.is_alive())
            )
            already_stopped = (
                self._lifecycle == "stopped" and workers_quiescent and self._backend_stopped
            )
            never_started = (
                self._lifecycle == "created" and workers_quiescent and self._backend is None
            )
            if already_stopped or never_started:
                self._slot.stop()
                self._lifecycle = "stopped"
                if never_started and final_drain:
                    self._final_drain_performed = True
                return self._drain_result if already_stopped else None
            if self._error is None:
                self._lifecycle = "stopping"
            self._stop_event.set()
            self._condition.notify_all()
        self._stop_backend_once(max(0.0, deadline - monotonic()), token=token)
        if start_thread is not None and start_thread is not threading.current_thread():
            start_thread.join(timeout=max(0.0, deadline - monotonic()))
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - monotonic()))
        with self._condition:
            residual = any(
                worker is not None and worker.is_alive()
                for worker in (
                    self._start_thread,
                    self._thread,
                    self._backend_stop_thread,
                    self._drain_thread,
                )
            )
            backend_unreleased = self._backend is not None and not self._backend_stopped
            if residual or backend_unreleased:
                if self._error is None:
                    self._error = "capture lifecycle workers did not stop within 2 seconds"
                self._lifecycle = "error"
            else:
                self._slot.stop()
                self._lifecycle = "error" if self._error is not None else "stopping"
            should_drain = (
                final_drain and not residual and not backend_unreleased and self._error is None
            )
            if not final_drain and not residual and not backend_unreleased:
                self._lifecycle = "error" if self._error is not None else "stopped"
            self._condition.notify_all()
        if not should_drain:
            return None
        if self._slot.pending is None:
            with self._condition:
                self._final_drain_performed = True
                self._final_drain_sequence = self._last_delivered_sequence
                self._lifecycle = "stopped"
                self._condition.notify_all()
            return None
        with self._condition:
            self._drain_result = None
            self._drain_error = None
            drain_thread = threading.Thread(
                target=self._run_final_drain,
                args=(token,),
                name="maple-vc003-final-drain",
                daemon=True,
            )
            self._drain_thread = drain_thread
            drain_thread.start()
        drain_thread.join(timeout=max(0.0, deadline - monotonic()))
        with self._condition:
            if drain_thread.is_alive():
                if self._error is None:
                    self._error = "final drain did not complete within 2 seconds"
                self._lifecycle = "error"
                self._condition.notify_all()
                return None
            if self._drain_error is not None:
                if self._error is None:
                    self._error = (
                        "final drain failed: "
                        f"{type(self._drain_error).__name__}: {self._drain_error}"
                    )
                self._lifecycle = "error"
                self._condition.notify_all()
                return None
            drained = self._drain_result
            self._final_drain_performed = True
            self._final_drain_sequence = None if drained is None else drained.sequence
            self._lifecycle = "stopped"
            self._condition.notify_all()
        return drained

    def reset(
        self,
        new_session_id: str,
        *,
        provenance: CaptureSourceProvenance | None = None,
    ) -> None:
        """Flush pending data and bind subsequent samples to a new session."""

        _ensure_non_empty_str(new_session_id, "new_session_id")
        with self._condition:
            if new_session_id == self._session_id:
                raise ValueError("new_session_id must differ from the active session_id.")
        if self._singleton_backend:
            raise RuntimeError("reset recovery requires a factory that creates a new backend.")
        if self._provenance_external and provenance is None:
            raise ValueError("an externally bound source requires new-session provenance on reset.")
        next_config = replace(self.config, session_id=new_session_id)
        next_provenance = (
            _default_source_provenance(next_config) if provenance is None else provenance
        )
        expected_config_hash = sha256(canonical_json(next_config.to_dict())).hexdigest()
        expected_calibration_hash = canonical_calibration_sha256(
            next_config.geometry,
            next_config.transform_version,
        )
        if (
            not isinstance(next_provenance, CaptureSourceProvenance)
            or next_provenance.source_id != next_config.source_id
            or next_provenance.session_id != new_session_id
            or next_provenance.backend != next_config.backend
            or next_provenance.config_sha256 != expected_config_hash
            or next_provenance.calibration_sha256 != expected_calibration_hash
            or dict(next_provenance.requested) != _requested_format(next_config)
        ):
            raise ValueError("reset provenance must bind the new session/config/calibration.")
        self._stop_for_reset()
        with self._condition:
            residual = any(
                worker is not None and worker.is_alive()
                for worker in (
                    self._start_thread,
                    self._thread,
                    self._backend_stop_thread,
                    self._drain_thread,
                )
            )
            if residual or (self._backend is not None and not self._backend_stopped):
                self._reset_pending = False
                self._condition.notify_all()
                raise RuntimeError("reset requires confirmed backend and worker cleanup.")
        if not self._consumer_transaction_lock.acquire(timeout=1.95):
            with self._condition:
                self._reset_pending = False
                self._condition.notify_all()
            self._set_error("consumer transaction did not quiesce within 2 seconds")
            raise RuntimeError("reset requires a quiescent consumer transaction.")
        reset_applied = False
        try:
            with self._condition:
                self._slot.reset(new_session_id)
                self.config = next_config
                self._provenance = next_provenance
                self._provenance_external = provenance is not None
                self._session_id = new_session_id
                self._generation += 1
                self._next_sequence = 1
                self._last_captured_at_ns = None
                self._last_sequence = None
                self._last_delivered_sequence = None
                self._last_content_hash = None
                self._negotiated_facts = None
                self._read_attempts = 0
                self._no_frame_count = 0
                self._read_failure_count = 0
                self._decode_rejection_count = 0
                self._eof_count = 0
                self._reconnect_count = 0
                self._backend_fallback_count = 0
                self._final_drain_performed = False
                self._final_drain_sequence = None
                self._error = None
                self._terminal_error_pending = False
                self._reset_pending = False
                self._backend = None
                self._backend_stopped = True
                self._start_thread = None
                self._backend_stop_thread = None
                self._backend_stop_failures = []
                self._thread = None
                self._stop_event = threading.Event()
                self._lifecycle = "created"
                reset_applied = True
                self._condition.notify_all()
        finally:
            if not reset_applied:
                with self._condition:
                    self._reset_pending = False
                    self._condition.notify_all()
            self._consumer_transaction_lock.release()

    reset_session = reset


VC003FrameSource = VC003Source
VC003ReadOnlySource = VC003Source
CaptureCardSource = VC003Source
RawLatest = RawLatestSlot
VC003Adapter = VC003Source
VC003ReadOnlyAdapter = VC003Source
RawLatestBuffer = RawLatestSlot


__all__ = [
    "BackendFactory",
    "BackendFrame",
    "CaptureBackend",
    "CaptureCardSource",
    "Clock",
    "NegotiatedCaptureFacts",
    "OpenCVBackend",
    "OpenCVCaptureBackend",
    "RawCaptureFrame",
    "RawCaptureSample",
    "RawFrameSample",
    "RawFrameSpec",
    "RawLatest",
    "RawLatestBuffer",
    "RawLatestMetrics",
    "RawLatestSlot",
    "RawLatestSlotStatus",
    "RawLatestStatus",
    "VC003Adapter",
    "VC003AdapterConfig",
    "VC003Backend",
    "VC003CaptureConfig",
    "VC003Config",
    "VC003FrameSource",
    "VC003RawFrame",
    "VC003ReadOnlyAdapter",
    "VC003ReadOnlySource",
    "VC003Sample",
    "VC003Source",
    "VC003SourceConfig",
    "VC003SourceError",
    "VC003Status",
    "create_opencv_backend",
    "make_opencv_backend",
]
