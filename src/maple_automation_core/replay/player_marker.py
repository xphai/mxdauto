"""Hash-only deterministic replay at the player-marker boundary.

The replay driver is intentionally independent of the marker implementation.
It verifies a frame corpus, Event Tape, accepted-frame ledger, calibration, and
CAS references, rebuilds the package's real :class:`FramePacket` envelope, and
then calls an injected read-only extractor.  The public result contains only
portable identifiers and digests.  ``truth_scope`` remains ingestion-only;
the report is not a marker-accuracy claim.
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

from maple_automation_core.capture.frame_source import (
    canonical_calibration_sha256,
    canonical_geometry_sha256,
)
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
    ensure_positive_int,
    ensure_time_ns,
    freeze_json_value,
    to_json_dict,
)
from maple_automation_core.domain.frame import (
    CaptureHealth,
    FramePacket,
    SourceGeometry,
)
from maple_automation_core.localization.player_localizer import PlayerCandidate
from maple_automation_core.replay.event_tape import EventRecord, EventTape
from maple_automation_core.replay.frame_corpus import (
    TRUTH_SCOPE,
    load_strict_json,
    verify_corpus_file,
    verify_corpus_manifest,
    verify_truth_record,
)
from maple_automation_core.replay.frame_corpus import (
    _privacy_findings as corpus_privacy_findings,
)
from maple_automation_core.replay.frame_corpus import (
    canonical_digest as corpus_canonical_digest,
)

PLAYER_MARKER_REPLAY_SCHEMA_VERSION = "1.0.0"
PLAYER_MARKER_REPLAY_REPORT_VERSION = PLAYER_MARKER_REPLAY_SCHEMA_VERSION
PLAYER_MARKER_REPLAY_REPORT_TYPE = "player_marker_replay"
PLAYER_MARKER_REPLAY_SCOPE = "G1-LOC-003B"
PLAYER_MARKER_REPLAY_REPEAT_COUNT = 3
PLAYER_MARKER_REPLAY_PROFILES = frozenset({"b2_gate", "contract_fixture"})
PLAYER_MARKER_REPLAY_TIMING_STRATEGY = (
    "observed_at_ns=ledger_received_at_ns_or_ledger_admitted_at_ns_or_event_recorded_at_ns;"
    "effective_now_ns=observed_at_ns+as_of_offset_ns;"
    "as_of_ns_alias=as_of_offset_ns"
)
PLAYER_MARKER_REPLAY_LIMITATIONS = (
    "truth_scope is frame_ingestion_only; no marker accuracy claim is made.",
    "Replay compares detector contract outputs only; it does not establish marker accuracy.",
    "Raw pixels, host paths, and identity mappings are excluded from this report.",
)
DEFAULT_MAX_AGE_NS = 10_000_000_000

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_CAS_RE = re.compile(r"^cas://sha256/[a-f0-9]{64}$")
_STATeless_DIGEST = "stateless"
_FAULTS = frozenset(
    {
        "admission_missing",
        "ambiguous_packet",
        "candidate_lineage_mismatch",
        "clock_domain_mismatch",
        "config_invalid",
        "calibration_mismatch",
        "detector_fault",
        "duplicate",
        "execution_invalid",
        "extraction_error",
        "extractor_error",
        "extractor_missing",
        "extractor_result_invalid",
        "frame_type",
        "frame_size_changed",
        "geometry_mismatch",
        "image_ref_mismatch",
        "no_frame",
        "out_of_order",
        "packet_lineage_mismatch",
        "pixel_hash_mismatch",
        "pixel_missing",
        "pixel_unavailable",
        "pixel_spec_mismatch",
        "roi_unconfigured",
        "session_mismatch",
        "source_error",
        "source_mismatch",
        "stale",
        "suppressed",
        "timestamp_mismatch",
        "timestamp_regression",
        "transform_mismatch",
        "truth_invalid",
    }
)
_STATUSES = frozenset({"detected", "no_marker", "rejected", "fault"})
_ZERO_INPUT_KEYS = frozenset(
    {
        "core_v2_real_input_call_count",
        "double_write_event_count",
        "failure_count",
        "input_owner",
        "keyboard_call_count",
        "mouse_call_count",
        "real_input_call_count",
        "real_input_enabled",
        "receiver_connect_count",
        "report_digest",
        "report_type",
        "schema_version",
        "source_commit",
        "status",
        "wheel_sha256",
        "window_write_count",
    }
)
_ZERO_COUNTER_KEYS = frozenset(
    {
        "core_v2_real_input_call_count",
        "double_write_event_count",
        "failure_count",
        "keyboard_call_count",
        "mouse_call_count",
        "real_input_call_count",
        "receiver_connect_count",
        "window_write_count",
    }
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
    """Raised when replay inputs or a report violate the contract."""


class PlayerMarkerReplayDeterminismError(PlayerMarkerReplayError):
    """Raised when the required three runs do not agree item by item."""


def _digest(value: Any) -> str:
    ensure_json_value(value, "digest payload")
    body: Mapping[str, Any]
    body = cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {"value": value}
    return sha256(canonical_json_bytes(body)).hexdigest()


_INVALID_STATE_DIGEST = _digest({"state": "invalid"})


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
    """Reduce an extractor value to a hashable, non-public representation."""

    if isinstance(value, PlayerCandidate):
        return value.to_dict()
    if isinstance(value, Mapping):
        return to_json_dict(_strict_json(value, field_name))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_json_dict(_strict_json(to_dict(), field_name))
        except (
            PlayerMarkerReplayError,
            TypeError,
            ValueError,
            AttributeError,
            RuntimeError,
            KeyError,
        ):
            pass
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        return {"bytes_sha256": sha256(raw).hexdigest(), "byte_count": len(raw)}
    try:
        return to_json_dict(_strict_json(value, field_name))
    except PlayerMarkerReplayError:
        declared = getattr(value, "digest", None)
        if isinstance(declared, str) and _SHA256_RE.fullmatch(declared) is not None:
            return {"declared_digest": declared}
        raise


def _value_digest(value: object, field_name: str) -> str:
    return _digest(_as_json(value, field_name))


def _status_token(value: object) -> str | None:
    token = getattr(value, "value", value)
    if not isinstance(token, str):
        token = str(token)
    token = token.lower()
    if token in {"candidate", "found", "detected", "accepted"}:
        return "detected"
    if token in {"none", "no_candidate", "no-marker", "no_marker", "not_detected"}:
        return "no_marker"
    if token in {"fault", "invalid", "error"}:
        return "fault"
    return None


def _fault_token(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        value = value.get("code")
    code = getattr(value, "code", value)
    token = getattr(code, "value", code)
    if not isinstance(token, str) or token not in _FAULTS:
        return None
    return token


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


def _assert_public_privacy(value: object) -> None:
    if _forbidden_keys(value):
        raise PlayerMarkerReplayError("report contains forbidden public fields")
    try:
        findings = corpus_privacy_findings(value)
    except (TypeError, ValueError) as exc:
        raise PlayerMarkerReplayError("report public JSON scan failed") from exc
    if findings:
        raise PlayerMarkerReplayError("report contains a path or identity-bearing string")


def _exception_type_digest(exc: BaseException) -> str:
    # The digest describes the exception type, not its run position.  This
    # keeps a stable exception type comparable while still exposing a type
    # change across runs through the sample digest.
    return _digest(
        {
            "exception_module": type(exc).__module__,
            "exception_qualname": type(exc).__qualname__,
        }
    )


def _sample_body(
    *,
    sample_id: str,
    status: str,
    candidate_digest: str | None,
    evidence_digest: str | None,
    detector_result_digest: str | None,
    detector_state_digest: str,
    detector_config_digest: str,
    exception_type_digest: str | None,
    fault: str | None,
    invoked: bool,
    as_of_offset_ns: int,
    effective_now_ns: int | None,
    observed_at_ns: int | None,
    generation: int,
) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "status": status,
        "candidate_digest": candidate_digest,
        "evidence_digest": evidence_digest,
        "detector_result_digest": detector_result_digest,
        "detector_state_digest": detector_state_digest,
        "detector_config_digest": detector_config_digest,
        "exception_type_digest": exception_type_digest,
        "fault": fault,
        "invoked": invoked,
        "as_of_offset_ns": as_of_offset_ns,
        # ``as_of_ns`` remains a wire-level compatibility alias.  The timing
        # strategy above makes its offset meaning explicit.
        "as_of_ns": as_of_offset_ns,
        "effective_now_ns": effective_now_ns,
        "observed_at_ns": observed_at_ns,
        "generation": generation,
    }


def _sample_result_digest(**body: Any) -> str:
    return _digest(_sample_body(**body))


@runtime_checkable
class PlayerMarkerExtractor(Protocol):
    """Structural seam for an extractor consuming a real ``FramePacket``."""

    def __call__(self, frame: FramePacket, **kwargs: Any) -> object:
        """Extract one marker from the packet; keyword timing is optional."""


MarkerExtractor = PlayerMarkerExtractor

# Kept as a source-compatibility name only.  There is no replay-specific frame
# class: detector input is always the package's real immutable FramePacket.
PlayerMarkerFrame = FramePacket


@dataclass(frozen=True, slots=True)
class PlayerMarkerExtraction:
    """Small generic result adapter for contract fixtures."""

    candidate: object | None = None
    evidence: object | None = None
    status: str = "detected"
    fault: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"detected", "no_marker", "accepted", "fault"}:
            raise ValueError("unsupported marker extraction status")
        if self.fault is not None and self.fault not in _FAULTS:
            raise ValueError("unsupported marker extraction fault")

    def to_dict(self) -> dict[str, Any]:
        """Return the complete strict result body used for replay hashing."""

        return {
            "candidate": None
            if self.candidate is None
            else _as_json(self.candidate, "extractor candidate"),
            "evidence": None
            if self.evidence is None
            else _as_json(self.evidence, "extractor evidence"),
            "status": self.status,
            "fault": self.fault,
        }


@dataclass(frozen=True, slots=True)
class PlayerMarkerReplaySample:
    """Hash-only result for one manifest sample."""

    sample_id: str
    status: str
    candidate_digest: str | None
    evidence_digest: str | None
    result_digest: str
    fault: str | None = None
    detector_result_digest: str | None = None
    detector_state_digest: str = ""
    detector_config_digest: str = ""
    exception_type_digest: str | None = None
    invoked: bool = False
    as_of_ns: int = 0
    observed_at_ns: int | None = None
    generation: int = 0
    as_of_offset_ns: int | None = None
    effective_now_ns: int | None = None

    def __post_init__(self) -> None:
        _portable_id(self.sample_id, "sample_id")
        if self.status not in _STATUSES:
            raise PlayerMarkerReplayError("sample status is unsupported")
        if self.status == "detected" and self.candidate_digest is None:
            raise PlayerMarkerReplayError("detected samples require candidate_digest")
        if self.status != "detected" and self.candidate_digest is not None:
            raise PlayerMarkerReplayError("non-detected samples cannot carry candidate_digest")
        if self.candidate_digest is not None:
            _sha256(self.candidate_digest, "candidate_digest")
        if self.evidence_digest is not None:
            _sha256(self.evidence_digest, "evidence_digest")
        if self.detector_result_digest is not None:
            _sha256(self.detector_result_digest, "detector_result_digest")
        _sha256(self.detector_state_digest, "detector_state_digest")
        _sha256(self.detector_config_digest, "detector_config_digest")
        if self.exception_type_digest is not None:
            _sha256(self.exception_type_digest, "exception_type_digest")
        if self.fault is not None and self.fault not in _FAULTS:
            raise PlayerMarkerReplayError("sample fault is unsupported")
        if not isinstance(self.invoked, bool):
            raise TypeError("invoked must be bool")
        ensure_time_ns(self.as_of_ns, "as_of_ns")
        offset = self.as_of_ns if self.as_of_offset_ns is None else self.as_of_offset_ns
        ensure_time_ns(offset, "as_of_offset_ns")
        if self.as_of_offset_ns is not None and self.as_of_ns not in (0, offset):
            raise PlayerMarkerReplayError("as_of_ns must equal as_of_offset_ns")
        object.__setattr__(self, "as_of_ns", offset)
        object.__setattr__(self, "as_of_offset_ns", offset)
        if self.observed_at_ns is not None:
            ensure_time_ns(self.observed_at_ns, "observed_at_ns")
        if self.observed_at_ns is None:
            if self.effective_now_ns is not None:
                raise PlayerMarkerReplayError(
                    "effective_now_ns requires observed_at_ns"
                )
        else:
            if self.effective_now_ns is None:
                raise PlayerMarkerReplayError(
                    "observed samples require effective_now_ns"
                )
            ensure_time_ns(self.effective_now_ns, "effective_now_ns")
            if self.effective_now_ns != self.observed_at_ns + offset:
                raise PlayerMarkerReplayError(
                    "effective_now_ns must equal observed_at_ns plus as_of_offset_ns"
                )
        ensure_non_negative_int(self.generation, "generation")
        expected = _sample_result_digest(
            sample_id=self.sample_id,
            status=self.status,
            candidate_digest=self.candidate_digest,
            evidence_digest=self.evidence_digest,
            detector_result_digest=self.detector_result_digest,
            detector_state_digest=self.detector_state_digest,
            detector_config_digest=self.detector_config_digest,
            exception_type_digest=self.exception_type_digest,
            fault=self.fault,
            invoked=self.invoked,
            as_of_offset_ns=offset,
            effective_now_ns=self.effective_now_ns,
            observed_at_ns=self.observed_at_ns,
            generation=self.generation,
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
        detector_result_digest: str | None = None,
        detector_state_digest: str,
        detector_config_digest: str,
        exception_type_digest: str | None = None,
        invoked: bool,
        observed_at_ns: int | None,
        generation: int,
        as_of_ns: int | None = None,
        as_of_offset_ns: int | None = None,
        effective_now_ns: int | None = None,
    ) -> PlayerMarkerReplaySample:
        if as_of_offset_ns is None:
            offset = 0 if as_of_ns is None else as_of_ns
        elif as_of_ns is None or as_of_ns == 0 or as_of_ns == as_of_offset_ns:
            offset = as_of_offset_ns
        else:
            raise PlayerMarkerReplayError("as_of_ns must equal as_of_offset_ns")
        body = _sample_body(
            sample_id=sample_id,
            status=status,
            candidate_digest=candidate_digest,
            evidence_digest=evidence_digest,
            detector_result_digest=detector_result_digest,
            detector_state_digest=detector_state_digest,
            detector_config_digest=detector_config_digest,
            exception_type_digest=exception_type_digest,
            fault=fault,
            invoked=invoked,
            as_of_offset_ns=offset,
            effective_now_ns=effective_now_ns,
            observed_at_ns=observed_at_ns,
            generation=generation,
        )
        return cls(
            sample_id=sample_id,
            status=status,
            candidate_digest=candidate_digest,
            evidence_digest=evidence_digest,
            result_digest=_digest(body),
            fault=fault,
            detector_result_digest=detector_result_digest,
            detector_state_digest=detector_state_digest,
            detector_config_digest=detector_config_digest,
            exception_type_digest=exception_type_digest,
            invoked=invoked,
            as_of_ns=offset,
            observed_at_ns=observed_at_ns,
            generation=generation,
            as_of_offset_ns=offset,
            effective_now_ns=effective_now_ns,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "status": self.status,
            "candidate_digest": self.candidate_digest,
            "evidence_digest": self.evidence_digest,
            "result_digest": self.result_digest,
            "detector_result_digest": self.detector_result_digest,
            "detector_state_digest": self.detector_state_digest,
            "detector_config_digest": self.detector_config_digest,
            "exception_type_digest": self.exception_type_digest,
            "fault": self.fault,
            "invoked": self.invoked,
            "as_of_offset_ns": self.as_of_offset_ns,
            "as_of_ns": self.as_of_ns,
            "effective_now_ns": self.effective_now_ns,
            "observed_at_ns": self.observed_at_ns,
            "generation": self.generation,
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
            "detector_result_digest",
            "detector_state_digest",
            "detector_config_digest",
            "exception_type_digest",
            "fault",
            "invoked",
            "as_of_offset_ns",
            "as_of_ns",
            "effective_now_ns",
            "observed_at_ns",
            "generation",
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
            detector_result_digest=data["detector_result_digest"],
            detector_state_digest=data["detector_state_digest"],
            detector_config_digest=data["detector_config_digest"],
            exception_type_digest=data["exception_type_digest"],
            invoked=data["invoked"],
            as_of_ns=data["as_of_ns"],
            observed_at_ns=data["observed_at_ns"],
            generation=data["generation"],
            as_of_offset_ns=data["as_of_offset_ns"],
            effective_now_ns=data["effective_now_ns"],
        )


@dataclass(frozen=True, slots=True)
class PlayerMarkerReplayRun:
    """One pass over the manifest's physical sample order."""

    run_index: int
    sample_order_digest: str
    samples: tuple[PlayerMarkerReplaySample, ...]
    run_digest: str

    def __post_init__(self) -> None:
        ensure_positive_int(self.run_index, "run_index")
        if self.run_index > PLAYER_MARKER_REPLAY_REPEAT_COUNT:
            raise PlayerMarkerReplayError("run_index must be at most three")
        _sha256(self.sample_order_digest, "sample_order_digest")
        if not isinstance(self.samples, tuple) or any(
            not isinstance(item, PlayerMarkerReplaySample) for item in self.samples
        ):
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


@dataclass(frozen=True, slots=True)
class _ReplaySemanticSummary:
    deterministic: bool
    execution_valid: bool
    execution_faults: tuple[str, ...]


def _recompute_replay_semantics(
    runs: Sequence[PlayerMarkerReplayRun],
    *,
    expected_wrong_size: Sequence[bool] | None,
    extractor_config_digest: str | None,
    as_of_offset_ns: int | None,
    generation: int | None,
) -> _ReplaySemanticSummary:
    """Derive validity from every sample rather than trusting report flags."""

    if not runs:
        raise PlayerMarkerReplayError("replay semantics require at least one run")
    first_samples = runs[0].samples
    if expected_wrong_size is not None and len(expected_wrong_size) != len(first_samples):
        raise PlayerMarkerReplayError("manifest/sample count mismatch")
    inferred_wrong_size = tuple(
        (
            item.status == "rejected"
            and item.fault == "frame_size_changed"
            and item.invoked is False
            and item.detector_result_digest is None
            and item.observed_at_ns is None
            and item.effective_now_ns is None
        )
        for item in first_samples
    )
    wrong_size = (
        tuple(expected_wrong_size)
        if expected_wrong_size is not None
        else inferred_wrong_size
    )
    deterministic = all(run.samples == first_samples for run in runs[1:])
    execution_valid = True
    faults: set[str] = set()

    for run in runs:
        if len(run.samples) != len(first_samples):
            deterministic = False
            execution_valid = False
            faults.add("execution_invalid")
            continue
        for index, sample in enumerate(run.samples):
            is_wrong_size = wrong_size[index]
            binding_valid = True
            if (
                extractor_config_digest is not None
                and sample.detector_config_digest != extractor_config_digest
            ):
                binding_valid = False
            if (
                as_of_offset_ns is not None
                and (
                    sample.as_of_offset_ns != as_of_offset_ns
                    or sample.as_of_ns != as_of_offset_ns
                )
            ):
                binding_valid = False
            if generation is not None and sample.generation != generation:
                binding_valid = False

            if is_wrong_size:
                valid = (
                    sample.status == "rejected"
                    and sample.fault == "frame_size_changed"
                    and sample.invoked is False
                    and sample.detector_result_digest is None
                    and sample.observed_at_ns is None
                    and sample.effective_now_ns is None
                )
            else:
                valid = (
                    sample.invoked is True
                    and sample.status in {"detected", "no_marker"}
                    and sample.fault is None
                    and sample.exception_type_digest is None
                    and sample.detector_result_digest is not None
                    and sample.observed_at_ns is not None
                    and sample.effective_now_ns is not None
                )
            if not binding_valid or not valid:
                execution_valid = False
                if sample.fault is not None and not (
                    is_wrong_size and sample.fault == "frame_size_changed"
                ):
                    faults.add(sample.fault)
                else:
                    faults.add("execution_invalid")

    return _ReplaySemanticSummary(
        deterministic=deterministic,
        execution_valid=execution_valid,
        execution_faults=tuple(sorted(faults)),
    )


def _verified_manifest_shape(
    manifest: Mapping[str, Any],
    *,
    manifest_path: Path | None = None,
    truth_root: Path | None = None,
    require_single_live_source: bool = False,
) -> tuple[list[str], tuple[bool, ...], tuple[str, ...]]:
    """Read the manifest's order/admission shape without exposing artifacts."""

    data = _mapping(manifest, "manifest")
    raw_samples = data.get("samples")
    if not isinstance(raw_samples, Sequence) or isinstance(raw_samples, str) or not raw_samples:
        raise PlayerMarkerReplayError("manifest samples must be a non-empty array")
    root = truth_root if truth_root is not None else (
        None if manifest_path is None else manifest_path.parent
    )
    order: list[str] = []
    wrong_size: list[bool] = []
    seen_sample_ids: set[str] = set()
    for index, raw_sample in enumerate(raw_samples):
        sample = _mapping(raw_sample, f"manifest sample[{index}]")
        sample_id = _portable_id(sample.get("sample_id"), f"manifest sample[{index}].sample_id")
        if sample_id in seen_sample_ids:
            raise PlayerMarkerReplayError("manifest sample IDs are not unique")
        seen_sample_ids.add(sample_id)
        wrong = sample.get("wrong_size_negative")
        if type(wrong) is not bool:
            raise PlayerMarkerReplayError("manifest wrong_size_negative must be boolean")
        order.append(sample_id)
        wrong_size.append(wrong)
        if root is None:
            continue
        truth_path_value = sample.get("truth_path")
        if not isinstance(truth_path_value, str):
            raise PlayerMarkerReplayError("manifest truth_path is required")
        relative = Path(truth_path_value.replace("\\", "/"))
        if (
            relative.is_absolute()
            or ":" in truth_path_value
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise PlayerMarkerReplayError("manifest truth_path is not normalized")
        try:
            truth = load_strict_json(root / relative)
            verify_truth_record(truth)
        except (OSError, ValueError, TypeError) as exc:
            raise PlayerMarkerReplayError("manifest truth verification failed") from exc
        if (
            truth.get("sample_id") != sample_id
            or truth.get("truth_id") != sample.get("truth_id")
            or truth.get("session_id") != sample.get("session_id")
            or truth.get("sequence") != sample.get("sequence")
            or truth.get("pixel_digest") != sample.get("pixel_digest")
        ):
            raise PlayerMarkerReplayError("manifest/truth identity binding mismatch")
        expected_status = truth.get("expected_status")
        expected = "frame_size_changed" if wrong else "accepted"
        if expected_status != expected:
            raise PlayerMarkerReplayError("manifest admission shape is unsupported")

    live_source_ids: set[str] = set()
    live_artifacts: set[str] = set()
    raw_sources = data.get("sources")
    if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, str):
        for raw_source in raw_sources:
            source = _mapping(raw_source, "manifest source")
            if source.get("locator_kind") == "live_session":
                live_source_ids.add(_portable_id(source.get("source_id"), "manifest source_id"))
                live_artifacts.add(
                    _sha256(source.get("artifact_sha256"), "manifest source.artifact_sha256")
                )
    if require_single_live_source and len(live_source_ids) != 1:
        raise PlayerMarkerReplayError(
            "b2_gate manifest must have exactly one live_session source"
        )
    return order, tuple(wrong_size), tuple(sorted(live_artifacts))


def _validate_zero_input_audit(
    value: Mapping[str, Any],
    *,
    expected_source_commit: str,
) -> dict[str, Any]:
    data = dict(_mapping(value, "zero_input_audit"))
    if set(data) != set(_ZERO_INPUT_KEYS):
        raise PlayerMarkerReplayError("zero_input_audit keys are not exact")
    if data["schema_version"] != "1.0.0":
        raise PlayerMarkerReplayError("zero_input_audit schema_version mismatch")
    if data["report_type"] != "b2_zero_input_audit":
        raise PlayerMarkerReplayError("zero_input_audit report_type mismatch")
    if data["status"] != "PASS" or data["input_owner"] != "legacy":
        raise PlayerMarkerReplayError("zero_input_audit is not a passing Legacy audit")
    if data["real_input_enabled"] is not False:
        raise PlayerMarkerReplayError("zero_input_audit enables real input")
    if data["source_commit"] != expected_source_commit:
        raise PlayerMarkerReplayError("zero_input_audit source_commit mismatch")
    _commit(data["source_commit"], "zero_input_audit.source_commit")
    _sha256(data["report_digest"], "zero_input_audit.report_digest")
    _sha256(data["wheel_sha256"], "zero_input_audit.wheel_sha256")
    for key in _ZERO_COUNTER_KEYS:
        if type(data[key]) is not int or data[key] != 0:
            raise PlayerMarkerReplayError("zero_input_audit contains a non-zero counter")
    body = {key: item for key, item in data.items() if key != "report_digest"}
    if _digest(body) != data["report_digest"]:
        raise PlayerMarkerReplayError("zero_input_audit report_digest mismatch")
    return cast(dict[str, Any], freeze_json_value(data))


@dataclass(frozen=True, slots=True)
class PlayerMarkerReplayReport:
    """Self-validating hash-only three-run replay report."""

    corpus_source_commit: str
    replay_source_commit: str
    manifest_digest: str
    corpus_digest: str
    extractor_artifact_digest: str
    extractor_config_digest: str
    as_of_ns: int
    timing_strategy: str
    sample_order_digest: str
    sample_count: int
    repeat_count: int
    deterministic: bool
    execution_valid: bool
    status: str
    event_tape_digest: str
    event_tape_index_artifact_digest: str | None
    accepted_ledger_digest: str
    calibration_artifact_digest: str
    zero_input_audit_artifact_digest: str
    zero_input_audit: Mapping[str, Any]
    execution_faults: tuple[str, ...]
    limitations: tuple[str, ...]
    runs: tuple[PlayerMarkerReplayRun, ...]
    report_digest: str
    verification_profile: str = "b2_gate"
    schema_version: str = PLAYER_MARKER_REPLAY_SCHEMA_VERSION
    report_type: str = PLAYER_MARKER_REPLAY_REPORT_TYPE
    scope: str = PLAYER_MARKER_REPLAY_SCOPE
    truth_scope: str = TRUTH_SCOPE
    report_id: str = ""
    # ``as_of_offset_ns`` is the explicit timing field.  ``as_of_ns`` is kept
    # as a compatibility alias and is required to carry the same offset.
    as_of_offset_ns: int | None = None
    generation: int = 0

    @property
    def config_digest(self) -> str:
        """Compatibility alias for the extractor configuration digest."""

        return self.extractor_config_digest

    @property
    def source_commit(self) -> str:
        """Compatibility alias for the corpus source commit."""

        return self.corpus_source_commit

    def __post_init__(self) -> None:
        _commit(self.corpus_source_commit, "corpus_source_commit")
        _commit(self.replay_source_commit, "replay_source_commit")
        for value, name in (
            (self.manifest_digest, "manifest_digest"),
            (self.corpus_digest, "corpus_digest"),
            (self.extractor_artifact_digest, "extractor_artifact_digest"),
            (self.extractor_config_digest, "extractor_config_digest"),
            (self.event_tape_digest, "event_tape_digest"),
            (self.accepted_ledger_digest, "accepted_ledger_digest"),
            (self.calibration_artifact_digest, "calibration_artifact_digest"),
            (
                self.zero_input_audit_artifact_digest,
                "zero_input_audit_artifact_digest",
            ),
            (self.sample_order_digest, "sample_order_digest"),
        ):
            _sha256(value, name)
        if self.event_tape_index_artifact_digest is not None:
            _sha256(
                self.event_tape_index_artifact_digest,
                "event_tape_index_artifact_digest",
            )
        ensure_time_ns(self.as_of_ns, "as_of_ns")
        offset = self.as_of_ns if self.as_of_offset_ns is None else self.as_of_offset_ns
        ensure_time_ns(offset, "as_of_offset_ns")
        if self.as_of_offset_ns is not None and self.as_of_ns not in (0, offset):
            raise PlayerMarkerReplayError("as_of_ns must equal as_of_offset_ns")
        object.__setattr__(self, "as_of_ns", offset)
        object.__setattr__(self, "as_of_offset_ns", offset)
        ensure_non_negative_int(self.generation, "generation")
        if self.timing_strategy != PLAYER_MARKER_REPLAY_TIMING_STRATEGY:
            raise PlayerMarkerReplayError("timing_strategy mismatch")
        ensure_positive_int(self.sample_count, "sample_count")
        if self.repeat_count != PLAYER_MARKER_REPLAY_REPEAT_COUNT:
            raise PlayerMarkerReplayError("G1-LOC-003B replay requires exactly three runs")
        if not isinstance(self.deterministic, bool) or not isinstance(self.execution_valid, bool):
            raise TypeError("deterministic and execution_valid must be bool")
        if self.status not in {"PASS", "FAIL"}:
            raise PlayerMarkerReplayError("report status is unsupported")
        if self.verification_profile not in PLAYER_MARKER_REPLAY_PROFILES:
            raise PlayerMarkerReplayError("verification_profile is unsupported")
        if (
            self.verification_profile == "b2_gate"
            and self.event_tape_index_artifact_digest is None
        ):
            raise PlayerMarkerReplayError(
                "b2_gate report requires event_tape_index_artifact_digest"
            )
        if self.schema_version != PLAYER_MARKER_REPLAY_SCHEMA_VERSION:
            raise PlayerMarkerReplayError("schema_version mismatch")
        if self.report_type != PLAYER_MARKER_REPLAY_REPORT_TYPE:
            raise PlayerMarkerReplayError("report_type mismatch")
        if self.scope != PLAYER_MARKER_REPLAY_SCOPE or self.truth_scope != TRUTH_SCOPE:
            raise PlayerMarkerReplayError("replay scope/truth_scope mismatch")
        if not isinstance(self.execution_faults, tuple) or any(
            fault not in _FAULTS for fault in self.execution_faults
        ):
            raise PlayerMarkerReplayError("execution_faults contain an unsupported value")
        if tuple(sorted(set(self.execution_faults))) != self.execution_faults:
            raise PlayerMarkerReplayError("execution_faults must be sorted and unique")
        if self.limitations != PLAYER_MARKER_REPLAY_LIMITATIONS:
            raise PlayerMarkerReplayError("limitations do not match the fixed contract")
        if not isinstance(self.runs, tuple) or len(self.runs) != self.repeat_count:
            raise PlayerMarkerReplayError("repeat_count must equal the number of runs")
        if any(not isinstance(item, PlayerMarkerReplayRun) for item in self.runs):
            raise TypeError("runs must contain PlayerMarkerReplayRun values")
        audit = _validate_zero_input_audit(
            self.zero_input_audit,
            expected_source_commit=self.corpus_source_commit,
        )
        object.__setattr__(self, "zero_input_audit", audit)
        if self.verification_profile == "b2_gate":
            expected_audit_artifact_digest = sha256(
                canonical_json(to_json_dict(audit)) + b"\n"
            ).hexdigest()
            if self.zero_input_audit_artifact_digest != expected_audit_artifact_digest:
                raise PlayerMarkerReplayError(
                    "b2_gate zero-input audit digest does not match embedded artifact"
                )
        report_id = self.report_id or f"player-marker-replay-{self.manifest_digest[:24]}"
        expected_id = f"player-marker-replay-{self.manifest_digest[:24]}"
        if report_id != expected_id:
            raise PlayerMarkerReplayError("report_id is not bound to manifest_digest")
        object.__setattr__(self, "report_id", report_id)
        if tuple(run.run_index for run in self.runs) != (1, 2, 3):
            raise PlayerMarkerReplayError("runs must be indexed 1, 2, 3")
        if self.sample_count != len(self.runs[0].samples):
            raise PlayerMarkerReplayError("sample_count does not match run samples")
        if any(len(run.samples) != self.sample_count for run in self.runs):
            raise PlayerMarkerReplayError("sample_count does not match every run")
        if any(run.sample_order_digest != self.sample_order_digest for run in self.runs):
            raise PlayerMarkerReplayError("run sample order digest mismatch")
        for run in self.runs:
            for sample in run.samples:
                if sample.detector_config_digest != self.extractor_config_digest:
                    raise PlayerMarkerReplayError(
                        "sample detector_config_digest is not report-bound"
                    )
                if (
                    sample.as_of_offset_ns != offset
                    or sample.as_of_ns != offset
                    or sample.generation != self.generation
                ):
                    raise PlayerMarkerReplayError(
                        "sample timing/generation is not report-bound"
                    )
        semantic = _recompute_replay_semantics(
            self.runs,
            expected_wrong_size=None,
            extractor_config_digest=self.extractor_config_digest,
            as_of_offset_ns=offset,
            generation=self.generation,
        )
        if (
            self.deterministic != semantic.deterministic
            or self.execution_valid != semantic.execution_valid
            or self.execution_faults != semantic.execution_faults
        ):
            raise PlayerMarkerReplayError("report semantic flags/faults are not recomputable")
        expected_status = "PASS" if self.deterministic and self.execution_valid else "FAIL"
        if self.status != expected_status:
            raise PlayerMarkerReplayError("report status contradicts validity flags")
        _assert_public_privacy(self._body_dict())
        if self.report_digest != _digest(self._body_dict()):
            raise PlayerMarkerReplayError("report_digest mismatch")

    def _body_dict(self) -> dict[str, Any]:
        body = {
            "schema_version": self.schema_version,
            "report_type": self.report_type,
            "report_id": self.report_id,
            "scope": self.scope,
            "truth_scope": self.truth_scope,
            "verification_profile": self.verification_profile,
            "corpus_source_commit": self.corpus_source_commit,
            "replay_source_commit": self.replay_source_commit,
            "manifest_digest": self.manifest_digest,
            "corpus_digest": self.corpus_digest,
            "extractor_artifact_digest": self.extractor_artifact_digest,
            "extractor_config_digest": self.extractor_config_digest,
            "as_of_offset_ns": self.as_of_offset_ns,
            "as_of_ns": self.as_of_ns,
            "generation": self.generation,
            "timing_strategy": self.timing_strategy,
            "sample_order_digest": self.sample_order_digest,
            "sample_count": self.sample_count,
            "repeat_count": self.repeat_count,
            "deterministic": self.deterministic,
            "execution_valid": self.execution_valid,
            "status": self.status,
            "event_tape_digest": self.event_tape_digest,
            "event_tape_index_artifact_digest": self.event_tape_index_artifact_digest,
            "accepted_ledger_digest": self.accepted_ledger_digest,
            "calibration_artifact_digest": self.calibration_artifact_digest,
            "zero_input_audit_artifact_digest": self.zero_input_audit_artifact_digest,
            "zero_input_audit": to_json_dict(self.zero_input_audit),
            "execution_faults": list(self.execution_faults),
            "limitations": list(self.limitations),
            "runs": [item.to_dict() for item in self.runs],
        }
        if (
            self.event_tape_index_artifact_digest is None
            and self.verification_profile == "contract_fixture"
        ):
            body.pop("event_tape_index_artifact_digest")
        return body

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
            "verification_profile",
            "corpus_source_commit",
            "replay_source_commit",
            "manifest_digest",
            "corpus_digest",
            "extractor_artifact_digest",
            "extractor_config_digest",
            "as_of_offset_ns",
            "as_of_ns",
            "generation",
            "timing_strategy",
            "sample_order_digest",
            "sample_count",
            "repeat_count",
            "deterministic",
            "execution_valid",
            "status",
            "event_tape_digest",
            "accepted_ledger_digest",
            "calibration_artifact_digest",
            "zero_input_audit_artifact_digest",
            "zero_input_audit",
            "execution_faults",
            "limitations",
            "runs",
            "report_digest",
        }
        if (
            not expected_keys.issubset(data)
            or set(data).difference(expected_keys | {"event_tape_index_artifact_digest"})
        ):
            raise PlayerMarkerReplayError("report keys are not exact")
        if (
            data.get("verification_profile") == "b2_gate"
            and "event_tape_index_artifact_digest" not in data
        ):
            raise PlayerMarkerReplayError(
                "b2_gate report requires event_tape_index_artifact_digest"
            )
        raw_runs = data["runs"]
        raw_limits = data["limitations"]
        raw_faults = data["execution_faults"]
        if not isinstance(raw_runs, list) or not isinstance(raw_limits, list):
            raise PlayerMarkerReplayError("report runs/limitations must be arrays")
        if not isinstance(raw_faults, list):
            raise PlayerMarkerReplayError("report execution_faults must be an array")
        if tuple(raw_limits) != PLAYER_MARKER_REPLAY_LIMITATIONS:
            raise PlayerMarkerReplayError("limitations do not match the fixed contract")
        return cls(
            schema_version=data["schema_version"],
            report_type=data["report_type"],
            report_id=data["report_id"],
            scope=data["scope"],
            truth_scope=data["truth_scope"],
            verification_profile=data["verification_profile"],
            corpus_source_commit=data["corpus_source_commit"],
            replay_source_commit=data["replay_source_commit"],
            manifest_digest=data["manifest_digest"],
            corpus_digest=data["corpus_digest"],
            extractor_artifact_digest=data["extractor_artifact_digest"],
            extractor_config_digest=data["extractor_config_digest"],
            as_of_ns=data["as_of_ns"],
            as_of_offset_ns=data["as_of_offset_ns"],
            generation=data["generation"],
            timing_strategy=data["timing_strategy"],
            sample_order_digest=data["sample_order_digest"],
            sample_count=data["sample_count"],
            repeat_count=data["repeat_count"],
            deterministic=data["deterministic"],
            execution_valid=data["execution_valid"],
            status=data["status"],
            event_tape_digest=data["event_tape_digest"],
            event_tape_index_artifact_digest=data.get("event_tape_index_artifact_digest"),
            accepted_ledger_digest=data["accepted_ledger_digest"],
            calibration_artifact_digest=data["calibration_artifact_digest"],
            zero_input_audit_artifact_digest=data["zero_input_audit_artifact_digest"],
            zero_input_audit=_mapping(data["zero_input_audit"], "zero_input_audit"),
            execution_faults=tuple(raw_faults),
            limitations=tuple(raw_limits),
            runs=tuple(PlayerMarkerReplayRun.from_dict(item) for item in raw_runs),
            report_digest=data["report_digest"],
        )


LedgerInput = str | Path | Mapping[str, Any] | Sequence[Mapping[str, Any]]
CalibrationInput = str | Path | Mapping[str, Any]
AuditInput = str | Path | Mapping[str, Any]
EventTapeIndexInput = str | Path | Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PlayerMarkerReplayConfig:
    """Fixed replay inputs shared by all three runs."""

    manifest: Mapping[str, Any] | str | Path
    verification_profile: str = "b2_gate"
    truth_root: str | Path | None = None
    cas_root: str | Path | PixelStore | None = None
    event_tapes: tuple[str | Path | EventTape, ...] = ()
    event_tape_index: EventTapeIndexInput | None = None
    event_tape_index_path: str | Path | None = None
    source_commit: str | None = None
    corpus_source_commit: str | None = None
    replay_source_commit: str | None = None
    config: Mapping[str, Any] | None = None
    config_digest: str | None = None
    extractor_artifact_digest: str | None = None
    accepted_frame_ledger: LedgerInput | None = None
    accepted_ledger: LedgerInput | None = None
    calibration: CalibrationInput | None = None
    calibration_artifact: CalibrationInput | None = None
    zero_input_audit_path: str | Path | None = None
    zero_input_audit_sha256: str | None = None
    as_of_ns: int = 0
    as_of_offset_ns: int = 0
    max_age_ns: int | None = None
    generation: int = 0
    zero_input_audit: AuditInput | None = None
    zero_input_audit_artifact_sha256: str | None = None
    pixels_by_digest: Mapping[str, bytes] | None = None

    def __post_init__(self) -> None:
        if self.verification_profile not in PLAYER_MARKER_REPLAY_PROFILES:
            raise PlayerMarkerReplayError("verification_profile is unsupported")
        ensure_time_ns(self.as_of_ns, "as_of_ns")
        offset = self.as_of_offset_ns
        ensure_time_ns(offset, "as_of_offset_ns")
        if offset != 0 and self.as_of_ns not in (0, offset):
            raise PlayerMarkerReplayError("as_of_ns must equal as_of_offset_ns")
        if self.as_of_ns != 0:
            offset = self.as_of_ns
        object.__setattr__(self, "as_of_ns", offset)
        object.__setattr__(self, "as_of_offset_ns", offset)
        ensure_non_negative_int(self.generation, "generation")
        if self.max_age_ns is not None:
            ensure_non_negative_int(self.max_age_ns, "max_age_ns")
        object.__setattr__(self, "event_tapes", tuple(self.event_tapes))
        if self.event_tape_index is not None and self.event_tape_index_path is not None:
            raise PlayerMarkerReplayError("provide one event-tape index input")
        if self.event_tape_index is None and self.event_tape_index_path is not None:
            object.__setattr__(self, "event_tape_index", self.event_tape_index_path)
        for value, name in (
            (self.source_commit, "source_commit"),
            (self.corpus_source_commit, "corpus_source_commit"),
            (self.replay_source_commit, "replay_source_commit"),
        ):
            if value is not None:
                _commit(value, name)
        if self.config_digest is not None:
            _sha256(self.config_digest, "config_digest")
        if self.extractor_artifact_digest is not None:
            _sha256(self.extractor_artifact_digest, "extractor_artifact_digest")
        if self.zero_input_audit_artifact_sha256 is not None:
            _sha256(self.zero_input_audit_artifact_sha256, "zero_input_audit_artifact_sha256")
        ledger_values = [
            value
            for value in (self.accepted_frame_ledger, self.accepted_ledger)
            if value is not None
        ]
        if len(ledger_values) > 1:
            raise PlayerMarkerReplayError("provide one accepted-frame ledger input")
        if self.accepted_frame_ledger is None and self.accepted_ledger is not None:
            object.__setattr__(self, "accepted_frame_ledger", self.accepted_ledger)
        calibration_values = [
            value
            for value in (self.calibration, self.calibration_artifact)
            if value is not None
        ]
        if len(calibration_values) > 1:
            raise PlayerMarkerReplayError("provide one calibration input")
        if self.calibration is None and self.calibration_artifact is not None:
            object.__setattr__(self, "calibration", self.calibration_artifact)
        if self.zero_input_audit_path is not None:
            if self.zero_input_audit is not None:
                raise PlayerMarkerReplayError("provide one zero-input audit input")
            object.__setattr__(self, "zero_input_audit", self.zero_input_audit_path)
        if self.zero_input_audit_sha256 is not None:
            if self.zero_input_audit_artifact_sha256 is not None:
                raise PlayerMarkerReplayError("provide one zero-input audit artifact digest")
            _sha256(self.zero_input_audit_sha256, "zero_input_audit_sha256")
            object.__setattr__(
                self,
                "zero_input_audit_artifact_sha256",
                self.zero_input_audit_sha256,
            )
        if self.config is not None:
            object.__setattr__(self, "config", _strict_json(self.config, "config"))
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
    def source_id(self) -> str:
        return cast(str, self.truth["source_id"])

    @property
    def wrong_size(self) -> bool:
        return bool(self.sample["wrong_size_negative"])

    @property
    def expected_status(self) -> str:
        return cast(str, self.truth["expected_status"])


@dataclass(frozen=True, slots=True)
class _Calibration:
    geometry: SourceGeometry
    transform_version: str
    calibration_sha256: str
    artifact_digest: str
    max_age_ns: int


@dataclass(frozen=True, slots=True)
class _Binding:
    sample: _CorpusSample
    event: EventRecord
    packet: FramePacket
    observed_at_ns: int
    effective_now_ns: int
    generation: int


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
        event_tape_index: EventTapeIndexInput | None = None,
        event_tape_index_path: str | Path | None = None,
        extractor: PlayerMarkerExtractor | Callable[..., object] | None = None,
        source_commit: str | None = None,
        corpus_source_commit: str | None = None,
        replay_source_commit: str | None = None,
        verification_profile: str = "b2_gate",
        config: Mapping[str, Any] | None = None,
        config_digest: str | None = None,
        extractor_artifact_digest: str | None = None,
        accepted_frame_ledger: LedgerInput | None = None,
        accepted_ledger: LedgerInput | None = None,
        accepted_frame_ledger_path: str | Path | None = None,
        calibration: CalibrationInput | None = None,
        calibration_artifact: CalibrationInput | None = None,
        calibration_path: str | Path | None = None,
        as_of_ns: int = 0,
        as_of_offset_ns: int = 0,
        max_age_ns: int | None = None,
        generation: int = 0,
        zero_input_audit: AuditInput | None = None,
        zero_input_audit_path: str | Path | None = None,
        zero_input_audit_artifact_sha256: str | None = None,
        zero_input_audit_sha256: str | None = None,
        pixels_by_digest: Mapping[str, bytes] | None = None,
    ) -> None:
        if isinstance(manifest, PlayerMarkerReplayConfig):
            overrides = (
                truth_root,
                cas_root,
                event_tapes,
                event_tape_paths,
                event_tape_index,
                event_tape_index_path,
                source_commit,
                corpus_source_commit,
                replay_source_commit,
                config,
                config_digest,
                extractor_artifact_digest,
                accepted_frame_ledger,
                accepted_ledger,
                accepted_frame_ledger_path,
                calibration,
                calibration_artifact,
                calibration_path,
                max_age_ns,
                zero_input_audit,
                zero_input_audit_path,
                zero_input_audit_artifact_sha256,
                zero_input_audit_sha256,
                pixels_by_digest,
            )
            if (
                any(value is not None for value in overrides)
                or as_of_ns != 0
                or as_of_offset_ns != 0
                or generation != 0
            ):
                raise PlayerMarkerReplayError("config object cannot be combined with overrides")
            self.config = manifest
        else:
            tapes = event_tapes if event_tapes is not None else event_tape_paths
            if event_tapes is not None and event_tape_paths is not None:
                raise PlayerMarkerReplayError("provide event_tapes or event_tape_paths, not both")
            index_values = [
                value
                for value in (event_tape_index, event_tape_index_path)
                if value is not None
            ]
            if len(index_values) > 1:
                raise PlayerMarkerReplayError("provide one event-tape index input")
            ledger_values = [
                value
                for value in (
                    accepted_frame_ledger,
                    accepted_ledger,
                    accepted_frame_ledger_path,
                )
                if value is not None
            ]
            if len(ledger_values) > 1:
                raise PlayerMarkerReplayError("provide one accepted-frame ledger input")
            calibration_values = [
                value
                for value in (calibration, calibration_artifact, calibration_path)
                if value is not None
            ]
            if len(calibration_values) > 1:
                raise PlayerMarkerReplayError("provide one calibration input")
            audit_values = [
                value
                for value in (zero_input_audit, zero_input_audit_path)
                if value is not None
            ]
            if len(audit_values) > 1:
                raise PlayerMarkerReplayError("provide one zero-input audit input")
            audit_sha_values = [
                value
                for value in (zero_input_audit_artifact_sha256, zero_input_audit_sha256)
                if value is not None
            ]
            if len(audit_sha_values) > 1:
                raise PlayerMarkerReplayError("provide one zero-input audit artifact digest")
            self.config = PlayerMarkerReplayConfig(
                manifest=manifest,
                verification_profile=verification_profile,
                truth_root=truth_root,
                cas_root=cas_root,
                event_tapes=tuple(() if tapes is None else tapes),
                event_tape_index=(None if not index_values else index_values[0]),
                source_commit=source_commit,
                corpus_source_commit=corpus_source_commit,
                replay_source_commit=replay_source_commit,
                config=config,
                config_digest=config_digest,
                extractor_artifact_digest=extractor_artifact_digest,
                accepted_frame_ledger=(None if not ledger_values else ledger_values[0]),
                calibration=(None if not calibration_values else calibration_values[0]),
                as_of_ns=as_of_ns,
                as_of_offset_ns=as_of_offset_ns,
                max_age_ns=max_age_ns,
                generation=generation,
                zero_input_audit=(None if not audit_values else audit_values[0]),
                zero_input_audit_artifact_sha256=(
                    None if not audit_sha_values else audit_sha_values[0]
                ),
                pixels_by_digest=pixels_by_digest,
            )
        self.extractor = extractor
        if self.extractor is None:
            raise PlayerMarkerReplayError("extractor is required; missing extractor is not a PASS")
        self._manifest, self._manifest_digest, self._samples = self._load_corpus()
        (
            self._event_tape_index,
            self._event_tape_index_artifact_digest,
        ) = self._load_event_tape_index()
        self._event_records = self._load_events()
        self._validate_event_tape()
        self._calibration = self._load_calibration()
        self._validate_calibration_truth_bindings()
        self._ledger_rows, self._accepted_ledger_digest = self._load_ledger()
        self._validate_ledger()
        self._sample_order_digest = _digest([item.sample_id for item in self._samples])
        self.corpus_source_commit = _commit(
            self._manifest["source_commit"], "manifest.source_commit"
        )
        expected_corpus_commit = self.config.corpus_source_commit
        if (
            expected_corpus_commit is not None
            and expected_corpus_commit != self.corpus_source_commit
        ):
            raise PlayerMarkerReplayError("corpus_source_commit does not match corpus manifest")
        replay_commit = self.config.replay_source_commit
        if (
            self.config.source_commit is not None
            and self.config.corpus_source_commit is not None
            and self.config.source_commit != self.config.corpus_source_commit
        ):
            raise PlayerMarkerReplayError("source_commit and corpus_source_commit disagree")
        if (
            self.config.source_commit is not None
            and self.config.corpus_source_commit is None
            and self.config.source_commit != self.corpus_source_commit
        ):
            # Before the split fields were introduced, a caller commonly used
            # source_commit for the replay artifact.  Preserve that spelling
            # when it is plainly not the manifest commit.
            replay_commit = self.config.source_commit
        if replay_commit is None:
            raise PlayerMarkerReplayError("replay_source_commit is required")
        self.replay_source_commit = _commit(replay_commit, "replay_source_commit")
        self._validate_extractor_boundary()
        self.extractor_config_digest = self._resolve_extractor_config_digest()
        self.extractor_artifact_digest = self._resolve_extractor_artifact_digest()
        self._event_tape_digest = self._compute_event_tape_digest()
        (
            self._zero_input_audit,
            self._zero_input_audit_artifact_digest,
        ) = self._load_zero_input_audit()
        self._execution_faults: set[str] = set()

    @property
    def manifest(self) -> Mapping[str, Any]:
        return self._manifest

    @property
    def sample_order(self) -> tuple[str, ...]:
        return tuple(item.sample_id for item in self._samples)

    def _load_corpus(self) -> tuple[Mapping[str, Any], str, tuple[_CorpusSample, ...]]:
        manifest_path: Path | None = None
        if isinstance(self.config.manifest, str | Path):
            manifest_path = Path(self.config.manifest)
            try:
                manifest = load_strict_json(manifest_path)
                if manifest_path.read_bytes() != canonical_json(manifest) + b"\n":
                    raise ValueError("manifest must use canonical JSON plus one LF")
            except (OSError, ValueError, TypeError) as exc:
                raise PlayerMarkerReplayError("invalid corpus manifest") from exc
        else:
            manifest = dict(_mapping(self.config.manifest, "manifest"))
        truth_root = (
            Path(self.config.truth_root)
            if self.config.truth_root is not None
            else (manifest_path.parent if manifest_path is not None else None)
        )
        if truth_root is None:
            raise PlayerMarkerReplayError("truth_root is required for an in-memory manifest")
        cas_root: str | Path | None
        if isinstance(self.config.cas_root, PixelStore):
            cas_root = self.config.cas_root.root
        else:
            cas_root = self.config.cas_root
        if self.config.verification_profile == "b2_gate" and cas_root is None:
            raise PlayerMarkerReplayError("b2_gate requires a verified CAS root")
        try:
            if manifest_path is not None:
                verify_corpus_file(
                    manifest_path,
                    truth_root=truth_root,
                    cas_root=cas_root,
                    profile=(
                        "b2_gate"
                        if self.config.verification_profile == "b2_gate"
                        else "b1_fixture"
                    ),
                )
            else:
                kwargs: dict[str, Any] = {
                    "truth_root": truth_root,
                    "cas_root": cas_root,
                    "minimum_samples": 1,
                    "minimum_unique_pixels": 1,
                    "minimum_sessions": 1,
                }
                if self.config.verification_profile == "b2_gate":
                    kwargs.update(
                        {
                            "minimum_samples": 300,
                            "minimum_unique_pixels": 300,
                            "minimum_sessions": 3,
                            "minimum_independent_sessions": 3,
                            "required_independent_fraction_ppm": 200_000,
                            "require_category_coverage": True,
                            "require_live_session": True,
                        }
                    )
                verify_corpus_manifest(manifest, **kwargs)
        except (OSError, ValueError, TypeError) as exc:
            raise PlayerMarkerReplayError("corpus manifest verification failed") from exc
        raw_samples = manifest.get("samples")
        if not isinstance(raw_samples, list) or not raw_samples:
            raise PlayerMarkerReplayError("manifest samples must be a non-empty array")
        loaded: list[_CorpusSample] = []
        for index, raw_sample in enumerate(raw_samples):
            sample = _mapping(raw_sample, f"samples[{index}]")
            sample_id = _portable_id(sample.get("sample_id"), f"samples[{index}].sample_id")
            truth_id = _portable_id(sample.get("truth_id"), f"samples[{index}].truth_id")
            session_id = _portable_id(sample.get("session_id"), f"samples[{index}].session_id")
            sequence = sample.get("sequence")
            if type(sequence) is not int:
                raise PlayerMarkerReplayError(
                    f"samples[{index}].sequence must be an integer >= 0"
                )
            ensure_non_negative_int(sequence, f"samples[{index}].sequence")
            digest = _sha256(sample.get("pixel_digest"), f"samples[{index}].pixel_digest")
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
            if (
                truth.get("sample_id") != sample_id
                or truth.get("truth_id") != truth_id
                or truth.get("session_id") != session_id
                or truth.get("sequence") != sequence
                or truth.get("pixel_digest") != digest
            ):
                raise PlayerMarkerReplayError("sample/truth identity binding mismatch")
            if type(sample.get("wrong_size_negative")) is not bool:
                raise PlayerMarkerReplayError("wrong_size_negative must be bool")
            loaded.append(
                _CorpusSample(
                    sample=cast(Mapping[str, Any], freeze_json_value(sample)),
                    truth=cast(Mapping[str, Any], freeze_json_value(truth)),
                    spec=spec,
                )
            )
        manifest_frozen = cast(Mapping[str, Any], freeze_json_value(manifest))
        manifest_digest = corpus_canonical_digest(manifest, omit=("corpus_digest",))
        if _sha256(manifest["corpus_digest"], "manifest.corpus_digest") != manifest_digest:
            raise PlayerMarkerReplayError("manifest corpus_digest mismatch")
        return manifest_frozen, manifest_digest, tuple(loaded)

    @staticmethod
    def _event_tape_path(source: str | Path | EventTape) -> Path:
        return source.path if isinstance(source, EventTape) else Path(source)

    @staticmethod
    def _relative_artifact_path(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value:
            raise PlayerMarkerReplayError(f"{field_name} must be a relative path")
        relative = Path(value.replace("\\", "/"))
        if (
            relative.is_absolute()
            or ":" in value
            or any(part in {"", ".", ".."} for part in relative.parts)
            or value.replace("\\", "/") != value
        ):
            raise PlayerMarkerReplayError(f"{field_name} must be a normalized relative path")
        return value

    def _load_event_tape_index(
        self,
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        value = self.config.event_tape_index
        if value is None:
            if self.config.verification_profile == "b2_gate":
                raise PlayerMarkerReplayError(
                    "b2_gate requires the canonical event_tape_index artifact"
                )
            return None, None

        index_path: Path | None = None
        if isinstance(value, str | Path):
            index_path = Path(value)
            try:
                raw = index_path.read_bytes()
                data = load_strict_json(index_path)
            except (OSError, ValueError, TypeError) as exc:
                raise PlayerMarkerReplayError("invalid event-tape index artifact") from exc
            if raw != canonical_json(data) + b"\n":
                raise PlayerMarkerReplayError(
                    "event-tape index must use canonical JSON plus one LF"
                )
            artifact_digest = sha256(raw).hexdigest()
        else:
            if self.config.verification_profile == "b2_gate":
                raise PlayerMarkerReplayError(
                    "b2_gate event-tape index must be a canonical file artifact"
                )
            data = dict(_mapping(value, "event_tape_index"))
            artifact_digest = sha256(canonical_json(data) + b"\n").hexdigest()

        expected_keys = {
            "schema_version",
            "source_commit",
            "corpus_digest",
            "event_count",
            "tapes",
            "index_digest",
        }
        if set(data) != expected_keys:
            raise PlayerMarkerReplayError("event-tape index keys are not exact")
        if data["schema_version"] != PLAYER_MARKER_REPLAY_SCHEMA_VERSION:
            raise PlayerMarkerReplayError("event-tape index schema_version mismatch")
        source_commit = _commit(data["source_commit"], "event_tape_index.source_commit")
        if source_commit != self._manifest.get("source_commit"):
            raise PlayerMarkerReplayError("event-tape index source_commit mismatch")
        corpus_digest = _sha256(data["corpus_digest"], "event_tape_index.corpus_digest")
        if corpus_digest != self._manifest.get("corpus_digest"):
            raise PlayerMarkerReplayError("event-tape index corpus_digest mismatch")
        event_count = data["event_count"]
        ensure_positive_int(event_count, "event_tape_index.event_count")
        index_digest = _sha256(data["index_digest"], "event_tape_index.index_digest")
        if index_digest != corpus_canonical_digest(data, omit=("index_digest",)):
            raise PlayerMarkerReplayError("event-tape index index_digest mismatch")
        raw_tapes = data["tapes"]
        if not isinstance(raw_tapes, list) or not raw_tapes:
            raise PlayerMarkerReplayError("event-tape index tapes must be a non-empty array")

        indexed: list[tuple[str, str, str, int]] = []
        seen_paths: set[str] = set()
        seen_sessions: set[str] = set()
        for index, raw_tape in enumerate(raw_tapes):
            tape = _mapping(raw_tape, f"event_tape_index.tapes[{index}]")
            if set(tape) != {"path", "session_id", "sha256", "size_bytes"}:
                raise PlayerMarkerReplayError("event-tape index tape keys are not exact")
            relative = self._relative_artifact_path(
                tape["path"], f"event_tape_index.tapes[{index}].path"
            )
            session_id = _portable_id(
                tape["session_id"], f"event_tape_index.tapes[{index}].session_id"
            )
            digest = _sha256(
                tape["sha256"], f"event_tape_index.tapes[{index}].sha256"
            )
            size_bytes = tape["size_bytes"]
            ensure_positive_int(size_bytes, f"event_tape_index.tapes[{index}].size_bytes")
            if relative in seen_paths or session_id in seen_sessions:
                raise PlayerMarkerReplayError("event-tape index has duplicate path/session")
            seen_paths.add(relative)
            seen_sessions.add(session_id)
            indexed.append((relative, session_id, digest, size_bytes))

        configured = [self._event_tape_path(source) for source in self.config.event_tapes]
        if not configured:
            raise PlayerMarkerReplayError("event_tapes are required and must be non-empty")
        if index_path is None:
            # A contract fixture may inject a structured index.  It has no
            # artifact directory from which a relative path can be resolved;
            # validate the tape metadata and session binding below instead.
            configured_paths: set[Path] | None = None
        else:
            configured_paths = {
                path.expanduser().resolve(strict=False) for path in configured
            }
            indexed_paths = {
                (index_path.parent / Path(relative)).resolve(strict=False)
                for relative, _session, _digest, _size in indexed
            }
            if indexed_paths != configured_paths or len(configured) != len(indexed):
                raise PlayerMarkerReplayError(
                    "event-tape index tapes do not exactly match configured tapes"
                )

        total_events = 0
        observed_sessions: set[str] = set()
        indexed_by_path = {
            (
                None
                if index_path is None
                else (index_path.parent / Path(relative)).resolve(strict=False)
            ): (relative, session_id, digest, size_bytes)
            for relative, session_id, digest, size_bytes in indexed
        }
        for configured_path in configured:
            path = configured_path.expanduser().resolve(strict=False)
            try:
                raw_tape = path.read_bytes()
                records = EventTape(path).read_all()
            except (OSError, ValueError, TypeError) as exc:
                raise PlayerMarkerReplayError("event-tape index tape artifact is invalid") from exc
            if not records:
                raise PlayerMarkerReplayError("event-tape index references an empty tape")
            sessions = {record.session_id for record in records}
            if len(sessions) != 1:
                raise PlayerMarkerReplayError("event-tape index tape has multiple sessions")
            session_id = next(iter(sessions))
            observed_sessions.add(session_id)
            if index_path is not None:
                entry = indexed_by_path.get(path)
                if entry is None:
                    raise PlayerMarkerReplayError("configured tape is absent from index")
                _relative, indexed_session, indexed_sha, indexed_size = entry
                if (
                    indexed_session != session_id
                    or indexed_sha != sha256(raw_tape).hexdigest()
                    or indexed_size != len(raw_tape)
                ):
                    raise PlayerMarkerReplayError("event-tape index tape metadata mismatch")
            total_events += len(records)

        if index_path is None:
            if len(indexed) != len(configured) or seen_sessions != observed_sessions:
                raise PlayerMarkerReplayError("structured event-tape index session mismatch")
            # There is no path root for a structured contract index, but its
            # paths still remain hash-only input metadata and are validated.
            for _relative, session_id, _digest, _size in indexed:
                if session_id not in observed_sessions:
                    raise PlayerMarkerReplayError("structured index references an unknown session")
        if total_events != event_count:
            raise PlayerMarkerReplayError("event-tape index event_count mismatch")
        if len(observed_sessions) != len(indexed):
            raise PlayerMarkerReplayError("event-tape index sessions are not unique")
        return cast(Mapping[str, Any], freeze_json_value(data)), artifact_digest

    def _load_events(self) -> tuple[EventRecord, ...]:
        if not self.config.event_tapes:
            raise PlayerMarkerReplayError("event_tapes are required and must be non-empty")
        records: list[EventRecord] = []
        tape_sessions: list[str] = []
        seen_tape_sessions: set[str] = set()
        for source in self.config.event_tapes:
            try:
                tape = source if isinstance(source, EventTape) else EventTape(source)
                batch = tape.read_all()
                if not batch:
                    raise ValueError("Event Tape is empty")
                sessions = {record.session_id for record in batch}
                if len(sessions) != 1:
                    raise ValueError("each Event Tape must contain one session")
                session_id = next(iter(sessions))
                if session_id in seen_tape_sessions:
                    raise ValueError("Event Tape session is split across tapes")
                seen_tape_sessions.add(session_id)
                tape_sessions.append(session_id)
                records.extend(batch)
            except (OSError, ValueError, TypeError) as exc:
                raise PlayerMarkerReplayError("event tape verification failed") from exc
        if not records:
            raise PlayerMarkerReplayError("event tape is empty")
        self._event_tape_sessions = tuple(tape_sessions)
        return tuple(records)

    def _records_for(self, sample: _CorpusSample) -> tuple[EventRecord, ...]:
        return tuple(
            record
            for record in self._event_records
            if record.session_id == sample.session_id
            and record.payload.get("truth_id") == sample.truth_id
        )

    def _validate_event_tape(self) -> None:
        manifest_sessions = {sample.session_id for sample in self._samples}
        tape_sessions = set(getattr(self, "_event_tape_sessions", ()))
        if tape_sessions != manifest_sessions or len(tape_sessions) != len(
            self._event_tape_sessions
        ):
            raise PlayerMarkerReplayError(
                "Event Tape sessions must map one-to-one to manifest sessions"
            )
        if len(self._event_records) != len(self._samples):
            raise PlayerMarkerReplayError("Event Tape is not complete for the verified manifest")
        seen: set[str] = set()
        for sample in self._samples:
            records = self._records_for(sample)
            if len(records) != 1:
                raise PlayerMarkerReplayError("Event Tape sample binding is not unique")
            record = records[0]
            payload = record.payload
            if record.frame_id != sample.sequence or payload.get("truth_scope") != TRUTH_SCOPE:
                raise PlayerMarkerReplayError("Event Tape packet lineage mismatch")
            if payload.get("truth_pixel_digest") != sample.pixel_digest:
                raise PlayerMarkerReplayError("Event Tape truth pixel binding mismatch")
            if sample.expected_status == "accepted" and not sample.wrong_size:
                if record.event_type != "frame.accepted":
                    raise PlayerMarkerReplayError("accepted sample lacks frame.accepted event")
                if (
                    payload.get("admission_status") != "accepted"
                    or payload.get("fault_latched") is not False
                    or payload.get("plan_suppressed") is not False
                    or payload.get("pixel_digest") != sample.pixel_digest
                    or payload.get("image_ref") != sample.sample.get("cas_ref")
                ):
                    raise PlayerMarkerReplayError("accepted Event Tape payload mismatch")
            elif sample.wrong_size:
                if record.event_type not in {"frame.fatal", "frame.suppressed"}:
                    raise PlayerMarkerReplayError("wrong-size sample lacks rejection event")
                if (
                    payload.get("admission_status") != "frame_size_changed"
                    or payload.get("fault_latched") is not (record.event_type == "frame.fatal")
                    or payload.get("plan_suppressed") is not True
                    or payload.get("pixel_digest") is not None
                    or payload.get("image_ref") is not None
                ):
                    raise PlayerMarkerReplayError("wrong-size admission status mismatch")
            else:
                raise PlayerMarkerReplayError("unsupported expected admission status")
            if record.record_hash in seen:
                raise PlayerMarkerReplayError("duplicate Event Tape record")
            seen.add(record.record_hash)

    def _compute_event_tape_digest(self) -> str:
        return _digest(
            {
                "record_hashes": [record.record_hash for record in self._event_records],
                "sample_bindings": [
                    {
                        "sample_id": sample.sample_id,
                        "record_hash": self._records_for(sample)[0].record_hash,
                    }
                    for sample in self._samples
                ],
            }
        )

    @staticmethod
    def _artifact_digest(path: Path) -> str:
        return sha256(path.read_bytes()).hexdigest()

    def _load_calibration(self) -> _Calibration:
        value = self.config.calibration
        if value is None:
            raise PlayerMarkerReplayError("calibration input is required")
        artifact_digest: str
        if isinstance(value, str | Path):
            path = Path(value)
            try:
                raw = path.read_bytes()
                data = load_strict_json(path)
            except (OSError, ValueError, TypeError) as exc:
                raise PlayerMarkerReplayError("invalid calibration artifact") from exc
            artifact_digest = sha256(raw).hexdigest()
        else:
            data = dict(_mapping(value, "calibration"))
            artifact_digest = _digest(data)
        raw_geometry = data.get("geometry", data.get("source_geometry"))
        if not isinstance(raw_geometry, Mapping):
            raise PlayerMarkerReplayError("calibration geometry is required")
        try:
            geometry = SourceGeometry.from_dict(raw_geometry)
        except (TypeError, ValueError) as exc:
            raise PlayerMarkerReplayError("invalid calibration geometry") from exc
        transform_version = data.get("transform_version")
        if not isinstance(transform_version, str):
            raise PlayerMarkerReplayError("calibration.transform_version must be a string")
        ensure_non_empty_str(transform_version, "calibration.transform_version")
        expected_calibration = canonical_calibration_sha256(geometry, transform_version)
        supplied_calibration = _sha256(data.get("calibration_sha256"), "calibration_sha256")
        if supplied_calibration != expected_calibration:
            raise PlayerMarkerReplayError("calibration_sha256 does not match geometry")
        max_age_value = data.get("max_age_ns", self.config.max_age_ns)
        if max_age_value is None:
            max_age_value = DEFAULT_MAX_AGE_NS
        ensure_non_negative_int(max_age_value, "max_age_ns")
        return _Calibration(
            geometry=geometry,
            transform_version=transform_version,
            calibration_sha256=supplied_calibration,
            artifact_digest=artifact_digest,
            max_age_ns=max_age_value,
        )

    def _validate_calibration_truth_bindings(self) -> None:
        """Bind every corpus truth derivation to the selected calibration.

        Geometry and transform hashes are independently self-consistent, so a
        same-sized rogue calibration can otherwise pass input validation.  The
        corpus truth is the authority for the capture transform context and is
        checked for both accepted and wrong-size samples before replay starts.
        """

        for sample in self._samples:
            derivation = _mapping(sample.truth.get("derivation"), "truth.derivation")
            if (
                derivation.get("transform_version") != self._calibration.transform_version
                or derivation.get("calibration_sha256") != self._calibration.calibration_sha256
            ):
                raise PlayerMarkerReplayError(
                    "calibration does not match corpus truth derivation"
                )

    @staticmethod
    def _strict_json_line(line: str, line_number: int) -> Mapping[str, Any]:
        def reject_constant(value: str) -> None:
            raise ValueError(f"non-standard JSON constant at line {line_number}: {value}")

        def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key at line {line_number}: {key}")
                result[key] = item
            return result

        try:
            value = json.loads(
                line,
                object_pairs_hook=reject_duplicate,
                parse_constant=reject_constant,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlayerMarkerReplayError("invalid accepted-frame ledger JSONL") from exc
        return _mapping(value, f"accepted_frame_ledger[{line_number}]")

    def _load_ledger(self) -> tuple[dict[tuple[str, int], Mapping[str, Any]], str]:
        value = self.config.accepted_frame_ledger
        if value is None:
            raise PlayerMarkerReplayError("accepted_frame_ledger input is required")
        rows: list[Mapping[str, Any]]
        artifact_digest: str
        if isinstance(value, str | Path):
            path = Path(value)
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise PlayerMarkerReplayError("accepted-frame ledger is unavailable") from exc
            artifact_digest = sha256(raw).hexdigest()
            rows = []
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PlayerMarkerReplayError(
                    "accepted-frame ledger is not UTF-8"
                ) from exc
            for line_number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    raise PlayerMarkerReplayError("accepted-frame ledger contains a blank line")
                rows.append(self._strict_json_line(line, line_number))
        elif isinstance(value, Mapping):
            wrapper = _mapping(value, "accepted_frame_ledger")
            raw_rows = wrapper.get("rows", wrapper.get("entries"))
            if not isinstance(raw_rows, list):
                raise PlayerMarkerReplayError("accepted_frame_ledger mapping requires rows")
            rows = [_mapping(item, "accepted_frame_ledger row") for item in raw_rows]
            artifact_digest = _digest(wrapper)
        else:
            rows = [_mapping(item, "accepted_frame_ledger row") for item in value]
            artifact_digest = _digest(rows)
        if not rows:
            raise PlayerMarkerReplayError("accepted-frame ledger must be non-empty")
        by_key: dict[tuple[str, int], Mapping[str, Any]] = {}
        for index, row in enumerate(rows):
            required = (
                "session_id",
                "source_id",
                "source_sequence",
                "captured_at_ns",
                "admitted_at_ns",
                "clock_domain",
                "image_ref",
                "pixel_digest",
                "source_width",
                "source_height",
            )
            if any(key not in row for key in required):
                raise PlayerMarkerReplayError(f"accepted-frame ledger row {index} is incomplete")
            session_id = _portable_id(row["session_id"], f"ledger[{index}].session_id")
            source_id = _portable_id(row["source_id"], f"ledger[{index}].source_id")
            sequence = row["source_sequence"]
            if type(sequence) is not int:
                raise PlayerMarkerReplayError(
                    f"ledger[{index}].source_sequence must be an integer >= 0"
                )
            ensure_non_negative_int(sequence, f"ledger[{index}].source_sequence")
            captured = row["captured_at_ns"]
            admitted = row["admitted_at_ns"]
            ensure_time_ns(captured, f"ledger[{index}].captured_at_ns")
            ensure_time_ns(admitted, f"ledger[{index}].admitted_at_ns")
            if admitted < captured:
                raise PlayerMarkerReplayError("ledger admitted_at_ns precedes captured_at_ns")
            if "received_at_ns" in row:
                ensure_time_ns(row["received_at_ns"], f"ledger[{index}].received_at_ns")
                if row["received_at_ns"] < captured:
                    raise PlayerMarkerReplayError("ledger received_at_ns precedes captured_at_ns")
            if "age_ns" in row:
                ensure_non_negative_int(row["age_ns"], f"ledger[{index}].age_ns")
                if row["age_ns"] != admitted - captured:
                    raise PlayerMarkerReplayError("ledger age_ns mismatch")
            ensure_non_empty_str(row["clock_domain"], f"ledger[{index}].clock_domain")
            digest = _sha256(row["pixel_digest"], f"ledger[{index}].pixel_digest")
            image_ref = row["image_ref"]
            if not isinstance(image_ref, str) or _CAS_RE.fullmatch(image_ref) is None:
                raise PlayerMarkerReplayError("ledger image_ref must be a CAS URI")
            if image_ref != f"cas://sha256/{digest}":
                raise PlayerMarkerReplayError("ledger image_ref does not match pixel_digest")
            ensure_positive_int(row["source_width"], f"ledger[{index}].source_width")
            ensure_positive_int(row["source_height"], f"ledger[{index}].source_height")
            if "retained" in row and type(row["retained"]) is not bool:
                raise PlayerMarkerReplayError("ledger retained must be bool")
            if "max_age_ns" in row:
                ensure_non_negative_int(row["max_age_ns"], f"ledger[{index}].max_age_ns")
            key = (session_id, sequence)
            if key in by_key:
                raise PlayerMarkerReplayError("accepted-frame ledger has duplicate source locator")
            by_key[key] = cast(Mapping[str, Any], freeze_json_value(row))
            if source_id != cast(str, row["source_id"]):  # pragma: no cover - defensive
                raise PlayerMarkerReplayError("ledger source identity normalisation mismatch")
        if self.config.verification_profile == "b2_gate" and not by_key:
            raise PlayerMarkerReplayError("b2_gate accepted-frame ledger is empty")
        return by_key, artifact_digest

    def _validate_ledger(self) -> None:
        accepted_count = 0
        live_pairs: set[tuple[str, str]] = set()
        live_source_ids: set[str] = set()
        live_artifacts: set[str] = set()
        if self.config.verification_profile == "b2_gate":
            raw_sources = self._manifest.get("sources")
            raw_sessions = self._manifest.get("sessions")
            if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, str):
                raise PlayerMarkerReplayError("b2_gate manifest sources are missing")
            if not isinstance(raw_sessions, Sequence) or isinstance(raw_sessions, str):
                raise PlayerMarkerReplayError("b2_gate manifest sessions are missing")
            source_info: dict[str, tuple[str, str]] = {}
            for raw_source in raw_sources:
                source = _mapping(raw_source, "manifest source")
                source_id = _portable_id(source.get("source_id"), "manifest source_id")
                locator_kind = source.get("locator_kind")
                if not isinstance(locator_kind, str):
                    raise PlayerMarkerReplayError("manifest source locator_kind is invalid")
                artifact = _sha256(
                    source.get("artifact_sha256"),
                    "manifest source.artifact_sha256",
                )
                source_info[source_id] = (locator_kind, artifact)
                if locator_kind == "live_session":
                    live_source_ids.add(source_id)
            for raw_session in raw_sessions:
                session = _mapping(raw_session, "manifest session")
                session_id = _portable_id(session.get("session_id"), "manifest session_id")
                source_id = _portable_id(session.get("source_id"), "manifest session.source_id")
                kind_artifact = source_info.get(source_id)
                if kind_artifact is None:
                    raise PlayerMarkerReplayError("manifest session source is unknown")
                if kind_artifact[0] == "live_session":
                    live_pairs.add((session_id, source_id))
                    live_artifacts.add(kind_artifact[1])
            if len(live_source_ids) != 1 or len(live_artifacts) != 1 or not live_pairs:
                raise PlayerMarkerReplayError(
                    "b2_gate requires one live_session source and session"
                )
            self._b2_live_source_artifact = next(iter(live_artifacts))

        for sample in self._samples:
            if sample.wrong_size or sample.expected_status != "accepted":
                continue
            accepted_count += 1
            row = self._ledger_rows.get((sample.session_id, sample.sequence))
            is_live = (sample.session_id, sample.source_id) in live_pairs
            if row is None:
                # Historical video samples have no capture-clock ledger row;
                # their Event Tape recorded_at_ns is the explicit fallback.
                if is_live:
                    raise PlayerMarkerReplayError(
                        "b2_gate live accepted sample lacks a ledger row"
                    )
                continue
            if (
                row["source_id"] != sample.source_id
                or row["pixel_digest"] != sample.pixel_digest
                or row["image_ref"] != sample.sample.get("cas_ref")
                or row["source_width"] != sample.spec.width
                or row["source_height"] != sample.spec.height
            ):
                raise PlayerMarkerReplayError("accepted-frame ledger/sample binding mismatch")
            if (
                "max_age_ns" in row
                and row["max_age_ns"] != self._calibration.max_age_ns
            ):
                raise PlayerMarkerReplayError(
                    "accepted-frame ledger max_age_ns disagrees with calibration"
                )
            event = self._records_for(sample)[0]
            if row["session_id"] != event.session_id or event.frame_id != sample.sequence:
                raise PlayerMarkerReplayError("accepted-frame ledger/Event Tape binding mismatch")
            if self.config.verification_profile == "b2_gate" and is_live:
                if row.get("retained") is not True:
                    raise PlayerMarkerReplayError(
                        "b2_gate live matched ledger row must be retained"
                    )
                for field in ("source_provenance_id", "pixel_artifact_sha256"):
                    row_digest = _sha256(row.get(field), f"ledger.{field}")
                    if row_digest != sample.sample.get(field) or row_digest != sample.truth.get(
                        field
                    ):
                        raise PlayerMarkerReplayError(
                            f"b2_gate ledger {field} does not match corpus truth"
                        )

        if self.config.verification_profile == "b2_gate":
            for (session_id, _source_sequence), ledger_row in self._ledger_rows.items():
                if (session_id, ledger_row["source_id"]) not in live_pairs:
                    raise PlayerMarkerReplayError(
                        "b2_gate ledger row is outside manifest live sessions"
                    )
        if accepted_count < 1:
            raise PlayerMarkerReplayError("manifest has no accepted samples")

    def _validate_extractor_boundary(self) -> None:
        method = getattr(self.extractor, "extract", None)
        if self.config.verification_profile == "b2_gate":
            if not callable(method):
                raise PlayerMarkerReplayError(
                    "b2_gate accepts only a read-only extractor exposing extract(FramePacket)"
                )
            if not hasattr(self.extractor, "config"):
                raise PlayerMarkerReplayError("b2_gate extractor.config is required")
            store = getattr(self.extractor, "pixel_store", getattr(self.extractor, "cas", None))
            if not callable(getattr(store, "read", None)):
                raise PlayerMarkerReplayError("b2_gate extractor must use a read-only pixel store")
            if callable(getattr(store, "write", None)) or callable(
                getattr(store, "put", None)
            ):
                raise PlayerMarkerReplayError("b2_gate extractor pixel store must be read-only")
        elif method is not None and not callable(method):
            raise PlayerMarkerReplayError("extractor.extract must be callable")

    @staticmethod
    def _config_value(config: object) -> Any:
        to_dict = getattr(config, "to_dict", None)
        if callable(to_dict):
            return _strict_json(to_dict(), "extractor.config")
        if isinstance(config, Mapping):
            return _strict_json(config, "extractor.config")
        raise PlayerMarkerReplayError("extractor.config must expose to_dict() or a mapping")

    def _resolve_extractor_config_digest(self) -> str:
        extractor_config = getattr(self.extractor, "config", None)
        supplied_config = self.config.config
        if extractor_config is None and supplied_config is None:
            raise PlayerMarkerReplayError(
                "extractor config is required; config=None is not accepted"
            )
        actual_value = (
            self._config_value(extractor_config)
            if extractor_config is not None
            else supplied_config
        )
        if actual_value is None:  # pragma: no cover - guarded above
            raise PlayerMarkerReplayError("extractor config is required")
        actual_digest = _digest(actual_value)
        declared_digest = getattr(extractor_config, "digest", None)
        if declared_digest is not None:
            if not isinstance(declared_digest, str) or _SHA256_RE.fullmatch(
                declared_digest
            ) is None:
                raise PlayerMarkerReplayError("extractor config digest is invalid")
            if declared_digest != actual_digest:
                raise PlayerMarkerReplayError("extractor config digest is not recomputable")
        if (
            extractor_config is not None
            and supplied_config is not None
            and _digest(_strict_json(supplied_config, "config")) != actual_digest
        ):
            raise PlayerMarkerReplayError("config does not match extractor.config")
        if self.config.config_digest is not None and self.config.config_digest != actual_digest:
            raise PlayerMarkerReplayError("config_digest does not match extractor config")
        return actual_digest

    def _resolve_extractor_artifact_digest(self) -> str:
        value = self.config.extractor_artifact_digest
        if value is None:
            value = getattr(self.extractor, "artifact_digest", None)
        if value is None:
            value = getattr(self.extractor, "extractor_artifact_digest", None)
        if value is None:
            raise PlayerMarkerReplayError("extractor_artifact_digest is required")
        return _sha256(value, "extractor_artifact_digest")

    def _load_zero_input_audit(self) -> tuple[Mapping[str, Any], str]:
        value = self.config.zero_input_audit
        if value is None:
            raise PlayerMarkerReplayError(
                "zero_input_audit artifact is required; it is never fabricated"
            )
        supplied_sha = self.config.zero_input_audit_artifact_sha256
        if isinstance(value, str | Path):
            path = Path(value)
            try:
                raw = path.read_bytes()
                data = load_strict_json(path)
            except (OSError, ValueError, TypeError) as exc:
                raise PlayerMarkerReplayError("invalid zero-input audit artifact") from exc
            if raw != canonical_json(data) + b"\n":
                raise PlayerMarkerReplayError(
                    "zero-input audit must use canonical JSON plus one LF"
                )
            actual_sha = sha256(raw).hexdigest()
        else:
            if self.config.verification_profile == "b2_gate":
                raise PlayerMarkerReplayError(
                    "b2_gate zero-input audit must be a canonical file artifact"
                )
            data = dict(_mapping(value, "zero_input_audit"))
            actual_sha = _digest(data)
        if supplied_sha is None and self.config.verification_profile == "b2_gate":
            raise PlayerMarkerReplayError("b2_gate requires zero_input_audit_artifact_sha256")
        if (
            supplied_sha is not None
            and _sha256(supplied_sha, "zero_input_audit_artifact_sha256") != actual_sha
        ):
            raise PlayerMarkerReplayError("zero-input audit artifact SHA mismatch")
        validated = _validate_zero_input_audit(
            data,
            expected_source_commit=self.corpus_source_commit,
        )
        if self.config.verification_profile == "b2_gate":
            live_artifact = getattr(self, "_b2_live_source_artifact", None)
            if live_artifact is None or validated["wheel_sha256"] != live_artifact:
                raise PlayerMarkerReplayError(
                    "b2_gate zero-input audit wheel is not the live source artifact"
                )
        return validated, actual_sha

    def _pixel_bytes(self, sample: _CorpusSample) -> bytes | None:
        if (
            self.config.pixels_by_digest is not None
            and sample.pixel_digest in self.config.pixels_by_digest
        ):
            data = self.config.pixels_by_digest[sample.pixel_digest]
            try:
                validate_pixels(sample.spec, data)
                if pixel_digest(sample.spec, data) != sample.pixel_digest:
                    raise ValueError("pixel digest mismatch")
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
            return None
        try:
            return store.read(sample.pixel_digest, sample.spec)
        except (OSError, ValueError, TypeError) as exc:
            raise PlayerMarkerReplayError("accepted packet pixels are unavailable") from exc

    def _packet_for(self, sample: _CorpusSample, event: EventRecord) -> _Binding:
        if (
            sample.spec.width != self._calibration.geometry.source_size.width
            or sample.spec.height != self._calibration.geometry.source_size.height
        ):
            raise PlayerMarkerReplayError(
                "accepted packet pixel geometry does not match calibration"
            )
        ledger = self._ledger_rows.get((sample.session_id, sample.sequence))
        if ledger is None:
            captured_at_ns = event.recorded_at_ns
            received_at_ns = event.recorded_at_ns
            observed_at_ns = event.recorded_at_ns
            clock_domain = "event_tape"
            max_age_ns = self._calibration.max_age_ns
        else:
            captured_at_ns = cast(int, ledger["captured_at_ns"])
            admitted_at_ns = cast(int, ledger["admitted_at_ns"])
            received_at_ns = cast(int, ledger.get("received_at_ns", admitted_at_ns))
            observed_at_ns = received_at_ns
            clock_domain = cast(str, ledger["clock_domain"])
            max_age_ns = cast(int, ledger.get("max_age_ns", self._calibration.max_age_ns))
        if ledger is None:
            admitted_at_ns = received_at_ns
        effective_now_ns = observed_at_ns + self.config.as_of_offset_ns
        ensure_time_ns(effective_now_ns, "effective_now_ns")
        image_ref = sample.sample.get("cas_ref")
        if not isinstance(image_ref, str) or _CAS_RE.fullmatch(image_ref) is None:
            raise PlayerMarkerReplayError("sample cas_ref is invalid")
        if (
            image_ref != f"cas://sha256/{sample.pixel_digest}"
            or event.payload.get("image_ref") != image_ref
        ):
            raise PlayerMarkerReplayError("accepted image_ref binding mismatch")
        event_bindings = {
            "source_id": sample.source_id,
            "session_id": sample.session_id,
            "captured_at_ns": captured_at_ns,
            "admitted_at_ns": admitted_at_ns,
            "received_at_ns": received_at_ns,
            "clock_domain": clock_domain,
            "max_age_ns": max_age_ns,
            "source_width": sample.spec.width,
            "source_height": sample.spec.height,
            "transform_version": self._calibration.transform_version,
            "calibration_sha256": self._calibration.calibration_sha256,
        }
        for key, expected in event_bindings.items():
            if key in event.payload and event.payload[key] != expected:
                raise PlayerMarkerReplayError(
                    f"accepted Event Tape {key} binding mismatch"
                )
        _ = self._pixel_bytes(sample)
        health = CaptureHealth(
            session_id=sample.session_id,
            frame_id=event.frame_id,
            source_id=sample.source_id,
            content_hash=sample.pixel_digest,
            clock_domain=clock_domain,
            captured_at_ns=captured_at_ns,
            received_at_ns=received_at_ns,
            transform_version=self._calibration.transform_version,
            max_age_ns=max_age_ns,
        )
        packet = FramePacket(
            source_id=sample.source_id,
            session_id=sample.session_id,
            frame_id=event.frame_id,
            captured_at_ns=captured_at_ns,
            received_at_ns=received_at_ns,
            transform_version=self._calibration.transform_version,
            clock_domain=clock_domain,
            content_hash=sample.pixel_digest,
            source_geometry=self._calibration.geometry,
            image_ref=image_ref,
            capture_health=health,
            image_metadata={
                "pixel_digest": sample.pixel_digest,
                "pixel_spec": sample.spec.to_dict(),
                "geometry_sha256": canonical_geometry_sha256(self._calibration.geometry),
                "calibration_sha256": self._calibration.calibration_sha256,
                "timing_strategy": PLAYER_MARKER_REPLAY_TIMING_STRATEGY,
                "source_id": sample.source_id,
                "session_id": sample.session_id,
                "frame_id": event.frame_id,
                "captured_at_ns": captured_at_ns,
                "admitted_at_ns": admitted_at_ns,
                "received_at_ns": received_at_ns,
                "observed_at_ns": observed_at_ns,
                "as_of_offset_ns": self.config.as_of_offset_ns,
                "effective_now_ns": effective_now_ns,
                "clock_domain": clock_domain,
                "max_age_ns": max_age_ns,
                "image_ref": image_ref,
                "source_sequence": sample.sequence,
            },
        )
        return _Binding(
            sample=sample,
            event=event,
            packet=packet,
            observed_at_ns=observed_at_ns,
            effective_now_ns=effective_now_ns,
            generation=self.config.generation,
        )

    @staticmethod
    def _extractor_roi(extractor: object) -> object | None:
        config = getattr(extractor, "config", None)
        if isinstance(config, Mapping):
            return cast(object | None, config.get("minimap_roi", config.get("roi")))
        for name in ("minimap_roi", "roi"):
            if hasattr(config, name):
                return cast(object | None, getattr(config, name))
        return None

    @staticmethod
    def _call_extractor(
        extractor: object,
        frame: FramePacket,
        *,
        effective_now_ns: int,
        observed_at_ns: int,
        generation: int,
        roi: object | None = None,
    ) -> object:
        method = getattr(extractor, "extract", None)
        target: Callable[..., object] = cast(
            Callable[..., object], method if callable(method) else extractor
        )
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            return target(
                frame,
                now_ns=effective_now_ns,
                observed_at_ns=observed_at_ns,
                generation=generation,
            )
        parameters = signature.parameters
        accepts_var_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs: dict[str, object] = {}
        if accepts_var_kwargs or "now_ns" in parameters:
            kwargs["now_ns"] = effective_now_ns
        elif "checked_at_ns" in parameters:
            kwargs["checked_at_ns"] = effective_now_ns
        elif "as_of_ns" in parameters:
            kwargs["as_of_ns"] = effective_now_ns
        if accepts_var_kwargs or "observed_at_ns" in parameters:
            kwargs["observed_at_ns"] = observed_at_ns
        if accepts_var_kwargs or "generation" in parameters:
            kwargs["generation"] = generation
        if "roi" in parameters:
            kwargs["roi"] = roi
        elif "minimap_roi" in parameters:
            kwargs["minimap_roi"] = roi
        elif accepts_var_kwargs and roi is not None:
            kwargs["roi"] = roi
        # The only positional argument is the real FramePacket.  In
        # particular, a second positional pixel buffer is never passed.
        return target(frame, **kwargs)

    @staticmethod
    def _declared_result_digest(raw_result: object) -> str:
        """Recompute the complete result; declaration fields are only checks.

        A result that has no strict representation is rejected instead of
        falling back to a caller-controlled digest.  That keeps hidden object
        state from being silently omitted from the three-run comparison.
        """

        declared: list[object] = []
        if isinstance(raw_result, Mapping):
            for key in ("result_digest", "digest", "sha256"):
                if key in raw_result:
                    declared.append(raw_result[key])
            result_body: object = {
                key: value
                for key, value in raw_result.items()
                if key not in {"result_digest", "digest", "sha256"}
            }
        elif raw_result is None:
            result_body = None
        else:
            for key in ("result_digest", "digest", "sha256"):
                try:
                    value = getattr(raw_result, key)
                except AttributeError:
                    continue
                except (TypeError, ValueError, RuntimeError, KeyError) as exc:
                    raise PlayerMarkerReplayError(
                        "detector result declaration is unreadable"
                    ) from exc
                declared.append(value)
            to_dict = getattr(raw_result, "to_dict", None)
            if not callable(to_dict):
                raise PlayerMarkerReplayError(
                    "detector result must be strict JSON or expose to_dict()"
                )
            try:
                result_body = to_dict()
            except (TypeError, ValueError, AttributeError, RuntimeError, KeyError) as exc:
                raise PlayerMarkerReplayError(
                    "detector result to_dict() failed"
                ) from exc

        if len(declared) > 1 and any(value != declared[0] for value in declared[1:]):
            raise PlayerMarkerReplayError("detector result exposes conflicting digests")
        declared_digest = (
            None if not declared else _sha256(declared[0], "detector_result_digest")
        )
        if isinstance(result_body, Mapping):
            result_body = {
                key: value
                for key, value in result_body.items()
                if key not in {"result_digest", "digest", "sha256"}
            }
        body = _strict_json(result_body, "detector result")
        recomputed = _digest(body)
        if declared_digest is not None and declared_digest != recomputed:
            raise PlayerMarkerReplayError(
                "detector result digest does not match serialized result"
            )
        return recomputed

    @staticmethod
    def _state_digest(extractor: object) -> str:
        missing = object()
        declared: list[tuple[str, object]] = []
        for name in ("detector_state_digest", "state_digest"):
            try:
                value = getattr(extractor, name, missing)
            except (TypeError, ValueError, RuntimeError, KeyError) as exc:
                raise PlayerMarkerReplayError(
                    "detector state digest declaration is unreadable"
                ) from exc
            if value is not missing:
                declared.append((name, value))

        try:
            state = getattr(extractor, "state", missing)
        except (TypeError, ValueError, RuntimeError, KeyError) as exc:
            raise PlayerMarkerReplayError("detector state is unreadable") from exc
        if state is not missing:
            # A serialisable state is authoritative.  A declaration may be
            # present as an assertion, but it never substitutes for state.
            if (
                isinstance(state, Mapping | list | tuple | str | int | float | bool)
                or state is None
            ):
                state_body = _strict_json(state, "detector state")
            else:
                to_dict = getattr(state, "to_dict", None)
                if not callable(to_dict):
                    raise PlayerMarkerReplayError(
                        "detector state must be strict JSON or expose to_dict()"
                    )
                try:
                    state_body = _strict_json(to_dict(), "detector state")
                except (TypeError, ValueError, AttributeError, RuntimeError, KeyError) as exc:
                    raise PlayerMarkerReplayError("detector state to_dict() failed") from exc
            actual = _digest(state_body)
            for name, value in declared:
                if _sha256(value, name) != actual:
                    raise PlayerMarkerReplayError(
                        "declared detector state digest does not match state"
                    )
            return actual

        if declared:
            values = [_sha256(value, name) for name, value in declared]
            if any(value != values[0] for value in values[1:]):
                raise PlayerMarkerReplayError(
                    "detector state digest declarations conflict"
                )
            return values[0]
        return _digest({"state": _STATeless_DIGEST})

    def _safe_state_digest(self) -> str:
        try:
            return self._state_digest(self.extractor)
        except (
            PlayerMarkerReplayError,
            TypeError,
            ValueError,
            AttributeError,
            RuntimeError,
            KeyError,
        ):
            self._execution_faults.add("execution_invalid")
            return _INVALID_STATE_DIGEST

    def _invalid_sample(
        self,
        binding: _Binding,
        *,
        run_index: int,
        fault: str,
        exception_type_digest: str | None = None,
        detector_result_digest: str | None = None,
        invoked: bool = False,
    ) -> PlayerMarkerReplaySample:
        self._execution_faults.add(fault)
        return PlayerMarkerReplaySample.build(
            sample_id=binding.sample.sample_id,
            status="fault",
            evidence_digest=_digest(
                {
                    "record_hash": binding.event.record_hash,
                    "pixel_digest": binding.sample.pixel_digest,
                }
            ),
            fault=fault,
            detector_result_digest=detector_result_digest,
            detector_state_digest=self._safe_state_digest(),
            detector_config_digest=self.extractor_config_digest,
            exception_type_digest=exception_type_digest,
            invoked=invoked,
            as_of_offset_ns=self.config.as_of_offset_ns,
            effective_now_ns=binding.effective_now_ns,
            observed_at_ns=binding.observed_at_ns,
            generation=binding.generation,
        )

    def _extract(self, binding: _Binding, run_index: int) -> PlayerMarkerReplaySample:
        sample = binding.sample
        evidence_digest = _digest(
            {
                "record_hash": binding.event.record_hash,
                "pixel_digest": sample.pixel_digest,
            }
        )
        try:
            state_digest = self._state_digest(self.extractor)
        except (
            PlayerMarkerReplayError,
            TypeError,
            ValueError,
            AttributeError,
            RuntimeError,
            KeyError,
        ):
            self._execution_faults.add("execution_invalid")
            return PlayerMarkerReplaySample.build(
                sample_id=sample.sample_id,
                status="fault",
                evidence_digest=evidence_digest,
                fault="execution_invalid",
                detector_config_digest=self.extractor_config_digest,
                detector_state_digest=_digest({"state": "invalid"}),
                invoked=False,
                as_of_offset_ns=self.config.as_of_offset_ns,
                effective_now_ns=binding.effective_now_ns,
                observed_at_ns=binding.observed_at_ns,
                generation=binding.generation,
            )
        try:
            raw_result = self._call_extractor(
                self.extractor,
                binding.packet,
                effective_now_ns=binding.effective_now_ns,
                observed_at_ns=binding.observed_at_ns,
                generation=binding.generation,
                roi=self._extractor_roi(self.extractor),
            )
        except (ValueError, RuntimeError, KeyError) as exc:
            self._execution_faults.add("extractor_error")
            return PlayerMarkerReplaySample.build(
                sample_id=sample.sample_id,
                status="fault",
                evidence_digest=evidence_digest,
                fault="extractor_error",
                detector_state_digest=state_digest,
                detector_config_digest=self.extractor_config_digest,
                exception_type_digest=_exception_type_digest(exc),
                invoked=True,
                effective_now_ns=binding.effective_now_ns,
                as_of_offset_ns=self.config.as_of_offset_ns,
                observed_at_ns=binding.observed_at_ns,
                generation=binding.generation,
            )
        except Exception as exc:
            self._execution_faults.add("extractor_error")
            return PlayerMarkerReplaySample.build(
                sample_id=sample.sample_id,
                status="fault",
                evidence_digest=evidence_digest,
                fault="extractor_error",
                detector_state_digest=state_digest,
                detector_config_digest=self.extractor_config_digest,
                exception_type_digest=_exception_type_digest(exc),
                invoked=True,
                as_of_offset_ns=self.config.as_of_offset_ns,
                effective_now_ns=binding.effective_now_ns,
                observed_at_ns=binding.observed_at_ns,
                generation=binding.generation,
        )
        try:
            detector_result_digest = self._declared_result_digest(raw_result)
        except (
            PlayerMarkerReplayError,
            TypeError,
            ValueError,
            AttributeError,
            RuntimeError,
            KeyError,
        ):
            return self._invalid_sample(
                binding,
                run_index=run_index,
                fault="execution_invalid",
                invoked=True,
            )
        status: str | None = None
        candidate: object | None = raw_result
        evidence: object | None = None
        fault_value: object | None = None
        if isinstance(raw_result, PlayerMarkerExtraction):
            status = _status_token(raw_result.status)
            candidate = raw_result.candidate
            evidence = raw_result.evidence
            fault_value = raw_result.fault
        elif isinstance(raw_result, Mapping):
            if "status" in raw_result:
                status = _status_token(raw_result["status"])
                candidate = None
            elif any(
                key in raw_result
                for key in ("candidate", "candidate_digest", "evidence", "fault")
            ):
                status = "detected"
                candidate = None
            if "candidate" in raw_result or "candidate_digest" in raw_result:
                candidate = raw_result.get("candidate")
                if candidate is None and raw_result.get("candidate_digest") is not None:
                    candidate = raw_result["candidate_digest"]
            elif status is None:
                candidate = raw_result
            evidence = raw_result.get("evidence", raw_result.get("evidence_digest"))
            fault_value = raw_result.get("fault")
        elif any(
            hasattr(raw_result, field) for field in ("candidate", "evidence", "status", "fault")
        ):
            status = _status_token(getattr(raw_result, "status", "detected"))
            candidate = getattr(raw_result, "candidate", None)
            evidence = getattr(raw_result, "evidence", None)
            fault_value = getattr(raw_result, "fault", None)
        elif raw_result is None:
            status = "no_marker"
            candidate = None
        else:
            status = "detected"
        if status is None:
            return self._invalid_sample(
                binding,
                run_index=run_index,
                fault="execution_invalid",
                detector_result_digest=detector_result_digest,
                invoked=True,
            )
        normalised_fault: str | None = None
        if fault_value is not None:
            normalised_fault = _fault_token(fault_value)
            if normalised_fault is None:
                return self._invalid_sample(
                    binding,
                    run_index=run_index,
                    fault="execution_invalid",
                    detector_result_digest=detector_result_digest,
                    invoked=True,
                )
            self._execution_faults.add(normalised_fault)
            status = "fault"
        if status == "fault":
            return self._invalid_sample(
                binding,
                run_index=run_index,
                fault=(normalised_fault if normalised_fault is not None else "detector_fault"),
                detector_result_digest=detector_result_digest,
                invoked=True,
            )
        if status == "no_marker" and candidate is not None:
            return self._invalid_sample(
                binding,
                run_index=run_index,
                fault="execution_invalid",
                detector_result_digest=detector_result_digest,
                invoked=True,
            )
        candidate_digest: str | None = None
        if candidate is not None:
            try:
                if isinstance(candidate, PlayerCandidate):
                    if (
                        candidate.session_id != binding.packet.session_id
                        or candidate.source_id != binding.packet.source_id
                        or candidate.source_frame_id != binding.packet.frame_id
                        or candidate.observed_at_ns != binding.observed_at_ns
                        or candidate.generation != binding.generation
                        or candidate.pixel_digest != binding.packet.content_hash
                        or candidate.calibration_sha256 != self._calibration.calibration_sha256
                        or candidate.working_size != binding.packet.source_geometry.working_size
                    ):
                        return self._invalid_sample(
                            binding,
                            run_index=run_index,
                            fault="candidate_lineage_mismatch",
                            detector_result_digest=detector_result_digest,
                            invoked=True,
                        )
                    candidate_digest = candidate.digest
                    if evidence is None:
                        evidence = candidate.evidence_digest
                elif isinstance(candidate, str) and _SHA256_RE.fullmatch(candidate) is not None:
                    candidate_digest = candidate
                else:
                    candidate_digest = _value_digest(candidate, "extractor candidate")
            except (PlayerMarkerReplayError, TypeError, ValueError):
                return self._invalid_sample(
                    binding,
                    run_index=run_index,
                    fault="execution_invalid",
                    detector_result_digest=detector_result_digest,
                    invoked=True,
                )
        if status == "detected" and candidate_digest is None:
            return self._invalid_sample(
                binding,
                run_index=run_index,
                fault="execution_invalid",
                detector_result_digest=detector_result_digest,
                invoked=True,
            )
        if evidence is not None:
            try:
                if isinstance(evidence, str) and _SHA256_RE.fullmatch(evidence) is not None:
                    evidence_digest = evidence
                elif hasattr(evidence, "digest") and isinstance(evidence.digest, str):
                    evidence_digest = _sha256(evidence.digest, "evidence_digest")
                else:
                    evidence_digest = _value_digest(evidence, "extractor evidence")
            except (PlayerMarkerReplayError, TypeError, ValueError):
                return self._invalid_sample(
                    binding,
                    run_index=run_index,
                    fault="execution_invalid",
                    detector_result_digest=detector_result_digest,
                    invoked=True,
                )
        if isinstance(candidate, PlayerCandidate) and candidate.evidence_digest != evidence_digest:
            return self._invalid_sample(
                binding,
                run_index=run_index,
                fault="candidate_lineage_mismatch",
                detector_result_digest=detector_result_digest,
                invoked=True,
            )
        return PlayerMarkerReplaySample.build(
            sample_id=sample.sample_id,
            status=status,
            candidate_digest=candidate_digest,
            evidence_digest=evidence_digest,
            detector_result_digest=detector_result_digest,
            detector_state_digest=state_digest,
            detector_config_digest=self.extractor_config_digest,
            invoked=True,
            as_of_offset_ns=self.config.as_of_offset_ns,
            effective_now_ns=binding.effective_now_ns,
            observed_at_ns=binding.observed_at_ns,
            generation=binding.generation,
        )

    def _run_once(self, run_index: int) -> PlayerMarkerReplayRun:
        rows: list[PlayerMarkerReplaySample] = []
        for sample in self._samples:
            records = self._records_for(sample)
            event = records[0]
            # This branch is deliberately before CAS access, packet creation,
            # or extractor invocation for wrong-size negatives.
            if sample.wrong_size:
                rows.append(
                    PlayerMarkerReplaySample.build(
                        sample_id=sample.sample_id,
                        status="rejected",
                        evidence_digest=_digest({"record_hash": event.record_hash}),
                        fault="frame_size_changed",
                        detector_state_digest=self._safe_state_digest(),
                        detector_config_digest=self.extractor_config_digest,
                        invoked=False,
                        as_of_offset_ns=self.config.as_of_offset_ns,
                        effective_now_ns=None,
                        observed_at_ns=None,
                        generation=self.config.generation,
                    )
                )
                continue
            try:
                binding = self._packet_for(sample, event)
            except PlayerMarkerReplayError:
                self._execution_faults.add("execution_invalid")
                rows.append(
                    PlayerMarkerReplaySample.build(
                        sample_id=sample.sample_id,
                        status="fault",
                        evidence_digest=_digest({"record_hash": event.record_hash}),
                        fault="execution_invalid",
                        detector_state_digest=self._safe_state_digest(),
                        detector_config_digest=self.extractor_config_digest,
                        invoked=False,
                        as_of_offset_ns=self.config.as_of_offset_ns,
                        effective_now_ns=None,
                        observed_at_ns=None,
                        generation=self.config.generation,
                    )
                )
                continue
            rows.append(self._extract(binding, run_index))
        return PlayerMarkerReplayRun.build(
            run_index=run_index,
            sample_order_digest=self._sample_order_digest,
            samples=rows,
        )

    def run(self, run_index: int = 1) -> PlayerMarkerReplayRun:
        ensure_positive_int(run_index, "run_index")
        if run_index > PLAYER_MARKER_REPLAY_REPEAT_COUNT:
            raise PlayerMarkerReplayError("run_index must be at most three")
        return self._run_once(run_index)

    replay = run

    def run_repeated(
        self,
        repetitions: int = PLAYER_MARKER_REPLAY_REPEAT_COUNT,
        *,
        source_commit: str | None = None,
        config_digest: str | None = None,
        as_of_ns: int | None = None,
        as_of_offset_ns: int | None = None,
    ) -> PlayerMarkerReplayReport:
        if repetitions != PLAYER_MARKER_REPLAY_REPEAT_COUNT:
            raise PlayerMarkerReplayError("G1-LOC-003B replay requires exactly three runs")
        if (
            source_commit is not None
            and _commit(source_commit, "source_commit") != self.corpus_source_commit
        ):
            raise PlayerMarkerReplayError("source_commit does not match corpus_source_commit")
        if (
            config_digest is not None
            and _sha256(config_digest, "config_digest") != self.extractor_config_digest
        ):
            raise PlayerMarkerReplayError("config_digest does not match extractor config")
        if as_of_ns is not None or as_of_offset_ns is not None:
            if as_of_ns is not None:
                ensure_time_ns(as_of_ns, "as_of_ns")
            if as_of_offset_ns is not None:
                ensure_time_ns(as_of_offset_ns, "as_of_offset_ns")
            requested_offset = (
                as_of_offset_ns
                if as_of_offset_ns is not None
                else cast(int, as_of_ns)
            )
            if as_of_ns not in (None, 0) and as_of_offset_ns not in (None, as_of_ns):
                raise PlayerMarkerReplayError("as_of_ns must equal as_of_offset_ns")
            if requested_offset != self.config.as_of_offset_ns:
                raise PlayerMarkerReplayError(
                    "as_of_offset_ns does not match configured replay"
                )
        self._execution_faults.clear()
        runs = tuple(self._run_once(index) for index in range(1, repetitions + 1))
        semantic = _recompute_replay_semantics(
            runs,
            expected_wrong_size=tuple(item.wrong_size for item in self._samples),
            extractor_config_digest=self.extractor_config_digest,
            as_of_offset_ns=self.config.as_of_offset_ns,
            generation=self.config.generation,
        )
        deterministic = semantic.deterministic
        execution_valid = semantic.execution_valid
        execution_faults = semantic.execution_faults
        report_id = f"player-marker-replay-{self._manifest_digest[:24]}"
        status = "PASS" if deterministic and execution_valid else "FAIL"
        report_body = {
            "schema_version": PLAYER_MARKER_REPLAY_SCHEMA_VERSION,
            "report_type": PLAYER_MARKER_REPLAY_REPORT_TYPE,
            "report_id": report_id,
            "scope": PLAYER_MARKER_REPLAY_SCOPE,
            "truth_scope": TRUTH_SCOPE,
            "verification_profile": self.config.verification_profile,
            "corpus_source_commit": self.corpus_source_commit,
            "replay_source_commit": self.replay_source_commit,
            "manifest_digest": self._manifest_digest,
            "corpus_digest": cast(str, self._manifest["corpus_digest"]),
            "extractor_artifact_digest": self.extractor_artifact_digest,
            "extractor_config_digest": self.extractor_config_digest,
            "as_of_offset_ns": self.config.as_of_offset_ns,
            "as_of_ns": self.config.as_of_ns,
            "generation": self.config.generation,
            "timing_strategy": PLAYER_MARKER_REPLAY_TIMING_STRATEGY,
            "sample_order_digest": self._sample_order_digest,
            "sample_count": len(self._samples),
            "repeat_count": repetitions,
            "deterministic": deterministic,
            "execution_valid": execution_valid,
            "status": status,
            "event_tape_digest": self._event_tape_digest,
            "event_tape_index_artifact_digest": self._event_tape_index_artifact_digest,
            "accepted_ledger_digest": self._accepted_ledger_digest,
            "calibration_artifact_digest": self._calibration.artifact_digest,
            "zero_input_audit_artifact_digest": self._zero_input_audit_artifact_digest,
            "zero_input_audit": to_json_dict(self._zero_input_audit),
            "execution_faults": list(execution_faults),
            "limitations": list(PLAYER_MARKER_REPLAY_LIMITATIONS),
            "runs": [run.to_dict() for run in runs],
        }
        if (
            self._event_tape_index_artifact_digest is None
            and self.config.verification_profile == "contract_fixture"
        ):
            report_body.pop("event_tape_index_artifact_digest")
        return PlayerMarkerReplayReport(
            verification_profile=self.config.verification_profile,
            corpus_source_commit=self.corpus_source_commit,
            replay_source_commit=self.replay_source_commit,
            manifest_digest=self._manifest_digest,
            corpus_digest=cast(str, self._manifest["corpus_digest"]),
            extractor_artifact_digest=self.extractor_artifact_digest,
            extractor_config_digest=self.extractor_config_digest,
            as_of_ns=self.config.as_of_ns,
            as_of_offset_ns=self.config.as_of_offset_ns,
            generation=self.config.generation,
            timing_strategy=PLAYER_MARKER_REPLAY_TIMING_STRATEGY,
            sample_order_digest=self._sample_order_digest,
            sample_count=len(self._samples),
            repeat_count=repetitions,
            deterministic=deterministic,
            execution_valid=execution_valid,
            status=status,
            event_tape_digest=self._event_tape_digest,
            event_tape_index_artifact_digest=self._event_tape_index_artifact_digest,
            accepted_ledger_digest=self._accepted_ledger_digest,
            calibration_artifact_digest=self._calibration.artifact_digest,
            zero_input_audit_artifact_digest=self._zero_input_audit_artifact_digest,
            zero_input_audit=self._zero_input_audit,
            execution_faults=execution_faults,
            limitations=PLAYER_MARKER_REPLAY_LIMITATIONS,
            runs=runs,
            report_digest=_digest(report_body),
            report_id=report_id,
        )

    def run_three_times(self) -> PlayerMarkerReplayReport:
        return self.run_repeated(PLAYER_MARKER_REPLAY_REPEAT_COUNT)


def verify_player_marker_replay_report(
    payload: Mapping[str, Any] | PlayerMarkerReplayReport,
    *,
    source_commit: str | None = None,
    corpus_source_commit: str | None = None,
    replay_source_commit: str | None = None,
    manifest_digest: str | None = None,
    config_digest: str | None = None,
    as_of_ns: int | None = None,
    as_of_offset_ns: int | None = None,
    expected_verification_profile: str | None = None,
    expected_extractor_artifact_digest: str | None = None,
    expected_event_tape_digest: str | None = None,
    expected_event_tape_index_artifact_digest: str | None = None,
    expected_accepted_ledger_digest: str | None = None,
    expected_calibration_artifact_digest: str | None = None,
    expected_zero_input_audit_artifact_digest: str | None = None,
    expected_generation: int | None = None,
    sample_order: Sequence[str] | None = None,
    manifest: Mapping[str, Any] | str | Path | None = None,
) -> None:
    """Validate a report, including strict privacy and verified order binding."""

    data = (
        payload.to_dict()
        if isinstance(payload, PlayerMarkerReplayReport)
        else dict(_mapping(payload, "report"))
    )
    _assert_public_privacy(data)
    report = PlayerMarkerReplayReport.from_dict(data)
    if expected_verification_profile is not None:
        if expected_verification_profile not in PLAYER_MARKER_REPLAY_PROFILES:
            raise PlayerMarkerReplayError("expected verification_profile is unsupported")
        if report.verification_profile != expected_verification_profile:
            raise PlayerMarkerReplayError("report verification_profile mismatch")
    expected_digest_bindings = (
        (
            expected_extractor_artifact_digest,
            report.extractor_artifact_digest,
            "extractor_artifact_digest",
        ),
        (expected_event_tape_digest, report.event_tape_digest, "event_tape_digest"),
        (
            expected_event_tape_index_artifact_digest,
            report.event_tape_index_artifact_digest,
            "event_tape_index_artifact_digest",
        ),
        (
            expected_accepted_ledger_digest,
            report.accepted_ledger_digest,
            "accepted_ledger_digest",
        ),
        (
            expected_calibration_artifact_digest,
            report.calibration_artifact_digest,
            "calibration_artifact_digest",
        ),
        (
            expected_zero_input_audit_artifact_digest,
            report.zero_input_audit_artifact_digest,
            "zero_input_audit_artifact_digest",
        ),
    )
    for expected, actual, field_name in expected_digest_bindings:
        if expected is not None and actual != _sha256(expected, f"expected {field_name}"):
            raise PlayerMarkerReplayError(f"report {field_name} mismatch")
    if expected_generation is not None:
        ensure_non_negative_int(expected_generation, "expected_generation")
        if report.generation != expected_generation:
            raise PlayerMarkerReplayError("report generation mismatch")
    if report.verification_profile == "b2_gate" and report.status == "PASS":
        if manifest is None:
            raise PlayerMarkerReplayError("b2_gate verification requires a canonical manifest")
        if not isinstance(manifest, str | Path):
            raise PlayerMarkerReplayError(
                "b2_gate verification requires a canonical manifest file"
            )
        if expected_verification_profile is None:
            raise PlayerMarkerReplayError(
                "b2_gate verification requires expected verification_profile"
            )
        if expected_extractor_artifact_digest is None:
            raise PlayerMarkerReplayError(
                "b2_gate verification requires expected extractor_artifact_digest"
            )
        if expected_event_tape_digest is None:
            raise PlayerMarkerReplayError(
                "b2_gate verification requires expected event_tape_digest"
            )
        if expected_event_tape_index_artifact_digest is None:
            raise PlayerMarkerReplayError(
                "b2_gate verification requires expected event_tape_index_artifact_digest"
            )
        if expected_accepted_ledger_digest is None:
            raise PlayerMarkerReplayError(
                "b2_gate verification requires expected accepted_ledger_digest"
            )
        if expected_calibration_artifact_digest is None:
            raise PlayerMarkerReplayError(
                "b2_gate verification requires expected calibration_artifact_digest"
            )
        if expected_zero_input_audit_artifact_digest is None:
            raise PlayerMarkerReplayError(
                "b2_gate verification requires expected zero_input_audit_artifact_digest"
            )
        if expected_generation is None:
            raise PlayerMarkerReplayError("b2_gate verification requires expected_generation")
    if source_commit is not None:
        expected = _commit(source_commit, "source_commit")
        if report.corpus_source_commit != expected:
            raise PlayerMarkerReplayError("report corpus source_commit mismatch")
    if corpus_source_commit is not None and report.corpus_source_commit != _commit(
        corpus_source_commit, "corpus_source_commit"
    ):
        raise PlayerMarkerReplayError("report corpus_source_commit mismatch")
    if replay_source_commit is not None and report.replay_source_commit != _commit(
        replay_source_commit, "replay_source_commit"
    ):
        raise PlayerMarkerReplayError("report replay_source_commit mismatch")
    if manifest_digest is not None and report.manifest_digest != _sha256(
        manifest_digest, "manifest_digest"
    ):
        raise PlayerMarkerReplayError("report manifest_digest mismatch")
    if config_digest is not None and report.extractor_config_digest != _sha256(
        config_digest, "config_digest"
    ):
        raise PlayerMarkerReplayError("report config_digest mismatch")
    if as_of_ns is not None or as_of_offset_ns is not None:
        if as_of_ns is not None:
            ensure_time_ns(as_of_ns, "as_of_ns")
        if as_of_offset_ns is not None:
            ensure_time_ns(as_of_offset_ns, "as_of_offset_ns")
        if as_of_ns not in (None, 0) and as_of_offset_ns not in (None, as_of_ns):
            raise PlayerMarkerReplayError("as_of_ns must equal as_of_offset_ns")
        requested_offset = (
            as_of_offset_ns
            if as_of_offset_ns is not None
            else cast(int, as_of_ns)
        )
        if report.as_of_offset_ns != requested_offset:
            raise PlayerMarkerReplayError("report as_of_offset_ns mismatch")
    expected_ids = [item.sample_id for item in report.runs[0].samples]
    manifest_wrong_size: tuple[bool, ...] | None = None
    manifest_live_artifacts: tuple[str, ...] = ()
    if sample_order is not None:
        order = list(sample_order)
        if order != expected_ids or _digest(order) != report.sample_order_digest:
            raise PlayerMarkerReplayError("report sample order is not manifest-bound")
    if manifest is not None:
        manifest_data: Mapping[str, Any]
        manifest_path: Path | None = None
        if isinstance(manifest, str | Path):
            try:
                manifest_path = Path(manifest)
                manifest_data = load_strict_json(manifest_path)
                if manifest_path.read_bytes() != canonical_json(manifest_data) + b"\n":
                    raise ValueError("verified manifest is not canonical JSON")
            except (OSError, ValueError, TypeError) as exc:
                raise PlayerMarkerReplayError("verified manifest is unavailable") from exc
        else:
            manifest_data = _mapping(manifest, "manifest")
        order, manifest_wrong_size, manifest_live_artifacts = _verified_manifest_shape(
            manifest_data,
            manifest_path=manifest_path,
            require_single_live_source=report.verification_profile == "b2_gate",
        )
        expected_manifest_digest = corpus_canonical_digest(manifest_data, omit=("corpus_digest",))
        expected_corpus_digest = _sha256(
            manifest_data.get("corpus_digest"),
            "manifest.corpus_digest",
        )
        if (
            report.manifest_digest != expected_manifest_digest
            or report.corpus_digest != expected_corpus_digest
            or order != expected_ids
        ):
            raise PlayerMarkerReplayError("report IDs are not bound to the verified manifest")
        if report.verification_profile == "b2_gate":
            if len(manifest_live_artifacts) != 1:
                raise PlayerMarkerReplayError(
                    "b2_gate manifest must have exactly one live source artifact"
                )
            if report.zero_input_audit["wheel_sha256"] != manifest_live_artifacts[0]:
                raise PlayerMarkerReplayError(
                    "b2_gate zero-input audit wheel is not the live source artifact"
                )
    if _digest(expected_ids) != report.sample_order_digest:
        raise PlayerMarkerReplayError("sample_order_digest mismatch")
    if any([item.sample_id for item in run.samples] != expected_ids for run in report.runs[1:]):
        raise PlayerMarkerReplayError("runs do not preserve manifest sample order")
    semantic = _recompute_replay_semantics(
        report.runs,
        expected_wrong_size=manifest_wrong_size,
        extractor_config_digest=report.extractor_config_digest,
        as_of_offset_ns=report.as_of_offset_ns,
        generation=report.generation,
    )
    if (
        report.deterministic != semantic.deterministic
        or report.execution_valid != semantic.execution_valid
        or report.execution_faults != semantic.execution_faults
    ):
        raise PlayerMarkerReplayError("report semantic flags/faults are not recomputable")
    if not report.deterministic:
        raise PlayerMarkerReplayDeterminismError("report deterministic flag is false")
    if not report.execution_valid or report.status != "PASS":
        raise PlayerMarkerReplayError("report execution_valid/status is not PASS")
    if any(run.samples != report.runs[0].samples for run in report.runs[1:]):
        raise PlayerMarkerReplayDeterminismError("replay sample results differ across runs")


def run_player_marker_replay(
    manifest: Mapping[str, Any] | str | Path | PlayerMarkerReplayConfig,
    *,
    extractor: PlayerMarkerExtractor | Callable[..., object] | None = None,
    **kwargs: Any,
) -> PlayerMarkerReplayReport:
    """Convenience entry point for the fixed three-run replay."""

    return PlayerMarkerReplayRunner(manifest, extractor=extractor, **kwargs).run_three_times()


MarkerReplayRunner = PlayerMarkerReplayRunner
MarkerReplayReport = PlayerMarkerReplayReport
MarkerReplayRun = PlayerMarkerReplayRun
MarkerReplaySample = PlayerMarkerReplaySample
ReplayConfig = PlayerMarkerReplayConfig


__all__ = [
    "PLAYER_MARKER_REPLAY_LIMITATIONS",
    "PLAYER_MARKER_REPLAY_PROFILES",
    "PLAYER_MARKER_REPLAY_REPEAT_COUNT",
    "PLAYER_MARKER_REPLAY_REPORT_TYPE",
    "PLAYER_MARKER_REPLAY_REPORT_VERSION",
    "PLAYER_MARKER_REPLAY_SCHEMA_VERSION",
    "PLAYER_MARKER_REPLAY_SCOPE",
    "PLAYER_MARKER_REPLAY_TIMING_STRATEGY",
    "EventTapeIndexInput",
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
