"""Strict frame-ingestion corpus, truth, and Event Tape bindings.

The corpus boundary deliberately records ingestion truth only.  A truth row
may describe canonical pixels, geometry admission, derivation, review, and
privacy; it is not a detector, model, world-state, or action label.  Public
manifests contain logical locators and hashes, never host paths or raw pixels.
"""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from maple_automation_core.capture.frame_source import FrameAdmissionResult
from maple_automation_core.capture.pixel_store import PixelSpec, PixelStore, canonical_json
from maple_automation_core.replay.event_tape import EventRecord, EventTape

FRAME_CORPUS_SCHEMA_VERSION = "1.0.0"
TRUTH_SCOPE = "frame_ingestion_only"
B2_MIN_LIVE_SESSION_SAMPLES = 100
_SHA256_LENGTH = 64
_GIT_COMMIT_LENGTH = 40
_ADMISSION = frozenset({"accepted", "suppressed", "fatal"})
_CATEGORIES = frozenset({"static", "motion", "transition", "dark", "crop_edge", "wrong_size"})
_PRIVACY_CLASSES = frozenset({"private", "restricted", "deidentified_public", "hash_only"})
_RETENTION_CLASSES = frozenset({"ephemeral", "candidate", "persistent"})
_SPLITS = ("train", "validation", "test")
_PRIVACY_SCAN_DOMAIN = b"MAPLE_PUBLIC_FRAME_CORPUS_PRIVACY_SCAN_V1\0"
_CORPUS_PROVENANCE_DOMAIN = b"MAPLE_CORPUS_SOURCE_PROVENANCE_V1\0"
_PRIVACY_PATTERNS = (
    re.compile(r"(?i)(?:^|[^A-Za-z])[A-Za-z]:[\\/]"),
    re.compile(r"(?:^|[^:])//(?:[^/]+/)?(?:Users|home)/", re.IGNORECASE),
    re.compile(r"(?:^|[\\/])(?:Users|home)[\\/]", re.IGNORECASE),
    re.compile(r"\\\\[^\\]+\\"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)
PRIVACY_SCAN_CONTRACT_SHA256 = sha256(
    _PRIVACY_SCAN_DOMAIN
    + b"drive-path|user-home|unc-path|email|backslash|absolute-path;canonical-json-v1"
).hexdigest()


class FrameCorpusError(ValueError):
    """Raised when a corpus or truth chain fails closed validation."""


def _reject_constant(value: str) -> None:
    raise FrameCorpusError(f"non-standard JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrameCorpusError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_strict_json(path: str | Path) -> dict[str, Any]:
    """Read one strict JSON object and reject duplicate keys/non-finite values."""

    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise FrameCorpusError(f"JSON artifact is missing, a symlink, or not a file: {target}")
    try:
        value = json.loads(
            target.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FrameCorpusError(f"invalid strict JSON artifact: {target}") from exc
    if not isinstance(value, dict):
        raise FrameCorpusError(f"JSON artifact must be an object: {target}")
    return cast(dict[str, Any], value)


def canonical_digest(payload: Mapping[str, Any], *, omit: Sequence[str] = ()) -> str:
    """Hash canonical JSON after omitting explicitly self-referential keys."""

    body = {key: value for key, value in payload.items() if key not in omit}
    return sha256(canonical_json(body)).hexdigest()


def corpus_source_provenance_id(
    *,
    source_id: str,
    session_id: str,
    source_artifact_sha256: str,
) -> str:
    """Derive a portable provenance identity for one imported source session."""

    body = {
        "source_id": _identifier(source_id, "source_id"),
        "session_id": _identifier(session_id, "session_id"),
        "source_artifact_sha256": _hex(
            source_artifact_sha256,
            "source_artifact_sha256",
        ),
    }
    digest = sha256()
    digest.update(_CORPUS_PROVENANCE_DOMAIN)
    digest.update(canonical_json(body))
    return digest.hexdigest()


def _privacy_findings(value: object, *, field: str = "public") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key in sorted(value):
            _identifier(str(key).replace("/", "-"), "public JSON key")
            findings.extend(_privacy_findings(value[key], field=f"{field}.{key}"))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            findings.extend(_privacy_findings(item, field=f"{field}[{index}]"))
    elif isinstance(value, str) and (
        "\\" in value
        or value.startswith("/")
        or any(pattern.search(value) is not None for pattern in _PRIVACY_PATTERNS)
    ):
        findings.append(field)
    return findings


def public_privacy_summary(
    manifest: Mapping[str, Any],
    truths: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Scan and bind every public manifest/truth value without exposing matches."""

    manifest_view = {
        key: value
        for key, value in manifest.items()
        if key not in {"privacy_summary", "corpus_digest"}
    }
    truth_views = sorted((dict(truth) for truth in truths), key=lambda item: str(item["truth_id"]))
    scan_input = {"manifest": manifest_view, "truths": truth_views}
    findings = _privacy_findings(scan_input)
    digest = sha256()
    digest.update(_PRIVACY_SCAN_DOMAIN)
    digest.update(canonical_json(scan_input))
    return {
        "raw_artifacts_public": False,
        "public_mode": "hash_only",
        "pii_findings": len(findings),
        "scan_contract_sha256": PRIVACY_SCAN_CONTRACT_SHA256,
        "scanned_json_count": 1 + len(truth_views),
        "scan_digest": digest.hexdigest(),
    }


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_exact_keys(
    value: Mapping[str, Any],
    required: set[str],
    field_name: str,
) -> None:
    actual = set(value)
    missing = required - actual
    extra = actual - required
    if missing:
        raise FrameCorpusError(f"{field_name} missing key(s): {sorted(missing)!r}")
    if extra:
        raise FrameCorpusError(f"{field_name} has unexpected key(s): {sorted(extra)!r}")


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FrameCorpusError(f"{field_name} must be an object")
    return cast(Mapping[str, Any], value)


def _array(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise FrameCorpusError(f"{field_name} must be an array")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise FrameCorpusError(f"{field_name} must be a non-empty string")
    return value


def _integer(value: object, field_name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise FrameCorpusError(f"{field_name} must be an integer >= {minimum}")
    return value


def _utc_timestamp(value: object, field_name: str) -> str:
    text = _text(value, field_name)
    if not text.endswith("Z"):
        raise FrameCorpusError(f"{field_name} must be an ISO-8601 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise FrameCorpusError(f"{field_name} must be a valid ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo != UTC:
        raise FrameCorpusError(f"{field_name} must use UTC")
    return text


def _boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise FrameCorpusError(f"{field_name} must be boolean")
    return value


def _hex(value: object, field_name: str, length: int = _SHA256_LENGTH) -> str:
    text = _text(value, field_name)
    if len(text) != length or text.lower() != text:
        raise FrameCorpusError(f"{field_name} must be {length} lowercase hexadecimal characters")
    try:
        bytes.fromhex(text)
    except ValueError as exc:
        raise FrameCorpusError(f"{field_name} must be hexadecimal") from exc
    return text


def _identifier(value: object, field_name: str) -> str:
    text = _text(value, field_name)
    if len(text) > 160 or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in text
    ):
        raise FrameCorpusError(f"{field_name} must be a portable identifier")
    return text


def _safe_relative(root: Path, value: object, field_name: str) -> Path:
    text = _text(value, field_name).replace("\\", "/")
    path = Path(text)
    if path.is_absolute() or ":" in text or any(part in {"", ".", ".."} for part in path.parts):
        raise FrameCorpusError(f"{field_name} must be a normalized relative path")
    root_lexical = root.absolute()
    for component in (*reversed(root_lexical.parents), root_lexical):
        if component.exists():
            attributes = getattr(component.lstat(), "st_file_attributes", 0)
            if component.is_symlink() or attributes & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ):
                raise FrameCorpusError(f"{field_name} root contains a symlink/reparse point")
    root_resolved = root_lexical.resolve(strict=True)
    current = root_resolved
    for part in path.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            raise FrameCorpusError(f"{field_name} does not exist")
        attributes = getattr(current.lstat(), "st_file_attributes", 0)
        if current.is_symlink() or attributes & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise FrameCorpusError(f"{field_name} contains a symlink/reparse point")
    candidate = current.resolve(strict=True)
    if candidate == root_resolved or root_resolved not in candidate.parents:
        raise FrameCorpusError(f"{field_name} escapes the artifact root")
    if not candidate.is_file():
        raise FrameCorpusError(f"{field_name} must resolve to a regular file")
    return candidate


def verify_truth_record(record: Mapping[str, Any]) -> PixelSpec:
    """Semantically verify one strict ingestion-truth record."""

    required = {
        "schema_version",
        "truth_id",
        "truth_scope",
        "sample_id",
        "source_id",
        "session_id",
        "sequence",
        "source_locator",
        "pixel_spec",
        "pixel_digest",
        "pixel_artifact_sha256",
        "source_provenance_id",
        "cas_ref",
        "expected_admission",
        "expected_status",
        "expected_reason_code",
        "category",
        "wrong_size_negative",
        "derivation",
        "privacy",
        "review",
        "record_digest",
    }
    _require_exact_keys(record, required, "truth")
    if record["schema_version"] != FRAME_CORPUS_SCHEMA_VERSION:
        raise FrameCorpusError("truth schema_version mismatch")
    if record["truth_scope"] != TRUTH_SCOPE:
        raise FrameCorpusError("truth_scope must be frame_ingestion_only")
    for field in ("truth_id", "sample_id", "source_id", "session_id"):
        _identifier(record[field], field)
    _integer(record["sequence"], "sequence")

    locator = _mapping(record["source_locator"], "source_locator")
    _require_exact_keys(locator, {"kind", "value", "timing_truth"}, "source_locator")
    locator_kind = locator["kind"]
    if locator_kind not in {"frame_index", "pts_locator", "live_sequence"}:
        raise FrameCorpusError("source_locator.kind is unsupported")
    locator_value = locator["value"]
    if locator_kind in {"frame_index", "live_sequence"}:
        _integer(locator_value, "source_locator.value")
    else:
        pts_value = _text(locator_value, "source_locator.value")
        if re.fullmatch(r"pts:[0-9]+(?:/[1-9][0-9]*)?", pts_value) is None:
            raise FrameCorpusError("PTS locator must be a portable pts:N or pts:N/D token")
    timing_truth = _boolean(locator["timing_truth"], "source_locator.timing_truth")
    if locator_kind == "pts_locator" and timing_truth:
        raise FrameCorpusError("container PTS is an extraction locator, not capture timing truth")

    try:
        spec = PixelSpec.from_dict(_mapping(record["pixel_spec"], "pixel_spec"))
    except (TypeError, ValueError) as exc:
        raise FrameCorpusError("pixel_spec failed Pixel V1 validation") from exc
    digest = _hex(record["pixel_digest"], "pixel_digest")
    _hex(record["pixel_artifact_sha256"], "pixel_artifact_sha256")
    _hex(record["source_provenance_id"], "source_provenance_id")
    if record["cas_ref"] != f"cas://sha256/{digest}":
        raise FrameCorpusError("cas_ref must be uniquely derived from pixel_digest")
    if record["expected_admission"] not in _ADMISSION:
        raise FrameCorpusError("expected_admission is unsupported")
    expected_status = _identifier(record["expected_status"], "expected_status")
    expected_reason_code = _identifier(
        record["expected_reason_code"],
        "expected_reason_code",
    )
    if expected_reason_code != expected_status:
        raise FrameCorpusError("expected_reason_code must equal the frozen admission status code")
    allowed_statuses = {
        "accepted": {"accepted"},
        "suppressed": {"no_frame", "stale"},
        "fatal": {
            "duplicate",
            "out_of_order",
            "timestamp_regression",
            "frame_size_changed",
            "source_mismatch",
            "session_mismatch",
            "clock_domain_mismatch",
            "source_error",
        },
    }
    if expected_status not in allowed_statuses[cast(str, record["expected_admission"])]:
        raise FrameCorpusError("expected_status contradicts expected_admission")
    if record["category"] not in _CATEGORIES:
        raise FrameCorpusError("category is unsupported")
    wrong_size = _boolean(record["wrong_size_negative"], "wrong_size_negative")
    if wrong_size != (record["category"] == "wrong_size"):
        raise FrameCorpusError("wrong_size_negative must exactly match category=wrong_size")
    if wrong_size and record["expected_admission"] != "fatal":
        raise FrameCorpusError("wrong-size truth must expect fatal admission")
    if wrong_size and expected_status != "frame_size_changed":
        raise FrameCorpusError("wrong-size truth must expect frame_size_changed")

    derivation = _mapping(record["derivation"], "derivation")
    _require_exact_keys(
        derivation,
        {
            "source_artifact_sha256",
            "extraction_artifact_sha256",
            "extraction_tool_sha256",
            "extraction_tool_version",
            "parent_pixel_digest",
            "transform_version",
            "calibration_sha256",
            "redaction_mode",
            "redaction_artifact_sha256",
            "deidentified_derivative_sha256",
        },
        "derivation",
    )
    _hex(derivation["source_artifact_sha256"], "source_artifact_sha256")
    _hex(derivation["extraction_artifact_sha256"], "extraction_artifact_sha256")
    _hex(derivation["extraction_tool_sha256"], "extraction_tool_sha256")
    _identifier(derivation["extraction_tool_version"], "extraction_tool_version")
    parent = derivation["parent_pixel_digest"]
    if parent is not None:
        _hex(parent, "parent_pixel_digest")
    _identifier(derivation["transform_version"], "transform_version")
    _hex(derivation["calibration_sha256"], "calibration_sha256")
    redaction_mode = derivation["redaction_mode"]
    redaction_hash = derivation["redaction_artifact_sha256"]
    derivative_hash = derivation["deidentified_derivative_sha256"]
    if redaction_mode == "not_applicable_hash_only":
        if redaction_hash is not None or derivative_hash is not None:
            raise FrameCorpusError("hash-only truth must not claim redaction derivatives")
    elif redaction_mode == "applied_deidentified_derivative":
        _hex(redaction_hash, "redaction_artifact_sha256")
        _hex(derivative_hash, "deidentified_derivative_sha256")
    else:
        raise FrameCorpusError("derivation.redaction_mode is unsupported")

    privacy = _mapping(record["privacy"], "privacy")
    _require_exact_keys(privacy, {"class", "retention", "license_id"}, "privacy")
    if privacy["class"] not in _PRIVACY_CLASSES:
        raise FrameCorpusError("privacy.class is unsupported")
    if privacy["retention"] not in _RETENTION_CLASSES:
        raise FrameCorpusError("privacy.retention is unsupported")
    _identifier(privacy["license_id"], "license_id")

    review = _mapping(record["review"], "review")
    _require_exact_keys(
        review,
        {
            "primary_reviewer_id",
            "primary_decision",
            "independent_reviewer_id",
            "independent_decision",
            "adjudication_id",
        },
        "review",
    )
    _identifier(review["primary_reviewer_id"], "primary_reviewer_id")
    if review["primary_decision"] != "confirmed":
        raise FrameCorpusError("primary review must be confirmed")
    independent_id = review["independent_reviewer_id"]
    independent_decision = review["independent_decision"]
    if (independent_id is None) != (independent_decision is None):
        raise FrameCorpusError("independent reviewer and decision must both be present or null")
    if independent_id is not None:
        _identifier(independent_id, "independent_reviewer_id")
        if independent_id == review["primary_reviewer_id"]:
            raise FrameCorpusError("independent reviewer must differ from the primary reviewer")
        if independent_decision not in {"confirmed", "disputed_then_adjudicated"}:
            raise FrameCorpusError("independent_decision is unsupported")
    adjudication = review["adjudication_id"]
    if independent_decision == "disputed_then_adjudicated":
        _identifier(adjudication, "adjudication_id")
    elif adjudication is not None:
        raise FrameCorpusError("adjudication_id is only valid for an adjudicated dispute")

    expected_digest = canonical_digest(record, omit=("record_digest",))
    if _hex(record["record_digest"], "record_digest") != expected_digest:
        raise FrameCorpusError("truth record_digest mismatch")
    return spec


def _verify_manifest_shape(manifest: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "corpus_id",
        "truth_scope",
        "created_at",
        "source_commit",
        "sources",
        "sessions",
        "samples",
        "splits",
        "review_summary",
        "privacy_summary",
        "limitations",
        "corpus_digest",
    }
    _require_exact_keys(manifest, required, "manifest")
    if manifest["schema_version"] != FRAME_CORPUS_SCHEMA_VERSION:
        raise FrameCorpusError("manifest schema_version mismatch")
    if manifest["truth_scope"] != TRUTH_SCOPE:
        raise FrameCorpusError("manifest truth_scope must be frame_ingestion_only")
    _identifier(manifest["corpus_id"], "corpus_id")
    _utc_timestamp(manifest["created_at"], "created_at")
    _hex(manifest["source_commit"], "source_commit", _GIT_COMMIT_LENGTH)
    _array(manifest["sources"], "sources")
    _array(manifest["sessions"], "sessions")
    _array(manifest["samples"], "samples")
    splits = _mapping(manifest["splits"], "splits")
    _require_exact_keys(splits, set(_SPLITS), "splits")
    for split in _SPLITS:
        _array(splits[split], f"splits.{split}")
    limitations = _array(manifest["limitations"], "limitations")
    for index, limitation in enumerate(limitations):
        _text(limitation, f"limitations[{index}]")
    expected = canonical_digest(manifest, omit=("corpus_digest",))
    if _hex(manifest["corpus_digest"], "corpus_digest") != expected:
        raise FrameCorpusError("corpus_digest mismatch")


def _verify_pixel_derivation_graph(
    pixel_digests: set[str],
    edges: Mapping[str, set[str]],
) -> None:
    """Require manifest-local parents and reject cycles in pixel derivations.

    A truth record can point at a pixel object captured by another truth
    record, but it must not point outside this manifest.  The graph is kept
    separate from the CAS checks because this invariant also applies when a
    caller verifies only the public manifest/truth tree.  Edges are stored as
    sets so repeated occurrences of one pixel digest remain deterministic and
    a future manifest can represent more than one occurrence without losing a
    derivation edge.
    """

    orphan_parents = sorted(
        {parent for parents in edges.values() for parent in parents if parent not in pixel_digests}
    )
    if orphan_parents:
        raise FrameCorpusError(
            "parent_pixel_digest references orphan pixel digest(s) not present in manifest: "
            f"{orphan_parents!r}"
        )

    # Use an explicit DFS stack rather than recursion: manifest size is
    # untrusted, and a long derivation chain must fail as a corpus error rather
    # than as a Python recursion error.
    state: dict[str, int] = {}
    for start in sorted(edges):
        if state.get(start, 0) == 2:
            continue

        state[start] = 1
        path = [start]
        path_index = {start: 0}
        stack: list[tuple[str, Any]] = [(start, iter(sorted(edges.get(start, ()))))]
        while stack:
            node, parents = stack[-1]
            try:
                parent = next(parents)
            except StopIteration:
                state[node] = 2
                stack.pop()
                path_index.pop(node, None)
                path.pop()
                continue

            parent_state = state.get(parent, 0)
            if parent_state == 2:
                continue
            if parent_state == 1:
                cycle_start = path_index[parent]
                cycle = [*path[cycle_start:], parent]
                raise FrameCorpusError(
                    "pixel derivation graph contains a cycle: " + " -> ".join(cycle)
                )

            state[parent] = 1
            path_index[parent] = len(path)
            path.append(parent)
            stack.append((parent, iter(sorted(edges.get(parent, ())))))


def verify_corpus_manifest(
    manifest: Mapping[str, Any],
    *,
    truth_root: str | Path,
    cas_root: str | Path | None = None,
    minimum_samples: int = 1,
    minimum_unique_pixels: int = 1,
    minimum_sessions: int = 1,
    minimum_independent_sessions: int = 0,
    required_independent_fraction_ppm: int = 0,
    require_category_coverage: bool = False,
    require_live_session: bool = False,
) -> dict[str, int | str]:
    """Recompute corpus/truth/CAS links and return verified summary counts."""

    # ``require_category_coverage`` is the historical low-level switch used
    # by callers before profile names were introduced.  It has only ever been
    # used for the B2 six-category gate, so keep that path fail-closed too.
    require_live_session = require_live_session or require_category_coverage
    _verify_manifest_shape(manifest)
    minimum_samples = _integer(minimum_samples, "minimum_samples", minimum=1)
    minimum_unique_pixels = _integer(
        minimum_unique_pixels,
        "minimum_unique_pixels",
        minimum=1,
    )
    minimum_sessions = _integer(minimum_sessions, "minimum_sessions", minimum=1)
    minimum_independent_sessions = _integer(
        minimum_independent_sessions,
        "minimum_independent_sessions",
    )
    required_independent_fraction_ppm = _integer(
        required_independent_fraction_ppm,
        "required_independent_fraction_ppm",
    )
    if required_independent_fraction_ppm > 1_000_000:
        raise FrameCorpusError("required_independent_fraction_ppm must be <= 1000000")

    sources: dict[str, Mapping[str, Any]] = {}
    for index, raw_source in enumerate(_array(manifest["sources"], "sources")):
        source = _mapping(raw_source, f"sources[{index}]")
        _require_exact_keys(
            source,
            {
                "source_id",
                "artifact_sha256",
                "artifact_size",
                "locator_kind",
                "license_id",
                "privacy_class",
                "timing_truth",
            },
            f"sources[{index}]",
        )
        source_id = _identifier(source["source_id"], "source_id")
        if source_id in sources:
            raise FrameCorpusError(f"duplicate source_id: {source_id}")
        _hex(source["artifact_sha256"], "artifact_sha256")
        _integer(source["artifact_size"], "artifact_size", minimum=1)
        if source["locator_kind"] not in {"live_session", "video_container", "raw_fixture"}:
            raise FrameCorpusError("source locator_kind is unsupported")
        _identifier(source["license_id"], "license_id")
        if source["privacy_class"] not in _PRIVACY_CLASSES:
            raise FrameCorpusError("source privacy_class is unsupported")
        _boolean(source["timing_truth"], "timing_truth")
        if source["locator_kind"] == "video_container" and source["timing_truth"]:
            raise FrameCorpusError("video container timing is not capture timing truth")
        sources[source_id] = source

    # B2 is a hardware-backed gate.  A corpus that merely has enough rows,
    # sessions, categories, and review coverage must not be able to claim that
    # gate when every source is a synthetic/raw fixture.  Check the source
    # locator before opening the (potentially very large) truth/CAS tree so the
    # failure is deterministic and explicit.
    if require_live_session and not any(
        source["locator_kind"] == "live_session" for source in sources.values()
    ):
        raise FrameCorpusError(
            "b2_gate corpus has fewer samples from live_session sources: "
            "requires at least one source with locator_kind=live_session"
        )

    sessions: dict[str, Mapping[str, Any]] = {}
    session_to_split: dict[str, str] = {}
    for index, raw_session in enumerate(_array(manifest["sessions"], "sessions")):
        session = _mapping(raw_session, f"sessions[{index}]")
        _require_exact_keys(
            session,
            {"session_id", "source_id", "split", "independent", "sample_count"},
            f"sessions[{index}]",
        )
        session_id = _identifier(session["session_id"], "session_id")
        if session_id in sessions:
            raise FrameCorpusError(f"duplicate session_id: {session_id}")
        if session["source_id"] not in sources:
            raise FrameCorpusError(f"session references unknown source: {session['source_id']}")
        split = _text(session["split"], "split")
        if split not in _SPLITS:
            raise FrameCorpusError("session split is unsupported")
        _boolean(session["independent"], "independent")
        _integer(session["sample_count"], "sample_count", minimum=1)
        sessions[session_id] = session
        session_to_split[session_id] = split

    if len(sessions) < minimum_sessions:
        raise FrameCorpusError("corpus has fewer sessions than required")
    independent_session_ids = {
        session_id for session_id, session in sessions.items() if session["independent"] is True
    }
    if len(independent_session_ids) < minimum_independent_sessions:
        raise FrameCorpusError("corpus has fewer independent source sessions than required")
    splits = _mapping(manifest["splits"], "splits")
    listed_sessions: set[str] = set()
    for split in _SPLITS:
        for raw_session_id in _array(splits[split], f"splits.{split}"):
            session_id = _identifier(raw_session_id, "split session_id")
            if session_id in listed_sessions:
                raise FrameCorpusError("a session appears in more than one split")
            if session_to_split.get(session_id) != split:
                raise FrameCorpusError("split listing disagrees with session split")
            listed_sessions.add(session_id)
    if listed_sessions != set(sessions):
        raise FrameCorpusError("splits must list every session exactly once")

    truth_root_path = Path(truth_root)
    if not truth_root_path.exists() or truth_root_path.is_symlink():
        raise FrameCorpusError("truth_root must be an existing non-symlink directory")
    cas = None if cas_root is None else PixelStore(cas_root)
    sample_ids: set[str] = set()
    truth_ids: set[str] = set()
    pixel_digests: set[str] = set()
    split_digests: dict[str, set[str]] = {split: set() for split in _SPLITS}
    session_counts = {session_id: 0 for session_id in sessions}
    independent_reviewed = 0
    primary_reviewed = 0
    session_sequences: set[tuple[str, int]] = set()
    truth_records: list[Mapping[str, Any]] = []
    observed_categories: set[str] = set()
    wrong_size_negative_count = 0
    derivation_edges: dict[str, set[str]] = {}

    samples = _array(manifest["samples"], "samples")
    if len(samples) < minimum_samples:
        raise FrameCorpusError("corpus has fewer samples than required")
    for index, raw_sample in enumerate(samples):
        sample = _mapping(raw_sample, f"samples[{index}]")
        _require_exact_keys(
            sample,
            {
                "sample_id",
                "truth_id",
                "truth_path",
                "truth_sha256",
                "session_id",
                "sequence",
                "pixel_digest",
                "pixel_artifact_sha256",
                "source_provenance_id",
                "cas_ref",
                "category",
                "wrong_size_negative",
            },
            f"samples[{index}]",
        )
        sample_id = _identifier(sample["sample_id"], "sample_id")
        truth_id = _identifier(sample["truth_id"], "truth_id")
        if sample_id in sample_ids or truth_id in truth_ids:
            raise FrameCorpusError("duplicate sample_id or truth_id")
        sample_ids.add(sample_id)
        truth_ids.add(truth_id)
        session_id = _identifier(sample["session_id"], "session_id")
        if session_id not in sessions:
            raise FrameCorpusError("sample references unknown session")
        sequence = _integer(sample["sequence"], "sequence")
        session_sequence = (session_id, sequence)
        if session_sequence in session_sequences:
            raise FrameCorpusError("duplicate (session_id, sequence) frame identity")
        session_sequences.add(session_sequence)
        digest = _hex(sample["pixel_digest"], "pixel_digest")
        pixel_artifact_sha256 = _hex(
            sample["pixel_artifact_sha256"],
            "pixel_artifact_sha256",
        )
        source_provenance_id = _hex(
            sample["source_provenance_id"],
            "source_provenance_id",
        )
        pixel_digests.add(digest)
        if sample["cas_ref"] != f"cas://sha256/{digest}":
            raise FrameCorpusError("sample cas_ref must be digest-derived")
        split = session_to_split[session_id]
        split_digests[split].add(digest)
        session_counts[session_id] += 1

        truth_path = _safe_relative(truth_root_path, sample["truth_path"], "truth_path")
        if _sha256_file(truth_path) != _hex(sample["truth_sha256"], "truth_sha256"):
            raise FrameCorpusError("truth artifact SHA-256 mismatch")
        truth = load_strict_json(truth_path)
        spec = verify_truth_record(truth)
        truth_records.append(truth)
        observed_categories.add(cast(str, truth["category"]))
        if truth["wrong_size_negative"] is True:
            wrong_size_negative_count += 1
        bindings = {
            "sample_id": sample_id,
            "truth_id": truth_id,
            "session_id": session_id,
            "sequence": sequence,
            "pixel_digest": digest,
            "pixel_artifact_sha256": pixel_artifact_sha256,
            "source_provenance_id": source_provenance_id,
            "cas_ref": sample["cas_ref"],
            "category": sample["category"],
            "wrong_size_negative": sample["wrong_size_negative"],
        }
        for key, expected in bindings.items():
            if truth[key] != expected:
                raise FrameCorpusError(f"truth/manifest binding mismatch: {key}")
        source_id = cast(str, truth["source_id"])
        if sessions[session_id]["source_id"] != source_id:
            raise FrameCorpusError("truth source_id disagrees with session source_id")
        source_locator = _mapping(truth["source_locator"], "source_locator")
        source_row = sources[source_id]
        locator_kind = source_locator["kind"]
        source_kind = source_row["locator_kind"]
        allowed_locator_kinds = {
            "raw_fixture": {"frame_index"},
            "live_session": {"live_sequence"},
            "video_container": {"frame_index", "pts_locator"},
        }[cast(str, source_kind)]
        if locator_kind not in allowed_locator_kinds:
            raise FrameCorpusError("truth locator kind contradicts its source artifact kind")
        if source_locator["timing_truth"] is not source_row["timing_truth"]:
            raise FrameCorpusError("truth timing_truth contradicts its source artifact")
        expected_provenance_id = corpus_source_provenance_id(
            source_id=source_id,
            session_id=session_id,
            source_artifact_sha256=cast(str, sources[source_id]["artifact_sha256"]),
        )
        if source_provenance_id != expected_provenance_id:
            raise FrameCorpusError("source_provenance_id is not derived from source session")
        derivation = _mapping(truth["derivation"], "derivation")
        parent = derivation["parent_pixel_digest"]
        if parent is not None:
            derivation_edges.setdefault(digest, set()).add(cast(str, parent))
        if derivation["source_artifact_sha256"] != sources[source_id]["artifact_sha256"]:
            raise FrameCorpusError("truth source artifact hash disagrees with manifest")
        review = _mapping(truth["review"], "review")
        if (
            require_category_coverage
            and review["independent_decision"] == "disputed_then_adjudicated"
        ):
            raise FrameCorpusError(
                "b2_gate requires a recomputable adjudication artifact for disputed review"
            )
        primary_reviewed += 1
        if review["independent_reviewer_id"] is not None:
            independent_reviewed += 1
        if cas is not None:
            resolved = cas.read(digest, spec)
            if len(resolved) != spec.length:
                raise FrameCorpusError("CAS resolver returned an invalid byte length")
            occurrence = cas.occurrence(
                digest,
                source_provenance_id=source_provenance_id,
                session_id=session_id,
                source_sequence=sequence,
            )
            if occurrence.artifact_sha256 != pixel_artifact_sha256:
                raise FrameCorpusError("CAS occurrence artifact hash mismatch")
            resolved_sha256 = sha256(resolved).hexdigest()
            if occurrence.encoded_sha256 != resolved_sha256:
                raise FrameCorpusError("CAS raw backing hash contradicts resolved bytes")
            if derivation["extraction_artifact_sha256"] != resolved_sha256:
                raise FrameCorpusError("truth extraction hash contradicts resolved CAS bytes")
            truth_privacy = _mapping(truth["privacy"], "privacy")
            if occurrence.privacy_class != truth_privacy["class"]:
                raise FrameCorpusError("CAS occurrence privacy class contradicts truth")
            if occurrence.retention_class != truth_privacy["retention"]:
                raise FrameCorpusError("CAS occurrence retention class contradicts truth")
            if occurrence.privacy_class not in {"private", "restricted"}:
                raise FrameCorpusError(
                    "raw ingestion CAS occurrence must remain private/restricted"
                )
            if occurrence.source_provenance_id != source_provenance_id:
                raise FrameCorpusError("CAS occurrence source provenance contradicts truth")
            if derivation["parent_pixel_digest"] is None:
                if (
                    occurrence.parent_pixel_digest is not None
                    or occurrence.transform_version is not None
                    or occurrence.calibration_sha256 is not None
                ):
                    raise FrameCorpusError("raw CAS occurrence must not assert a derivation")
            elif (
                occurrence.parent_pixel_digest != derivation["parent_pixel_digest"]
                or occurrence.transform_version != derivation["transform_version"]
                or occurrence.calibration_sha256 != derivation["calibration_sha256"]
            ):
                raise FrameCorpusError("derived CAS occurrence contradicts truth derivation")

    _verify_pixel_derivation_graph(pixel_digests, derivation_edges)

    for left_index, left in enumerate(_SPLITS):
        for right in _SPLITS[left_index + 1 :]:
            if split_digests[left] & split_digests[right]:
                raise FrameCorpusError("pixel digest overlap across splits")
    if len(pixel_digests) < minimum_unique_pixels:
        raise FrameCorpusError("corpus has fewer unique pixel objects than required")
    for session_id, expected_count in session_counts.items():
        if expected_count != sessions[session_id]["sample_count"]:
            raise FrameCorpusError("session sample_count mismatch")

    independent_fraction_ppm = independent_reviewed * 1_000_000 // len(samples)
    review_summary = _mapping(manifest["review_summary"], "review_summary")
    _require_exact_keys(
        review_summary,
        {"primary_reviewed", "independent_reviewed", "independent_fraction_ppm"},
        "review_summary",
    )
    expected_review = {
        "primary_reviewed": primary_reviewed,
        "independent_reviewed": independent_reviewed,
        "independent_fraction_ppm": independent_fraction_ppm,
    }
    if dict(review_summary) != expected_review:
        raise FrameCorpusError("review_summary does not match truth records")
    if independent_fraction_ppm < required_independent_fraction_ppm:
        raise FrameCorpusError("independent review fraction is below the required threshold")
    if require_category_coverage and observed_categories != set(_CATEGORIES):
        raise FrameCorpusError("corpus does not cover every required frame category")
    if require_category_coverage and wrong_size_negative_count < 1:
        raise FrameCorpusError("corpus requires at least one wrong_size negative sample")

    live_source_ids = {
        source_id
        for source_id, source in sources.items()
        if source["locator_kind"] == "live_session"
    }
    live_session_ids = {
        session_id
        for session_id, session in sessions.items()
        if session["source_id"] in live_source_ids
    }
    live_session_sample_count = sum(session_counts[session_id] for session_id in live_session_ids)
    if require_live_session and not any(
        session_counts[session_id] >= B2_MIN_LIVE_SESSION_SAMPLES for session_id in live_session_ids
    ):
        raise FrameCorpusError(
            "b2_gate requires at least one live_session session with at least "
            f"{B2_MIN_LIVE_SESSION_SAMPLES} samples"
        )

    privacy = _mapping(manifest["privacy_summary"], "privacy_summary")
    _require_exact_keys(
        privacy,
        {
            "raw_artifacts_public",
            "public_mode",
            "pii_findings",
            "scan_contract_sha256",
            "scanned_json_count",
            "scan_digest",
        },
        "privacy_summary",
    )
    if privacy["raw_artifacts_public"] is not False:
        raise FrameCorpusError("raw artifacts must not be public")
    if privacy["public_mode"] not in {"hash_only", "deidentified_derivatives"}:
        raise FrameCorpusError("privacy_summary.public_mode is unsupported")
    if _integer(privacy["pii_findings"], "pii_findings") != 0:
        raise FrameCorpusError("public corpus evidence has privacy findings")
    expected_privacy = public_privacy_summary(manifest, truth_records)
    if dict(privacy) != expected_privacy:
        raise FrameCorpusError("privacy_summary does not match recomputed public JSON scan")

    summary: dict[str, int | str] = {
        "status": "PASS",
        "source_count": len(sources),
        "session_count": len(sessions),
        "independent_session_count": len(independent_session_ids),
        "sample_count": len(samples),
        "unique_pixel_count": len(pixel_digests),
        "primary_reviewed": primary_reviewed,
        "independent_reviewed": independent_reviewed,
        "independent_fraction_ppm": independent_fraction_ppm,
        "category_count": len(observed_categories),
        "wrong_size_negative_count": wrong_size_negative_count,
    }
    # Keep the B1 fixture summary backwards-compatible while making the B2
    # provenance counters part of every B2 summary/report.  The caller opts in
    # through ``require_live_session`` (and ``verify_corpus_file`` does so for
    # profile=b2_gate).
    if require_live_session:
        summary.update(
            {
                "live_session_count": len(live_session_ids),
                "live_session_sample_count": live_session_sample_count,
            }
        )
    return summary


def verify_corpus_file(
    manifest_path: str | Path,
    *,
    truth_root: str | Path | None = None,
    cas_root: str | Path | None = None,
    minimum_samples: int = 1,
    minimum_unique_pixels: int = 1,
    minimum_sessions: int = 1,
    minimum_independent_sessions: int = 0,
    required_independent_fraction_ppm: int = 0,
    profile: str = "b1_fixture",
) -> dict[str, int | str]:
    """Load and verify a canonical manifest plus every referenced truth artifact."""

    if profile not in {"b1_fixture", "b2_gate"}:
        raise FrameCorpusError("profile must be b1_fixture or b2_gate")
    if profile == "b2_gate":
        if cas_root is None:
            raise FrameCorpusError("b2_gate requires full CAS verification")
        minimum_samples = max(minimum_samples, 300)
        minimum_unique_pixels = max(minimum_unique_pixels, 300)
        minimum_sessions = max(minimum_sessions, 3)
        minimum_independent_sessions = max(minimum_independent_sessions, 3)
        required_independent_fraction_ppm = max(
            required_independent_fraction_ppm,
            200_000,
        )
        require_live_session = True
    else:
        require_live_session = False

    path = Path(manifest_path).resolve(strict=True)
    manifest = load_strict_json(path)
    canonical_file = canonical_json(manifest) + b"\n"
    if path.read_bytes() != canonical_file:
        raise FrameCorpusError("manifest must use canonical JSON plus one LF")
    root = path.parent if truth_root is None else Path(truth_root)
    return verify_corpus_manifest(
        manifest,
        truth_root=root,
        cas_root=cas_root,
        minimum_samples=minimum_samples,
        minimum_unique_pixels=minimum_unique_pixels,
        minimum_sessions=minimum_sessions,
        minimum_independent_sessions=minimum_independent_sessions,
        required_independent_fraction_ppm=required_independent_fraction_ppm,
        require_category_coverage=profile == "b2_gate",
        require_live_session=require_live_session,
    )


def append_admission_to_event_tape(
    tape: EventTape,
    result: FrameAdmissionResult,
    *,
    truth_id: str,
    truth_pixel_digest: str,
    recorded_at_ns: int | None = None,
) -> EventRecord:
    """Bind one admission result to the existing hash-chained Event Tape."""

    if not isinstance(tape, EventTape):
        raise TypeError("tape must be EventTape")
    if not isinstance(result, FrameAdmissionResult):
        raise TypeError("result must be FrameAdmissionResult")
    _identifier(truth_id, "truth_id")
    truth_digest = _hex(truth_pixel_digest, "truth_pixel_digest")
    event = result.event
    if result.accepted:
        event_type = "frame.accepted"
    elif result.fault_latched:
        event_type = "frame.fatal"
    else:
        event_type = "frame.suppressed"
    packet = result.packet
    if packet is not None and packet.content_hash != truth_digest:
        raise FrameCorpusError("accepted packet digest does not match ingestion truth")
    frame_id = event.frame_id if event.frame_id is not None else 0
    payload: dict[str, Any] = {
        "truth_scope": TRUTH_SCOPE,
        "truth_id": truth_id,
        "truth_pixel_digest": truth_digest,
        "admission_status": result.status.value,
        "plan_suppressed": result.plan_suppressed,
        "fault_latched": result.fault_latched,
        "pixel_digest": None if packet is None else packet.content_hash,
        "image_ref": None if packet is None else packet.image_ref,
        "reason": result.reason,
        "reason_code": result.status.value,
    }
    return tape.append(
        event_type=event_type,
        payload=payload,
        session_id=event.session_id,
        frame_id=frame_id,
        world_state_version=0,
        recorded_at_ns=event.observed_at_ns if recorded_at_ns is None else recorded_at_ns,
    )
