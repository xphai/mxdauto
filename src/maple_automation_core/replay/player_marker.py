"""Deterministic, hash-only replay for the independent player marker.

The frame corpus and Event Tape are the source of truth at this boundary.  This
module deliberately does not import a marker detector implementation: callers
inject a small callable (or an object implementing ``extract``) and receive a
``PlayerMarkerFrame`` containing the verified CAS bytes.  Only events carrying
an accepted admission packet reach that callable.  In particular, a
``wrong_size`` corpus row is rejected before the extractor is touched.

The public report is an evidence envelope, not an accuracy report.  It keeps
sample order and all cross-run comparisons deterministic while publishing only
portable identifiers and SHA-256 digests.  Raw pixels, host paths, source
session identifiers, and candidate identity fields never enter the report.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from maple_automation_core.capture.pixel_store import (
    PixelSpec,
    PixelStore,
    canonical_json,
    pixel_digest,
    validate_pixels,
)
from maple_automation_core.domain._contract_utils import (
    canonical_json_bytes,
    ensure_json_value,
    ensure_mapping,
    ensure_non_empty_str,
    ensure_non_negative_int,
    ensure_time_ns,
    freeze_json_value,
    to_json_dict,
)
from maple_automation_core.localization.player_localizer import PlayerCandidate
from maple_automation_core.replay.event_tape import EventRecord, EventTape
from maple_automation_core.replay.frame_corpus import (
    TRUTH_SCOPE,
    load_strict_json,
    verify_corpus_manifest,
    verify_truth_record,
)
from maple_automation_core.replay.frame_corpus import (
    canonical_digest as corpus_canonical_digest,
)

PLAYER_MARKER_REPLAY_SCHEMA_VERSION = "1.0.0"
PLAYER_MARKER_REPLAY_REPORT_VERSION = PLAYER_MARKER_REPLAY_SCHEMA_VERSION
PLAYER_MARKER_REPLAY_REPORT_TYPE = "player_marker_replay"
PLAYER_MARKER_REPLAY_SCOPE = "G1-LOC-003B"
PLAYER_MARKER_REPLAY_REPEAT_COUNT = 3

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_FAULTS = frozenset(
    {
        "admission_missing",
        "ambiguous_packet",
        "candidate_lineage_mismatch",
        "clock_domain_mismatch",
        "duplicate",
        "extractor_error",
        "extractor_missing",
        "extractor_result_invalid",
        "frame_size_changed",
        "no_frame",
        "out_of_order",
        "pixel_unavailable",
        "packet_lineage_mismatch",
        "session_mismatch",
        "source_error",
        "source_mismatch",
        "stale",
        "suppressed",
        "timestamp_regression",
        "truth_invalid",
    }
)
_STATUSES = frozenset({"detected", "no_marker", "rejected", "fault"})
_ZERO_INPUT_KEYS = (
    "input_owner",
    "real_input_enabled",
    "real_input_call_count",
    "core_v2_real_input_call_count",
    "receiver_connect_count",
    "window_write_count",
    "double_write_event_count",
    "keyboard_call_count",
    "mouse_call_count",
)
_REPORT_FORBIDDEN_KEYS = frozenset(
    {
        "path",
        "paths",
        "raw_pixel",
        "raw_pixels",
        "pixels",
        "image_ref",
        "subject_id",
        "player_id",
        "identity",
        "identity_mapping",
    }
)


class PlayerMarkerReplayError(ValueError):
    """Raised when a marker replay input or report violates its contract."""


class PlayerMarkerReplayDeterminismError(PlayerMarkerReplayError):
    """Raised when the required three runs do not agree item by item."""


def _digest(value: Any) -> str:
    """Hash a strict JSON value, wrapping non-mappings for the shared helper."""

    if isinstance(value, Mapping):
        payload = value
    else:
        ensure_json_value(value, "digest payload")
        payload = {"value": value}
    return sha256(canonical_json_bytes(payload)).hexdigest()


def _digest_bytes(value: bytes) -> str:
    digest = sha256()
    digest.update(b"MAPLE_PLAYER_MARKER_REPLAY_BYTES_V1\0")
    digest.update(value)
    return digest.hexdigest()


def _sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PlayerMarkerReplayError(f"{field_name} must be lowercase SHA-256")
    return value


def _commit(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise PlayerMarkerReplayError(f"{field_name} must be a lowercase 40-character commit")
    return value


def _portable_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise PlayerMarkerReplayError(f"{field_name} must be a portable identifier")
    return value


def _strict_json(value: object, field_name: str) -> Any:
    try:
        ensure_json_value(value, field_name)
        return freeze_json_value(value)
    except (TypeError, ValueError) as exc:
        raise PlayerMarkerReplayError(f"{field_name} must be strict JSON") from exc


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    try:
        return ensure_mapping(value, field_name)
    except ValueError as exc:
        raise PlayerMarkerReplayError(str(exc)) from exc


def _as_json(value: object, field_name: str) -> Any:
    """Convert a detector value to the strict JSON subset without exposing it."""

    if isinstance(value, PlayerCandidate):
        return value.to_dict()
    if isinstance(value, Mapping):
        return to_json_dict(_strict_json(value, field_name))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            converted = to_dict()
            return to_json_dict(_strict_json(converted, field_name))
        except (PlayerMarkerReplayError, TypeError, ValueError):
            # A detector object may intentionally expose only a stable digest
            # while its rich ``to_dict`` contains runtime-only fields.
            pass
    if isinstance(value, bytes):
        # Detector implementations occasionally return a compact encoded mask.
        # Keep the bytes internal and bind them to a digest; never serialize it.
        return {"bytes_sha256": _digest_bytes(value), "byte_count": len(value)}
    if isinstance(value, bytearray | memoryview):
        raw = bytes(value)
        return {"bytes_sha256": _digest_bytes(raw), "byte_count": len(raw)}
    try:
        return to_json_dict(_strict_json(value, field_name))
    except PlayerMarkerReplayError:
        # Objects with a stable digest property are a useful integration seam
        # for the in-development detector.  Only the digest crosses the seam.
        declared = getattr(value, "digest", None)
        if isinstance(declared, str) and _SHA256_RE.fullmatch(declared) is not None:
            return {"declared_digest": declared}
        raise


def _value_digest(value: object, field_name: str) -> str:
    return _digest(_as_json(value, field_name))


def _status_token(value: object) -> str:
    """Accept enum-like detector statuses without importing its module."""

    token = getattr(value, "value", value)
    if not isinstance(token, str):
        token = str(token)
    token = token.lower()
    if token in {"candidate", "found", "detected", "accepted"}:
        return "detected"
    if token in {"none", "no_candidate", "no-marker", "no_marker", "not_detected", "unknown"}:
        return "no_marker"
    if token in {"fault", "invalid", "error"}:
        return "fault"
    return token


def _fault_token(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    code = getattr(value, "code", value)
    token = getattr(code, "value", code)
    return token if isinstance(token, str) else "extractor_result_invalid"


def _forbidden_keys(value: object, *, field_name: str = "report") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _REPORT_FORBIDDEN_KEYS:
                found.append(f"{field_name}.{key}")
            found.extend(_forbidden_keys(child, field_name=f"{field_name}.{key}"))
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            found.extend(_forbidden_keys(child, field_name=f"{field_name}[{index}]"))
    return found


def _sample_result_digest(
    sample_id: str,
    status: str,
    candidate_digest: str | None,
    evidence_digest: str | None,
    fault: str | None,
) -> str:
    return _digest(
        {
            "sample_id": sample_id,
            "status": status,
            "candidate_digest": candidate_digest,
            "evidence_digest": evidence_digest,
            "fault": fault,
        }
    )


@runtime_checkable
class PlayerMarkerExtractor(Protocol):
    """Callable seam for a marker detector under development."""

    def __call__(self, frame: PlayerMarkerFrame) -> object:
        """Extract one candidate or return ``None`` for no marker."""


MarkerExtractor = PlayerMarkerExtractor


@dataclass(frozen=True, slots=True)
class PlayerMarkerFrame:
    """Verified accepted packet view handed to an injected extractor.

    ``pixels`` is intentionally an internal input value.  This object is never
    serialized into a report.  ``frame_id`` is the packet frame id; ``sequence``
    is the corpus sequence and is the stable sample locator used for replay.
    """

    sample_id: str
    truth_id: str
    session_id: str
    source_id: str
    sequence: int
    frame_id: int
    observed_at_ns: int
    as_of_ns: int
    pixel_digest: str
    pixel_spec: PixelSpec
    pixels: bytes
    event_record_hash: str
    admission_status: str = "accepted"

    def __post_init__(self) -> None:
        _portable_id(self.sample_id, "sample_id")
        _portable_id(self.truth_id, "truth_id")
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.source_id, "source_id")
        ensure_non_negative_int(self.sequence, "sequence")
        ensure_non_negative_int(self.frame_id, "frame_id")
        ensure_time_ns(self.observed_at_ns, "observed_at_ns")
        ensure_time_ns(self.as_of_ns, "as_of_ns")
        _sha256(self.pixel_digest, "pixel_digest")
        if not isinstance(self.pixel_spec, PixelSpec):
            raise TypeError("pixel_spec must be PixelSpec")
        if not isinstance(self.pixels, bytes):
            raise TypeError("pixels must be immutable bytes")
        if len(self.pixels) != self.pixel_spec.length:
            raise ValueError("pixels length does not match pixel_spec")
        _sha256(self.event_record_hash, "event_record_hash")
        if self.admission_status != "accepted":
            raise ValueError("PlayerMarkerFrame must represent an accepted packet")

    @property
    def raw_pixels(self) -> bytes:
        """Compatibility alias for detector integrations; never report this value."""

        return self.pixels

    @property
    def data(self) -> bytes:
        return self.pixels

    @property
    def width(self) -> int:
        return self.pixel_spec.width

    @property
    def height(self) -> int:
        return self.pixel_spec.height

    @property
    def channels(self) -> int:
        return self.pixel_spec.channels


@dataclass(frozen=True, slots=True)
class PlayerMarkerExtraction:
    """Optional structured result type for injected marker extractors."""

    candidate: object | None = None
    evidence: object | None = None
    status: str = "detected"
    fault: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"detected", "no_marker", "accepted", "fault"}:
            raise ValueError("unsupported marker extraction status")
        if self.fault is not None:
            _portable_id(self.fault, "fault")


@dataclass(frozen=True, slots=True)
class PlayerMarkerReplaySample:
    """Hash-only result for one manifest sample."""

    sample_id: str
    status: str
    candidate_digest: str | None
    evidence_digest: str | None
    result_digest: str
    fault: str | None = None

    def __post_init__(self) -> None:
        _portable_id(self.sample_id, "sample_id")
        if self.status not in _STATUSES:
            raise PlayerMarkerReplayError("sample status is unsupported")
        if self.status == "detected" and self.candidate_digest is None:
            raise PlayerMarkerReplayError("detected samples require candidate_digest")
        if self.status == "no_marker" and self.candidate_digest is not None:
            raise PlayerMarkerReplayError("no_marker samples cannot carry candidate_digest")
        if self.candidate_digest is not None:
            _sha256(self.candidate_digest, "candidate_digest")
        if self.evidence_digest is not None:
            _sha256(self.evidence_digest, "evidence_digest")
        if self.fault is not None and self.fault not in _FAULTS:
            raise PlayerMarkerReplayError("sample fault is unsupported")
        expected = _sample_result_digest(
            self.sample_id,
            self.status,
            self.candidate_digest,
            self.evidence_digest,
            self.fault,
        )
        if self.result_digest != expected:
            raise PlayerMarkerReplayError("sample result_digest mismatch")

    @classmethod
    def build(
        cls,
        *,
        sample_id: str,
        status: str,
        candidate_digest: str | None = None,
        evidence_digest: str | None = None,
        fault: str | None = None,
    ) -> PlayerMarkerReplaySample:
        return cls(
            sample_id=sample_id,
            status=status,
            candidate_digest=candidate_digest,
            evidence_digest=evidence_digest,
            result_digest=_sample_result_digest(
                sample_id,
                status,
                candidate_digest,
                evidence_digest,
                fault,
            ),
            fault=fault,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "status": self.status,
            "candidate_digest": self.candidate_digest,
            "evidence_digest": self.evidence_digest,
            "result_digest": self.result_digest,
            "fault": self.fault,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlayerMarkerReplaySample:
        data = _mapping(value, "sample result")
        expected_keys = {
            "sample_id",
            "status",
            "candidate_digest",
            "evidence_digest",
            "result_digest",
            "fault",
        }
        if set(data) != expected_keys:
            raise PlayerMarkerReplayError("sample result keys are not exact")
        return cls(
            sample_id=data["sample_id"],
            status=data["status"],
            candidate_digest=data["candidate_digest"],
            evidence_digest=data["evidence_digest"],
            result_digest=data["result_digest"],
            fault=data["fault"],
        )


@dataclass(frozen=True, slots=True)
class PlayerMarkerReplayRun:
    """One deterministic pass over the manifest's physical sample order."""

    run_index: int
    sample_order_digest: str
    samples: tuple[PlayerMarkerReplaySample, ...]
    run_digest: str

    def __post_init__(self) -> None:
        ensure_non_negative_int(self.run_index, "run_index")
        if self.run_index < 1:
            raise PlayerMarkerReplayError("run_index must start at one")
        _sha256(self.sample_order_digest, "sample_order_digest")
        if not isinstance(self.samples, tuple):
            raise TypeError("samples must be a tuple")
        if any(not isinstance(item, PlayerMarkerReplaySample) for item in self.samples):
            raise TypeError("samples must contain PlayerMarkerReplaySample values")
        if _digest([item.sample_id for item in self.samples]) != self.sample_order_digest:
            raise PlayerMarkerReplayError("run sample order does not match sample_order_digest")
        expected = _digest(
            {
                "sample_order_digest": self.sample_order_digest,
                "samples": [item.to_dict() for item in self.samples],
            }
        )
        if self.run_digest != expected:
            raise PlayerMarkerReplayError("run_digest mismatch")

    @classmethod
    def build(
        cls,
        *,
        run_index: int,
        sample_order_digest: str,
        samples: Sequence[PlayerMarkerReplaySample],
    ) -> PlayerMarkerReplayRun:
        frozen = tuple(samples)
        return cls(
            run_index=run_index,
            sample_order_digest=sample_order_digest,
            samples=frozen,
            run_digest=_digest(
                {
                    "sample_order_digest": sample_order_digest,
                    "samples": [item.to_dict() for item in frozen],
                }
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_index": self.run_index,
            "sample_order_digest": self.sample_order_digest,
            "samples": [item.to_dict() for item in self.samples],
            "run_digest": self.run_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlayerMarkerReplayRun:
        data = _mapping(value, "replay run")
        expected_keys = {"run_index", "sample_order_digest", "samples", "run_digest"}
        if set(data) != expected_keys:
            raise PlayerMarkerReplayError("replay run keys are not exact")
        raw_samples = data["samples"]
        if not isinstance(raw_samples, list):
            raise PlayerMarkerReplayError("replay run samples must be an array")
        return cls(
            run_index=data["run_index"],
            sample_order_digest=data["sample_order_digest"],
            samples=tuple(PlayerMarkerReplaySample.from_dict(item) for item in raw_samples),
            run_digest=data["run_digest"],
        )


def _default_zero_input_audit() -> dict[str, Any]:
    return {
        "input_owner": "legacy",
        "real_input_enabled": False,
        "real_input_call_count": 0,
        "core_v2_real_input_call_count": 0,
        "receiver_connect_count": 0,
        "window_write_count": 0,
        "double_write_event_count": 0,
        "keyboard_call_count": 0,
        "mouse_call_count": 0,
    }


def _normalise_zero_input_audit(value: Mapping[str, Any] | None) -> dict[str, Any]:
    defaults = _default_zero_input_audit()
    if value is not None:
        incoming = _mapping(value, "zero_input_audit")
        unknown = set(incoming).difference(_ZERO_INPUT_KEYS)
        if unknown:
            raise PlayerMarkerReplayError(
                f"zero_input_audit has unexpected key(s): {sorted(unknown)!r}"
            )
        for key in _ZERO_INPUT_KEYS:
            if key in incoming:
                defaults[key] = incoming[key]
    if set(defaults) != set(_ZERO_INPUT_KEYS):  # pragma: no cover - defensive
        raise PlayerMarkerReplayError("zero_input_audit schema drift")
    if defaults["input_owner"] != "legacy" or defaults["real_input_enabled"] is not False:
        raise PlayerMarkerReplayError("zero-input audit contradicts Legacy ownership")
    for key in _ZERO_INPUT_KEYS[2:]:
        if type(defaults[key]) is not int or defaults[key] != 0:
            raise PlayerMarkerReplayError("zero-input audit contains a non-zero counter")
    return defaults


@dataclass(frozen=True, slots=True)
class PlayerMarkerReplayReport:
    """Self-validating hash-only three-run replay report."""

    source_commit: str
    manifest_digest: str
    corpus_digest: str
    config_digest: str
    as_of_ns: int
    sample_order_digest: str
    sample_count: int
    repeat_count: int
    deterministic: bool
    status: str
    event_tape_digest: str
    zero_input_audit: Mapping[str, Any]
    limitations: tuple[str, ...]
    runs: tuple[PlayerMarkerReplayRun, ...]
    report_digest: str
    schema_version: str = PLAYER_MARKER_REPLAY_SCHEMA_VERSION
    report_type: str = PLAYER_MARKER_REPLAY_REPORT_TYPE
    scope: str = PLAYER_MARKER_REPLAY_SCOPE
    truth_scope: str = TRUTH_SCOPE
    report_id: str = ""

    def __post_init__(self) -> None:
        _commit(self.source_commit, "source_commit")
        _sha256(self.manifest_digest, "manifest_digest")
        _sha256(self.corpus_digest, "corpus_digest")
        _sha256(self.config_digest, "config_digest")
        ensure_time_ns(self.as_of_ns, "as_of_ns")
        _sha256(self.sample_order_digest, "sample_order_digest")
        ensure_non_negative_int(self.sample_count, "sample_count")
        if self.sample_count < 1:
            raise PlayerMarkerReplayError("sample_count must be positive")
        if self.repeat_count != PLAYER_MARKER_REPLAY_REPEAT_COUNT:
            raise PlayerMarkerReplayError("G1-LOC-003B replay requires exactly three runs")
        if not isinstance(self.deterministic, bool):
            raise TypeError("deterministic must be bool")
        if self.status not in {"PASS", "FAIL"}:
            raise PlayerMarkerReplayError("report status is unsupported")
        _sha256(self.event_tape_digest, "event_tape_digest")
        if not isinstance(self.runs, tuple) or len(self.runs) != self.repeat_count:
            raise PlayerMarkerReplayError("repeat_count must equal the number of runs")
        if any(not isinstance(item, PlayerMarkerReplayRun) for item in self.runs):
            raise TypeError("runs must contain PlayerMarkerReplayRun values")
        if not isinstance(self.zero_input_audit, Mapping):
            raise TypeError("zero_input_audit must be a mapping")
        audit = _normalise_zero_input_audit(self.zero_input_audit)
        object.__setattr__(self, "zero_input_audit", freeze_json_value(audit))
        if not isinstance(self.limitations, tuple) or not self.limitations:
            raise PlayerMarkerReplayError("limitations must be a non-empty tuple")
        if any(not isinstance(item, str) or not item for item in self.limitations):
            raise PlayerMarkerReplayError("limitations must contain non-empty strings")
        if self.schema_version != PLAYER_MARKER_REPLAY_SCHEMA_VERSION:
            raise PlayerMarkerReplayError("schema_version mismatch")
        if self.report_type != PLAYER_MARKER_REPLAY_REPORT_TYPE:
            raise PlayerMarkerReplayError("report_type mismatch")
        if self.scope != PLAYER_MARKER_REPLAY_SCOPE or self.truth_scope != TRUTH_SCOPE:
            raise PlayerMarkerReplayError("replay scope/truth_scope mismatch")
        report_id = self.report_id or f"player-marker-replay-{self.manifest_digest[:24]}"
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", report_id) is None:
            raise PlayerMarkerReplayError("report_id must be portable")
        object.__setattr__(self, "report_id", report_id)
        if self.sample_count != len(self.runs[0].samples):
            raise PlayerMarkerReplayError("sample_count does not match run samples")
        if tuple(run.run_index for run in self.runs) != (1, 2, 3):
            raise PlayerMarkerReplayError("runs must be indexed 1, 2, 3")
        if any(len(run.samples) != self.sample_count for run in self.runs):
            raise PlayerMarkerReplayError("sample_count does not match every run")
        if any(run.sample_order_digest != self.sample_order_digest for run in self.runs):
            raise PlayerMarkerReplayError("run sample order digest mismatch")
        expected_status = "PASS" if self.deterministic else "FAIL"
        if self.status != expected_status:
            raise PlayerMarkerReplayError("report status contradicts deterministic flag")
        expected_report_digest = _digest(self._body_dict())
        if self.report_digest != expected_report_digest:
            raise PlayerMarkerReplayError("report_digest mismatch")

    def _body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_type": self.report_type,
            "report_id": self.report_id,
            "scope": self.scope,
            "truth_scope": self.truth_scope,
            "source_commit": self.source_commit,
            "manifest_digest": self.manifest_digest,
            "corpus_digest": self.corpus_digest,
            "config_digest": self.config_digest,
            "as_of_ns": self.as_of_ns,
            "sample_order_digest": self.sample_order_digest,
            "sample_count": self.sample_count,
            "repeat_count": self.repeat_count,
            "deterministic": self.deterministic,
            "status": self.status,
            "event_tape_digest": self.event_tape_digest,
            "zero_input_audit": to_json_dict(self.zero_input_audit),
            "limitations": list(self.limitations),
            "runs": [item.to_dict() for item in self.runs],
        }

    def to_dict(self) -> dict[str, Any]:
        body = self._body_dict()
        body["report_digest"] = self.report_digest
        return body

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=False)

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json() + "\n", encoding="utf-8")
        return target

    def assert_deterministic(self) -> None:
        if not self.deterministic:
            raise PlayerMarkerReplayDeterminismError(
                "three player-marker replay runs differed item by item"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlayerMarkerReplayReport:
        data = _mapping(value, "player marker replay report")
        expected_keys = {
            "schema_version",
            "report_type",
            "report_id",
            "scope",
            "truth_scope",
            "source_commit",
            "manifest_digest",
            "corpus_digest",
            "config_digest",
            "as_of_ns",
            "sample_order_digest",
            "sample_count",
            "repeat_count",
            "deterministic",
            "status",
            "event_tape_digest",
            "zero_input_audit",
            "limitations",
            "runs",
            "report_digest",
        }
        if set(data) != expected_keys:
            raise PlayerMarkerReplayError("report keys are not exact")
        raw_runs = data["runs"]
        raw_limits = data["limitations"]
        if not isinstance(raw_runs, list) or not isinstance(raw_limits, list):
            raise PlayerMarkerReplayError("report runs/limitations must be arrays")
        return cls(
            schema_version=data["schema_version"],
            report_type=data["report_type"],
            report_id=data["report_id"],
            scope=data["scope"],
            truth_scope=data["truth_scope"],
            source_commit=data["source_commit"],
            manifest_digest=data["manifest_digest"],
            corpus_digest=data["corpus_digest"],
            config_digest=data["config_digest"],
            as_of_ns=data["as_of_ns"],
            sample_order_digest=data["sample_order_digest"],
            sample_count=data["sample_count"],
            repeat_count=data["repeat_count"],
            deterministic=data["deterministic"],
            status=data["status"],
            event_tape_digest=data["event_tape_digest"],
            zero_input_audit=_mapping(data["zero_input_audit"], "zero_input_audit"),
            limitations=tuple(raw_limits),
            runs=tuple(PlayerMarkerReplayRun.from_dict(item) for item in raw_runs),
            report_digest=data["report_digest"],
        )


@dataclass(frozen=True, slots=True)
class PlayerMarkerReplayConfig:
    """Fixed replay inputs shared by all three runs."""

    manifest: Mapping[str, Any] | str | Path
    truth_root: str | Path | None = None
    cas_root: str | Path | PixelStore | None = None
    event_tapes: tuple[str | Path | EventTape, ...] = ()
    source_commit: str | None = None
    config: Mapping[str, Any] | None = None
    config_digest: str | None = None
    as_of_ns: int = 0
    zero_input_audit: Mapping[str, Any] | None = None
    pixels_by_digest: Mapping[str, bytes] | None = None

    def __post_init__(self) -> None:
        ensure_time_ns(self.as_of_ns, "as_of_ns")
        object.__setattr__(self, "event_tapes", tuple(self.event_tapes))
        if self.source_commit is not None:
            _commit(self.source_commit, "source_commit")
        if self.config_digest is not None:
            _sha256(self.config_digest, "config_digest")
        if self.config is not None:
            object.__setattr__(self, "config", _strict_json(self.config, "config"))
        object.__setattr__(
            self,
            "zero_input_audit",
            _normalise_zero_input_audit(self.zero_input_audit),
        )
        if self.pixels_by_digest is not None:
            for digest, pixels in self.pixels_by_digest.items():
                _sha256(digest, "pixels_by_digest key")
                if not isinstance(pixels, bytes):
                    raise TypeError("pixels_by_digest values must be bytes")
            object.__setattr__(self, "pixels_by_digest", dict(self.pixels_by_digest))


@dataclass(frozen=True, slots=True)
class _CorpusSample:
    sample: Mapping[str, Any]
    truth: Mapping[str, Any]
    spec: PixelSpec

    @property
    def sample_id(self) -> str:
        return cast(str, self.sample["sample_id"])

    @property
    def truth_id(self) -> str:
        return cast(str, self.sample["truth_id"])

    @property
    def pixel_digest(self) -> str:
        return cast(str, self.sample["pixel_digest"])

    @property
    def session_id(self) -> str:
        return cast(str, self.sample["session_id"])

    @property
    def sequence(self) -> int:
        return cast(int, self.sample["sequence"])

    @property
    def wrong_size(self) -> bool:
        return bool(self.sample["wrong_size_negative"])

    @property
    def expected_status(self) -> str:
        return cast(str, self.truth["expected_status"])


class PlayerMarkerReplayRunner:
    """Replay verified accepted corpus packets through an injected extractor."""

    def __init__(
        self,
        manifest: Mapping[str, Any] | str | Path | PlayerMarkerReplayConfig,
        *,
        truth_root: str | Path | None = None,
        cas_root: str | Path | PixelStore | None = None,
        event_tapes: Sequence[str | Path | EventTape] | None = None,
        event_tape_paths: Sequence[str | Path | EventTape] | None = None,
        extractor: PlayerMarkerExtractor | Callable[..., object] | None = None,
        source_commit: str | None = None,
        config: Mapping[str, Any] | None = None,
        config_digest: str | None = None,
        as_of_ns: int = 0,
        zero_input_audit: Mapping[str, Any] | None = None,
        pixels_by_digest: Mapping[str, bytes] | None = None,
    ) -> None:
        if isinstance(manifest, PlayerMarkerReplayConfig):
            if any(
                value is not None
                for value in (
                    truth_root,
                    cas_root,
                    event_tapes,
                    event_tape_paths,
                    source_commit,
                    config,
                    config_digest,
                    pixels_by_digest,
                )
            ) or as_of_ns != 0 or zero_input_audit is not None:
                raise PlayerMarkerReplayError("config object cannot be combined with overrides")
            self.config = manifest
        else:
            tapes = event_tapes if event_tapes is not None else event_tape_paths
            if event_tapes is not None and event_tape_paths is not None:
                raise PlayerMarkerReplayError("provide event_tapes or event_tape_paths, not both")
            self.config = PlayerMarkerReplayConfig(
                manifest=manifest,
                truth_root=truth_root,
                cas_root=cas_root,
                event_tapes=tuple(() if tapes is None else tapes),
                source_commit=source_commit,
                config=config,
                config_digest=config_digest,
                as_of_ns=as_of_ns,
                zero_input_audit=zero_input_audit,
                pixels_by_digest=pixels_by_digest,
            )
        self.extractor = extractor
        self._manifest, self._manifest_digest, self._samples = self._load_corpus()
        self._event_records = self._load_events()
        self._sample_order_digest = _digest([item.sample_id for item in self._samples])
        manifest_commit = _commit(self._manifest["source_commit"], "manifest.source_commit")
        if self.config.source_commit is not None and self.config.source_commit != manifest_commit:
            raise PlayerMarkerReplayError("source_commit does not match the corpus manifest")
        self.source_commit = manifest_commit
        if self.config.config_digest is not None:
            if self.config.config is not None and self.config.config_digest != _digest(
                self.config.config
            ):
                raise PlayerMarkerReplayError("config_digest does not match config")
            self.config_digest = self.config.config_digest
        else:
            config_value: Any = self.config.config
            if config_value is None:
                config_value = {
                    "manifest_digest": self._manifest_digest,
                    "source_commit": self.source_commit,
                    "as_of_ns": self.config.as_of_ns,
                }
            elif not isinstance(config_value, Mapping):  # pragma: no cover - dataclass guard
                raise PlayerMarkerReplayError("config must be a mapping")
            self.config_digest = _digest(config_value)
        self._event_tape_digest = self._compute_event_tape_digest()
        self._zero_input_audit = _normalise_zero_input_audit(self.config.zero_input_audit)

    @property
    def manifest(self) -> Mapping[str, Any]:
        return self._manifest

    @property
    def sample_order(self) -> tuple[str, ...]:
        return tuple(item.sample_id for item in self._samples)

    def _load_corpus(self) -> tuple[Mapping[str, Any], str, tuple[_CorpusSample, ...]]:
        manifest_path: Path | None = None
        manifest: Mapping[str, Any]
        if isinstance(self.config.manifest, str | Path):
            manifest_path = Path(self.config.manifest)
            try:
                manifest = load_strict_json(manifest_path)
            except (OSError, ValueError) as exc:
                raise PlayerMarkerReplayError("invalid corpus manifest") from exc
            raw = manifest_path.read_bytes()
            if raw != canonical_json(manifest) + b"\n":
                raise PlayerMarkerReplayError("corpus manifest must use canonical JSON plus one LF")
        else:
            manifest = _mapping(self.config.manifest, "manifest")
        truth_root = (
            Path(self.config.truth_root)
            if self.config.truth_root is not None
            else (manifest_path.parent if manifest_path is not None else None)
        )
        if truth_root is None:
            raise PlayerMarkerReplayError("truth_root is required for an in-memory manifest")
        try:
            verify_corpus_manifest(
                manifest,
                truth_root=truth_root,
                cas_root=(
                    self.config.cas_root.root
                    if isinstance(self.config.cas_root, PixelStore)
                    else self.config.cas_root
                ),
                minimum_samples=1,
                minimum_unique_pixels=1,
                minimum_sessions=1,
            )
        except (OSError, ValueError, TypeError) as exc:
            raise PlayerMarkerReplayError("corpus manifest verification failed") from exc
        raw_samples = manifest.get("samples")
        if not isinstance(raw_samples, list) or not raw_samples:
            raise PlayerMarkerReplayError("manifest samples must be a non-empty array")
        loaded: list[_CorpusSample] = []
        for index, raw_sample in enumerate(raw_samples):
            sample = _mapping(raw_sample, f"samples[{index}]")
            truth_path_value = sample.get("truth_path")
            if not isinstance(truth_path_value, str):
                raise PlayerMarkerReplayError("sample truth_path must be a string")
            truth_path = truth_root / Path(truth_path_value.replace("\\", "/"))
            try:
                truth = load_strict_json(truth_path)
                spec = verify_truth_record(truth)
            except (OSError, ValueError, TypeError) as exc:
                raise PlayerMarkerReplayError(
                    f"truth verification failed for sample index {index}"
                ) from exc
            loaded.append(
                _CorpusSample(
                    sample=cast(Mapping[str, Any], freeze_json_value(sample)),
                    truth=cast(Mapping[str, Any], freeze_json_value(truth)),
                    spec=spec,
                )
            )
        return (
            cast(Mapping[str, Any], freeze_json_value(manifest)),
            # ``corpus_digest`` is the manifest's self-excluded canonical
            # digest (the same convention used by ``verify_corpus_manifest``).
            # Reuse that preimage for the report's explicit manifest binding;
            # hashing the stored self-digest again would create a second,
            # needlessly ambiguous identity for the same artifact.
            corpus_canonical_digest(manifest, omit=("corpus_digest",)),
            tuple(loaded),
        )

    def _load_events(self) -> tuple[EventRecord, ...]:
        records: list[EventRecord] = []
        for source in self.config.event_tapes:
            try:
                if isinstance(source, EventTape):
                    records.extend(source.read_all())
                else:
                    records.extend(EventTape(source).read_all())
            except (OSError, ValueError, TypeError) as exc:
                raise PlayerMarkerReplayError("event tape verification failed") from exc
        return tuple(records)

    def _compute_event_tape_digest(self) -> str:
        # Bind only portable event identities in corpus order.  Tape paths and
        # full payloads never enter the public report.
        by_truth: dict[str, list[str]] = {}
        for record in self._event_records:
            truth_id = record.payload.get("truth_id")
            if isinstance(truth_id, str):
                by_truth.setdefault(truth_id, []).append(record.record_hash)
        entries = [
            {
                "sample_id": item.sample_id,
                "record_hashes": by_truth.get(item.truth_id, []),
            }
            for item in self._samples
        ]
        return _digest(
            {
                "record_hashes": [record.record_hash for record in self._event_records],
                "sample_bindings": entries,
            }
        )

    def _records_for(self, sample: _CorpusSample) -> tuple[EventRecord, ...]:
        return tuple(
            record
            for record in self._event_records
            if record.session_id == sample.session_id
            and record.payload.get("truth_id") == sample.truth_id
        )

    @staticmethod
    def _accepted_records(
        sample: _CorpusSample,
        records: Sequence[EventRecord],
    ) -> tuple[EventRecord, ...]:
        accepted: list[EventRecord] = []
        for record in records:
            payload = record.payload
            if record.event_type != "frame.accepted":
                continue
            if payload.get("truth_scope") != TRUTH_SCOPE:
                continue
            if payload.get("admission_status") != "accepted":
                continue
            if (
                payload.get("fault_latched") is not False
                or payload.get("plan_suppressed") is not False
            ):
                continue
            if payload.get("truth_pixel_digest") != sample.pixel_digest:
                continue
            if payload.get("pixel_digest") != sample.pixel_digest:
                continue
            # Event Tape frame ids are the packet's source sequence at this
            # boundary.  A re-signed packet for another frame is not eligible
            # merely because its pixel digest happens to match this row.
            if record.frame_id != sample.sequence:
                continue
            accepted.append(record)
        return tuple(accepted)

    @staticmethod
    def _accepted_like_records(records: Sequence[EventRecord]) -> tuple[EventRecord, ...]:
        """Return accepted-shaped packets before digest/lineage filtering."""

        return tuple(
            record
            for record in records
            if record.event_type == "frame.accepted"
            and record.payload.get("admission_status") == "accepted"
        )

    @staticmethod
    def _accepted_record(
        sample: _CorpusSample,
        records: Sequence[EventRecord],
    ) -> EventRecord | None:
        """Return the unique accepted packet, retaining a small API seam."""

        accepted = PlayerMarkerReplayRunner._accepted_records(sample, records)
        return accepted[0] if len(accepted) == 1 else None

    def _pixel_bytes(self, sample: _CorpusSample) -> bytes:
        if (
            self.config.pixels_by_digest is not None
            and sample.pixel_digest in self.config.pixels_by_digest
        ):
            data = self.config.pixels_by_digest[sample.pixel_digest]
            try:
                validate_pixels(sample.spec, data)
                if pixel_digest(sample.spec, data) != sample.pixel_digest:
                    raise ValueError("pixel digest does not match sample")
            except (TypeError, ValueError) as exc:
                raise PlayerMarkerReplayError("injected pixels failed Pixel V1 validation") from exc
            return data
        store: PixelStore | None
        if isinstance(self.config.cas_root, PixelStore):
            store = self.config.cas_root
        elif self.config.cas_root is not None:
            store = PixelStore(self.config.cas_root)
        else:
            store = None
        if store is None:
            raise PlayerMarkerReplayError("CAS root or pixels_by_digest is required")
        try:
            return store.read(sample.pixel_digest, sample.spec)
        except (OSError, ValueError, TypeError) as exc:
            raise PlayerMarkerReplayError("accepted packet pixels are unavailable") from exc

    @staticmethod
    def _call_extractor(extractor: Callable[..., object], frame: PlayerMarkerFrame) -> object:
        """Invoke common one/two/three argument integration spellings."""

        try:
            signature = inspect.signature(extractor)
        except (TypeError, ValueError):
            return extractor(frame)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
        if has_varargs or len(positional) <= 1:
            return extractor(frame)
        if len(positional) == 2:
            return extractor(frame, frame.pixels)
        return extractor(frame, frame.pixels, frame.pixel_spec)

    def _extract(self, frame: PlayerMarkerFrame, event: EventRecord) -> PlayerMarkerReplaySample:
        event_evidence_digest = _digest(
            {"record_hash": event.record_hash, "pixel_digest": frame.pixel_digest}
        )
        if self.extractor is None:
            return PlayerMarkerReplaySample.build(
                sample_id=frame.sample_id,
                status="fault",
                evidence_digest=event_evidence_digest,
                fault="extractor_missing",
            )
        try:
            extract_method = getattr(self.extractor, "extract", None)
            raw_result = (
                self._call_extractor(cast(Callable[..., object], extract_method), frame)
                if callable(extract_method)
                else self._call_extractor(cast(Callable[..., object], self.extractor), frame)
            )
        except Exception:
            return PlayerMarkerReplaySample.build(
                sample_id=frame.sample_id,
                status="fault",
                evidence_digest=event_evidence_digest,
                fault="extractor_error",
            )

        status = "detected"
        fault: str | None = None
        candidate: object | None = raw_result
        evidence: object | None = None
        declared_candidate_digest: str | None = None
        if isinstance(raw_result, PlayerMarkerExtraction):
            status = _status_token(raw_result.status)
            candidate = raw_result.candidate
            evidence = raw_result.evidence
            fault = raw_result.fault
        elif isinstance(raw_result, Mapping):
            values = raw_result
            if "candidate" in values or "candidate_digest" in values or "status" in values:
                status = _status_token(values.get("status", "detected"))
                candidate = values.get("candidate")
                if "candidate_digest" in values:
                    raw_candidate_digest = values.get("candidate_digest")
                    if raw_candidate_digest is not None:
                        if not isinstance(raw_candidate_digest, str) or _SHA256_RE.fullmatch(
                            raw_candidate_digest
                        ) is None:
                            return PlayerMarkerReplaySample.build(
                                sample_id=frame.sample_id,
                                status="fault",
                                evidence_digest=event_evidence_digest,
                                fault="extractor_result_invalid",
                            )
                        declared_candidate_digest = raw_candidate_digest
                        if candidate is None:
                            candidate = raw_candidate_digest
                evidence = values.get("evidence", values.get("evidence_digest"))
                fault = _fault_token(values.get("fault"))
            else:
                candidate = values
        elif any(hasattr(raw_result, field) for field in ("candidate", "evidence", "status")):
            # Structural adapter for result dataclasses from another module
            # (for example, the in-development minimap detector).  This keeps
            # the replay seam import-free while preserving candidate/evidence
            # lineage checks below.
            status = _status_token(getattr(raw_result, "status", "detected"))
            candidate = getattr(raw_result, "candidate", None)
            evidence = getattr(raw_result, "evidence", None)
            fault = _fault_token(getattr(raw_result, "fault", None))
        if candidate is None:
            status = "no_marker"
        elif status == "accepted" or status not in {"detected", "no_marker", "fault"}:
            status = "detected"
        if fault is not None:
            if fault not in _FAULTS:
                fault = "extractor_result_invalid"
            status = "fault"
        candidate_digest: str | None = None
        if candidate is not None:
            try:
                if isinstance(candidate, PlayerCandidate):
                    if (
                        candidate.session_id != frame.session_id
                        or candidate.source_id != frame.source_id
                        or candidate.source_frame_id != frame.frame_id
                        or candidate.observed_at_ns != frame.observed_at_ns
                        or candidate.pixel_digest != frame.pixel_digest
                    ):
                        return PlayerMarkerReplaySample.build(
                            sample_id=frame.sample_id,
                            status="fault",
                            evidence_digest=event_evidence_digest,
                            fault="candidate_lineage_mismatch",
                        )
                    candidate_digest = candidate.digest
                    if evidence is None:
                        evidence = candidate.evidence_digest
                elif isinstance(candidate, str) and _SHA256_RE.fullmatch(candidate) is not None:
                    candidate_digest = candidate
                else:
                    candidate_digest = _value_digest(candidate, "extractor candidate")
                if (
                    declared_candidate_digest is not None
                    and candidate_digest != declared_candidate_digest
                ):
                    return PlayerMarkerReplaySample.build(
                        sample_id=frame.sample_id,
                        status="fault",
                        evidence_digest=event_evidence_digest,
                        fault="extractor_result_invalid",
                    )
            except (PlayerMarkerReplayError, TypeError, ValueError):
                return PlayerMarkerReplaySample.build(
                    sample_id=frame.sample_id,
                    status="fault",
                    evidence_digest=event_evidence_digest,
                    fault="extractor_result_invalid",
                )
        evidence_digest = event_evidence_digest
        if evidence is not None:
            try:
                if isinstance(evidence, str) and _SHA256_RE.fullmatch(evidence) is not None:
                    evidence_digest = evidence
                else:
                    evidence_digest = _value_digest(evidence, "extractor evidence")
            except (PlayerMarkerReplayError, TypeError, ValueError):
                return PlayerMarkerReplaySample.build(
                    sample_id=frame.sample_id,
                    status="fault",
                    candidate_digest=candidate_digest,
                    evidence_digest=event_evidence_digest,
                    fault="extractor_result_invalid",
                )
        if isinstance(candidate, PlayerCandidate) and candidate.evidence_digest != evidence_digest:
            return PlayerMarkerReplaySample.build(
                sample_id=frame.sample_id,
                status="fault",
                candidate_digest=candidate_digest,
                evidence_digest=evidence_digest,
                fault="extractor_result_invalid",
            )
        return PlayerMarkerReplaySample.build(
            sample_id=frame.sample_id,
            status=status,
            candidate_digest=candidate_digest,
            evidence_digest=evidence_digest,
            fault=fault,
        )

    def _run_once(self, run_index: int) -> PlayerMarkerReplayRun:
        rows: list[PlayerMarkerReplaySample] = []
        for sample in self._samples:
            records = self._records_for(sample)
            # Wrong-size is a negative admission fixture.  This branch is
            # intentionally before CAS access and before the extractor call.
            if sample.wrong_size or sample.expected_status != "accepted":
                fault = "frame_size_changed" if sample.wrong_size else sample.expected_status
                if fault not in _FAULTS:
                    fault = "admission_missing"
                evidence = None
                if records:
                    evidence = _digest(
                        {"record_hashes": sorted(record.record_hash for record in records)}
                    )
                rows.append(
                    PlayerMarkerReplaySample.build(
                        sample_id=sample.sample_id,
                        status="rejected",
                        evidence_digest=evidence,
                        fault=fault,
                    )
                )
                continue
            accepted_records = self._accepted_records(sample, records)
            accepted_like = self._accepted_like_records(records)
            if len(accepted_like) > 1:
                rows.append(
                    PlayerMarkerReplaySample.build(
                        sample_id=sample.sample_id,
                        status="rejected",
                        evidence_digest=_digest(
                            {"record_hashes": sorted(record.record_hash for record in records)}
                        ),
                        fault="ambiguous_packet",
                    )
                )
                continue
            if accepted_like and not accepted_records:
                rows.append(
                    PlayerMarkerReplaySample.build(
                        sample_id=sample.sample_id,
                        status="rejected",
                        evidence_digest=_digest(
                            {"record_hashes": sorted(record.record_hash for record in records)}
                        ),
                        fault="packet_lineage_mismatch",
                    )
                )
                continue
            accepted_record = accepted_records[0] if accepted_records else None
            if accepted_record is None:
                fault = "admission_missing"
                accepted_like_missing = [
                    record
                    for record in records
                    if record.event_type == "frame.accepted"
                    and record.payload.get("admission_status") == "accepted"
                ]
                if accepted_like_missing or any(
                    record.payload.get("truth_scope") != TRUTH_SCOPE for record in records
                ):
                    fault = "packet_lineage_mismatch"
                rows.append(
                    PlayerMarkerReplaySample.build(
                        sample_id=sample.sample_id,
                        status="rejected",
                        evidence_digest=(
                            None
                            if not records
                            else _digest(
                                {
                                    "record_hashes": sorted(
                                        record.record_hash for record in records
                                    )
                                }
                            )
                        ),
                        fault=fault,
                    )
                )
                continue
            try:
                pixels = self._pixel_bytes(sample)
            except PlayerMarkerReplayError:
                rows.append(
                    PlayerMarkerReplaySample.build(
                        sample_id=sample.sample_id,
                        status="fault",
                        evidence_digest=_digest({"record_hash": accepted_record.record_hash}),
                        fault="pixel_unavailable",
                    )
                )
                continue
            frame = PlayerMarkerFrame(
                sample_id=sample.sample_id,
                truth_id=sample.truth_id,
                session_id=sample.session_id,
                source_id=cast(str, sample.truth["source_id"]),
                sequence=sample.sequence,
                frame_id=accepted_record.frame_id,
                observed_at_ns=accepted_record.recorded_at_ns,
                as_of_ns=self.config.as_of_ns,
                pixel_digest=sample.pixel_digest,
                pixel_spec=sample.spec,
                pixels=pixels,
                event_record_hash=accepted_record.record_hash,
            )
            rows.append(self._extract(frame, accepted_record))
        return PlayerMarkerReplayRun.build(
            run_index=run_index,
            sample_order_digest=self._sample_order_digest,
            samples=rows,
        )

    def run(self, run_index: int = 1) -> PlayerMarkerReplayRun:
        ensure_non_negative_int(run_index, "run_index")
        if run_index < 1:
            raise PlayerMarkerReplayError("run_index must be >= 1")
        return self._run_once(run_index)

    replay = run

    def run_repeated(
        self,
        repetitions: int = PLAYER_MARKER_REPLAY_REPEAT_COUNT,
        *,
        source_commit: str | None = None,
        config_digest: str | None = None,
        as_of_ns: int | None = None,
    ) -> PlayerMarkerReplayReport:
        if repetitions != PLAYER_MARKER_REPLAY_REPEAT_COUNT:
            raise PlayerMarkerReplayError("G1-LOC-003B replay requires exactly three runs")
        if (
            source_commit is not None
            and _commit(source_commit, "source_commit") != self.source_commit
        ):
            raise PlayerMarkerReplayError("source_commit does not match the configured manifest")
        if (
            config_digest is not None
            and _sha256(config_digest, "config_digest") != self.config_digest
        ):
            raise PlayerMarkerReplayError("config_digest does not match the configured replay")
        if as_of_ns is not None:
            ensure_time_ns(as_of_ns, "as_of_ns")
            if as_of_ns != self.config.as_of_ns:
                raise PlayerMarkerReplayError("as_of_ns does not match the configured replay")
        runs = tuple(self._run_once(index) for index in range(1, repetitions + 1))
        deterministic = all(run.samples == runs[0].samples for run in runs[1:])
        report_id = f"player-marker-replay-{self._manifest_digest[:24]}"
        report_body = {
            "schema_version": PLAYER_MARKER_REPLAY_SCHEMA_VERSION,
            "report_type": PLAYER_MARKER_REPLAY_REPORT_TYPE,
            "report_id": report_id,
            "scope": PLAYER_MARKER_REPLAY_SCOPE,
            "truth_scope": TRUTH_SCOPE,
            "source_commit": self.source_commit,
            "manifest_digest": self._manifest_digest,
            "corpus_digest": cast(str, self._manifest["corpus_digest"]),
            "config_digest": self.config_digest,
            "as_of_ns": self.config.as_of_ns,
            "sample_order_digest": self._sample_order_digest,
            "sample_count": len(self._samples),
            "repeat_count": repetitions,
            "deterministic": deterministic,
            "status": "PASS" if deterministic else "FAIL",
            "event_tape_digest": self._event_tape_digest,
            "zero_input_audit": self._zero_input_audit,
            "limitations": [
                "truth_scope is frame_ingestion_only; no marker accuracy claim is made.",
                "Only accepted admission packets are eligible for extractor invocation.",
                "Raw pixels, paths, and identity mappings are excluded from this report.",
            ],
            "runs": [run.to_dict() for run in runs],
        }
        # Compute the self-digest from the same canonical body used by the
        # immutable report.  ``report_digest`` itself is excluded from the
        # preimage.
        report_digest = _digest(report_body)
        return PlayerMarkerReplayReport(
            source_commit=self.source_commit,
            manifest_digest=self._manifest_digest,
            corpus_digest=cast(str, self._manifest["corpus_digest"]),
            config_digest=self.config_digest,
            as_of_ns=self.config.as_of_ns,
            sample_order_digest=self._sample_order_digest,
            sample_count=len(self._samples),
            repeat_count=repetitions,
            deterministic=deterministic,
            status="PASS" if deterministic else "FAIL",
            event_tape_digest=self._event_tape_digest,
            zero_input_audit=self._zero_input_audit,
            limitations=tuple(cast(list[str], report_body["limitations"])),
            runs=runs,
            report_digest=report_digest,
            report_id=report_id,
        )

    def run_three_times(self) -> PlayerMarkerReplayReport:
        return self.run_repeated(PLAYER_MARKER_REPLAY_REPEAT_COUNT)


def verify_player_marker_replay_report(
    payload: Mapping[str, Any] | PlayerMarkerReplayReport,
    *,
    source_commit: str | None = None,
    manifest_digest: str | None = None,
    config_digest: str | None = None,
    as_of_ns: int | None = None,
) -> None:
    """Validate report shape, digests, order, three-run equality, and privacy."""

    if isinstance(payload, PlayerMarkerReplayReport):
        data = payload.to_dict()
    else:
        data = dict(_mapping(payload, "report"))
    forbidden = _forbidden_keys(data)
    if forbidden:
        raise PlayerMarkerReplayError("report contains forbidden public fields")
    report = PlayerMarkerReplayReport.from_dict(data)
    if source_commit is not None and report.source_commit != _commit(
        source_commit, "source_commit"
    ):
        raise PlayerMarkerReplayError("report source_commit mismatch")
    if manifest_digest is not None and report.manifest_digest != _sha256(
        manifest_digest, "manifest_digest"
    ):
        raise PlayerMarkerReplayError("report manifest_digest mismatch")
    if config_digest is not None and report.config_digest != _sha256(
        config_digest, "config_digest"
    ):
        raise PlayerMarkerReplayError("report config_digest mismatch")
    if as_of_ns is not None:
        ensure_time_ns(as_of_ns, "as_of_ns")
        if report.as_of_ns != as_of_ns:
            raise PlayerMarkerReplayError("report as_of_ns mismatch")
    expected_ids = [item.sample_id for item in report.runs[0].samples]
    if _digest(expected_ids) != report.sample_order_digest:
        raise PlayerMarkerReplayError("sample_order_digest mismatch")
    if any([item.sample_id for item in run.samples] != expected_ids for run in report.runs[1:]):
        raise PlayerMarkerReplayError("runs do not preserve manifest sample order")
    if any(run.samples != report.runs[0].samples for run in report.runs[1:]):
        raise PlayerMarkerReplayDeterminismError("replay sample results differ across runs")
    if not report.deterministic:
        raise PlayerMarkerReplayDeterminismError("report deterministic flag is false")


def run_player_marker_replay(
    manifest: Mapping[str, Any] | str | Path | PlayerMarkerReplayConfig,
    *,
    extractor: PlayerMarkerExtractor | Callable[..., object] | None = None,
    **kwargs: Any,
) -> PlayerMarkerReplayReport:
    """Convenience entry point for the fixed three-run replay."""

    return PlayerMarkerReplayRunner(manifest, extractor=extractor, **kwargs).run_three_times()


# Concise aliases used by integrations while retaining the explicit public API.
MarkerReplayRunner = PlayerMarkerReplayRunner
MarkerReplayReport = PlayerMarkerReplayReport
MarkerReplayRun = PlayerMarkerReplayRun
MarkerReplaySample = PlayerMarkerReplaySample
ReplayConfig = PlayerMarkerReplayConfig


__all__ = [
    "PLAYER_MARKER_REPLAY_REPEAT_COUNT",
    "PLAYER_MARKER_REPLAY_REPORT_TYPE",
    "PLAYER_MARKER_REPLAY_REPORT_VERSION",
    "PLAYER_MARKER_REPLAY_SCHEMA_VERSION",
    "PLAYER_MARKER_REPLAY_SCOPE",
    "MarkerExtractor",
    "MarkerReplayReport",
    "MarkerReplayRun",
    "MarkerReplayRunner",
    "MarkerReplaySample",
    "PlayerMarkerExtraction",
    "PlayerMarkerExtractor",
    "PlayerMarkerFrame",
    "PlayerMarkerReplayConfig",
    "PlayerMarkerReplayDeterminismError",
    "PlayerMarkerReplayError",
    "PlayerMarkerReplayReport",
    "PlayerMarkerReplayRun",
    "PlayerMarkerReplayRunner",
    "PlayerMarkerReplaySample",
    "ReplayConfig",
    "run_player_marker_replay",
    "verify_player_marker_replay_report",
]
