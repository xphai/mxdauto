"""Strict verifier for a G1 Frame Candidate packet.

Verification has two deliberately separate modes:

* metadata-only (the public CI default) verifies packet shape, digests,
  cross-links and every local artifact, while leaving restricted external
  locators unresolved;
* full-root verification additionally resolves ``external://`` locators from
  explicitly supplied roots and recomputes their hashes and sizes.

The verifier never starts capture, networking, receiver, keyboard or mouse
code.  It only reads bytes named by a packet.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from maple_automation_core.replay.frame_corpus import FrameCorpusError

try:
    from . import audit_frame_provenance
    from .bundle_common import read_json, safe_relative_path, sha256_file
    from .verify_hardware_smoke_report import verify_hardware_smoke_report
except ImportError:  # pragma: no cover - direct script execution
    import audit_frame_provenance  # type: ignore[import-not-found]
    from bundle_common import (  # type: ignore[import-not-found,no-redef]
        read_json,
        safe_relative_path,
        sha256_file,
    )
    from verify_hardware_smoke_report import (
        verify_hardware_smoke_report,  # type: ignore[import-not-found]
    )


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "g1-frame-candidate-packet.schema.json"
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[A-Fa-f0-9]{40}$")
REQUIRED_EVIDENCE_ROLES = (
    "source_provenance",
    "hardware_smoke",
    "frame_ledger",
    "corpus_manifest",
    "truth_set",
    "deterministic_replay",
    "event_tape",
    "provenance_audit",
    "privacy_audit",
    "zero_input_audit",
)
ZERO_INPUT_FIELDS = {
    "real_input_call_count",
    "core_v2_real_input_call_count",
    "receiver_connect_count",
    "window_write_count",
    "double_write_event_count",
    "keyboard_call_count",
    "mouse_call_count",
}
ZERO_INPUT_BOOLEAN_FIELDS = {"connected", "receiver_connected", "window_write"}


class CandidatePacketVerificationError(ValueError):
    """Raised by :func:`assert_valid` when packet verification fails."""


def _same_digest(left: object, right: object) -> bool:
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()


def canonical_packet_digest(payload: Mapping[str, Any]) -> str:
    """Compute the canonical packet digest excluding the self field."""

    body = dict(payload)
    body.pop("packet_digest", None)
    body.pop("canonical_packet_sha256", None)
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


packet_digest = canonical_packet_digest
compute_packet_digest = canonical_packet_digest
canonical_digest = canonical_packet_digest


def _mapping(value: object) -> Mapping[str, Any] | None:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else None


def _schema_errors(payload: Mapping[str, Any], schema_path: Path) -> list[str]:
    try:
        schema = read_json(schema_path)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        problems = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    except (OSError, ValueError, TypeError) as exc:
        return [f"schema could not be loaded: {exc}"]
    return [f"schema: {error.json_path}: {error.message}" for error in problems]


def _safe_local_path(root: Path, value: object, field: str) -> tuple[Path | None, str | None]:
    if not isinstance(value, str):
        return None, f"{field} path must be a string"
    try:
        relative = safe_relative_path(value)
    except ValueError as exc:
        return None, f"{field} unsafe path: {exc}"
    root_input = root.expanduser()
    if root_input.is_symlink():
        return None, f"{field} root must not be a symlink"
    root_resolved = root_input.resolve()
    candidate = root_resolved.joinpath(*relative.split("/"))
    try:
        parts = candidate.relative_to(root_resolved).parts
    except ValueError:
        return None, f"{field} escaped root"
    current = root_resolved
    for part in parts:
        current /= part
        if current.is_symlink():
            return None, f"{field} traverses a symlink: {relative}"
    return candidate, None


def _controlled_directory(value: Path | str | None, field: str, errors: list[str]) -> Path | None:
    """Resolve an explicitly supplied private root without following links.

    Full candidate verification must not silently substitute the packet's
    metadata locators (or the current working directory) for private evidence.
    Requiring a real directory and auditing every lexical component also keeps
    a later ``verify_report`` call bound to the same bytes the caller selected.
    """

    if value is None:
        errors.append(f"full mode requires an explicit {field}")
        return None
    raw = Path(value).expanduser()
    lexical = Path(os.path.abspath(raw))
    for component in (*reversed(lexical.parents), lexical):
        if not component.exists() and not component.is_symlink():
            continue
        attributes = getattr(component.lstat(), "st_file_attributes", 0)
        if component.is_symlink() or attributes & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            errors.append(f"{field} must be a controlled non-symlink directory")
            return None
    if not lexical.exists():
        errors.append(f"{field} does not exist")
        return None
    if not lexical.is_dir():
        errors.append(f"{field} must be a directory")
        return None
    try:
        return lexical.resolve(strict=True)
    except OSError as exc:
        errors.append(f"{field} could not be resolved: {exc}")
        return None


def _controlled_file(value: Path | str, field: str, errors: list[str]) -> Path | None:
    """Resolve an explicitly supplied Event Tape path without following links."""

    raw = Path(value).expanduser()
    lexical = Path(os.path.abspath(raw))
    for component in (*reversed(lexical.parents), lexical):
        if not component.exists() and not component.is_symlink():
            continue
        attributes = getattr(component.lstat(), "st_file_attributes", 0)
        if component.is_symlink() or attributes & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            errors.append(f"{field} must be a controlled regular file (no symlink/reparse point)")
            return None
    if not lexical.exists() or not lexical.is_file():
        errors.append(f"{field} must be an existing regular file")
        return None
    try:
        return lexical.resolve(strict=True)
    except OSError as exc:
        errors.append(f"{field} could not be resolved: {exc}")
        return None


def _normalise_event_tape_paths(
    values: Sequence[Path | str] | Path | str | None,
) -> list[Path | str]:
    if values is None:
        return []
    if isinstance(values, Path | str):
        return [values]
    return list(values)


def _normalise_full_inputs(
    *,
    private_cas_root: Path | str | None,
    cas_root: Path | str | None,
    truth_root: Path | str | None,
    event_tape_paths: Sequence[Path | str] | Path | str | None,
    event_tapes: Sequence[Path | str] | Path | str | None,
    event_tape_path: Path | str | None,
    errors: list[str],
) -> tuple[Path | None, Path | None, list[Path]]:
    """Normalize API aliases used by callers and the CLI."""

    selected_cas = private_cas_root if private_cas_root is not None else cas_root
    if private_cas_root is not None and cas_root is not None:
        try:
            if (
                Path(private_cas_root).expanduser().resolve()
                != Path(cas_root).expanduser().resolve()
            ):
                errors.append("private_cas_root and cas_root refer to different directories")
        except OSError:
            errors.append("private_cas_root and cas_root could not be resolved consistently")
    selected_events = event_tape_paths if event_tape_paths is not None else event_tapes
    if event_tape_path is not None:
        if selected_events is None:
            selected_events = [event_tape_path]
        else:
            selected_events = [*_normalise_event_tape_paths(selected_events), event_tape_path]

    cas = _controlled_directory(selected_cas, "private CAS root", errors)
    truth = _controlled_directory(truth_root, "truth root", errors)
    raw_events = _normalise_event_tape_paths(selected_events)
    if not raw_events:
        errors.append("full mode requires at least one explicit Event Tape path")
    events: list[Path] = []
    seen: set[Path] = set()
    for index, value in enumerate(raw_events):
        if not isinstance(value, Path | str):
            errors.append(f"event_tape_paths[{index}] must be a filesystem path")
            continue
        path = _controlled_file(value, f"event_tape_paths[{index}]", errors)
        if path is None:
            continue
        if path in seen:
            errors.append(f"event_tape_paths contains duplicate path: {path}")
            continue
        seen.add(path)
        events.append(path)
    return cas, truth, events


def _check_hash_size(
    path: Path, expected_hash: object, expected_size: object, field: str, errors: list[str]
) -> None:
    if not path.is_file():
        errors.append(f"{field} is missing or not a regular file")
        return
    try:
        actual_size = path.stat().st_size
        actual_hash = sha256_file(path)
    except OSError as exc:
        errors.append(f"{field} cannot be read: {exc}")
        return
    if expected_size != actual_size:
        errors.append(f"{field} size mismatch: expected {expected_size}, got {actual_size}")
    if not isinstance(expected_hash, str) or expected_hash.lower() != actual_hash:
        errors.append(f"{field} hash mismatch")


def _external_path(
    uri: str,
    external_roots: Mapping[str, Path],
    field: str,
    errors: list[str],
) -> Path | None:
    if not uri.startswith("external://"):
        errors.append(f"{field} external locator must begin with external://")
        return None
    remainder = uri[len("external://") :]
    root_name, separator, relative = remainder.partition("/")
    if not separator or not root_name or not relative:
        errors.append(f"{field} external locator must contain root name and relative path")
        return None
    root = external_roots.get(root_name)
    if root is None:
        errors.append(f"{field} has no supplied external root: {root_name}")
        return None
    candidate, path_error = _safe_local_path(root, relative, field)
    if path_error:
        errors.append(path_error)
        return None
    return candidate


def _validate_external_locator(uri: object, field: str, errors: list[str]) -> bool:
    """Validate an external URI without requiring access to its private root."""

    if not isinstance(uri, str) or not uri.startswith("external://"):
        errors.append(f"{field} external locator must begin with external://")
        return False
    remainder = uri[len("external://") :]
    root_name, separator, relative = remainder.partition("/")
    if not separator or not root_name or not relative:
        errors.append(f"{field} external locator must contain root name and relative path")
        return False
    try:
        safe_relative_path(relative)
    except ValueError as exc:
        errors.append(f"{field} unsafe external locator: {exc}")
        return False
    return True


def _verify_artifacts(
    packet: Mapping[str, Any],
    *,
    repo_root: Path | None,
    external_roots: Mapping[str, Path],
    metadata_only: bool,
    errors: list[str],
) -> dict[str, Mapping[str, Any]]:
    raw_artifacts = packet.get("artifacts")
    if not isinstance(raw_artifacts, list):
        errors.append("artifacts must be an array")
        return {}
    artifacts: dict[str, Mapping[str, Any]] = {}
    role_counts: dict[str, int] = {}
    for index, raw in enumerate(raw_artifacts):
        artifact = _mapping(raw)
        prefix = f"artifacts[{index}]"
        if artifact is None:
            errors.append(f"{prefix} must be an object")
            continue
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, str):
            errors.append(f"{prefix}.artifact_id must be a string")
            continue
        if artifact_id in artifacts:
            errors.append(f"duplicate artifact_id: {artifact_id}")
            continue
        artifacts[artifact_id] = artifact
        role = artifact.get("role")
        if isinstance(role, str):
            role_counts[role] = role_counts.get(role, 0) + 1
        locator = _mapping(artifact.get("locator"))
        if locator is None:
            errors.append(f"{prefix}.locator must be an object")
            continue
        kind = locator.get("kind")
        if kind == "local":
            if repo_root is None:
                errors.append(f"{prefix} local artifact cannot be verified without repo_root")
                continue
            path, path_error = _safe_local_path(repo_root, locator.get("path"), prefix)
            if path_error:
                errors.append(path_error)
                continue
            assert path is not None
            _check_hash_size(
                path, artifact.get("sha256"), artifact.get("size_bytes"), prefix, errors
            )
        elif kind == "external":
            if artifact.get("restricted") is not True:
                errors.append(f"{prefix} external artifact must be restricted")
            privacy_class = artifact.get("privacy_class")
            if not isinstance(privacy_class, str) or privacy_class not in {
                "restricted",
                "private",
                "hash_only",
            }:
                errors.append(f"{prefix} external artifact privacy class is not restricted")
            uri = locator.get("uri")
            if not _validate_external_locator(uri, prefix, errors):
                continue
            if metadata_only:
                continue
            assert isinstance(uri, str)
            path = _external_path(uri, external_roots, prefix, errors)
            if path is not None:
                _check_hash_size(
                    path, artifact.get("sha256"), artifact.get("size_bytes"), prefix, errors
                )
        else:
            errors.append(f"{prefix}.locator.kind is unsupported")
    for role in REQUIRED_EVIDENCE_ROLES:
        if role_counts.get(role, 0) != 1:
            errors.append(
                f"artifact role {role!r} must occur exactly once (got {role_counts.get(role, 0)})"
            )
    return artifacts


def _ref_artifact(
    value: object,
    *,
    role: str,
    artifacts: Mapping[str, Mapping[str, Any]],
    errors: list[str],
) -> Mapping[str, Any] | None:
    ref = _mapping(value)
    if ref is None:
        errors.append(f"evidence.{role} must be an artifact reference object")
        return None
    artifact_id = ref.get("artifact_id")
    if not isinstance(artifact_id, str) or artifact_id not in artifacts:
        errors.append(f"evidence.{role} references an unknown artifact: {artifact_id!r}")
        return None
    artifact = artifacts[artifact_id]
    if not _same_digest(ref.get("sha256"), artifact.get("sha256")):
        errors.append(f"evidence.{role} SHA-256 does not match artifact {artifact_id}")
    artifact_role = artifact.get("role")
    if artifact_role != role:
        errors.append(f"evidence.{role} role mismatch: artifact role is {artifact_role!r}")
    return artifact


def _zero_input_scan(value: object, field: str, errors: list[str]) -> None:
    """Find input-policy contradictions in nested JSON evidence."""

    if isinstance(value, Mapping):
        if "input_owner" in value and value.get("input_owner") != "legacy":
            errors.append(f"{field}.input_owner contradicts legacy ownership")
        if "real_input_enabled" in value and value.get("real_input_enabled") is not False:
            errors.append(f"{field}.real_input_enabled contradicts zero-input policy")
        for key, child in value.items():
            if key in ZERO_INPUT_FIELDS and (type(child) is not int or child != 0):
                errors.append(f"{field}.{key} contradicts zero-input policy (expected 0)")
            if key in ZERO_INPUT_BOOLEAN_FIELDS and child is not False:
                errors.append(f"{field}.{key} contradicts zero-input policy (expected false)")
            _zero_input_scan(child, f"{field}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _zero_input_scan(child, f"{field}[{index}]", errors)


def _privacy_scan(value: object, field: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        if value.get("raw_artifacts_public") is True:
            errors.append(f"{field}.raw_artifacts_public is true")
        for key in ("pii_findings", "privacy_failures", "failure_count"):
            child = value.get(key)
            if key in value and (type(child) is not int or child != 0):
                errors.append(f"{field}.{key} must be zero")
        privacy_status = value.get("privacy_status")
        if privacy_status is not None and (
            not isinstance(privacy_status, str) or privacy_status not in {"PASS", "passed", "pass"}
        ):
            errors.append(f"{field}.privacy_status is not PASS")
        for key, child in value.items():
            _privacy_scan(child, f"{field}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _privacy_scan(child, f"{field}[{index}]", errors)


def _load_json_artifact(
    artifact: Mapping[str, Any],
    *,
    repo_root: Path | None,
    external_roots: Mapping[str, Path],
    metadata_only: bool,
    errors: list[str],
    field: str,
) -> tuple[dict[str, Any] | None, Path | None]:
    locator = _mapping(artifact.get("locator"))
    if locator is None:
        return None, None
    path: Path | None = None
    if locator.get("kind") == "local":
        if repo_root is None:
            return None, None
        path, path_error = _safe_local_path(repo_root, locator.get("path"), field)
        if path_error:
            errors.append(path_error)
            return None, None
    elif locator.get("kind") == "external":
        if metadata_only:
            return None, None
        uri = locator.get("uri")
        if isinstance(uri, str):
            path = _external_path(uri, external_roots, field, errors)
    if path is None or not path.is_file() or path.suffix.lower() != ".json":
        return None, path
    try:
        value = read_json(path)
    except (OSError, ValueError) as exc:
        errors.append(f"{field} JSON is invalid: {exc}")
        return None, path
    return value, path


def _generic_digest(value: Mapping[str, Any]) -> str | None:
    digest_fields = (
        "report_digest",
        "canonical_report_sha256",
        "record_digest",
        "corpus_digest",
        "snapshot_digest",
        "audit_digest",
    )
    present = [field for field in digest_fields if field in value]
    if not present:
        return None
    body = dict(value)
    for field in present:
        body.pop(field, None)
    return sha256(
        json.dumps(
            body, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def _verify_contract_links(
    packet: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
    errors: list[str],
) -> None:
    """Ensure contract and B1 metadata references point at artifact rows."""

    b1 = _mapping(packet.get("b1_binding"))
    if b1 is None:
        return
    wheel = _mapping(b1.get("wheel"))
    ci_run = _mapping(b1.get("ci_run"))
    if ci_run is None or ci_run.get("status") != "success":
        errors.append("b1_binding.ci_run.status must be success")
    wheel_artifact = artifacts.get("b1-wheel")
    if wheel is None or wheel_artifact is None:
        errors.append("b1_binding.wheel must be bound to the b1-wheel artifact")
    elif not _same_digest(wheel.get("sha256"), wheel_artifact.get("sha256")):
        errors.append("b1_binding.wheel.sha256 does not match b1-wheel artifact")
    elif wheel.get("size_bytes") != wheel_artifact.get("size_bytes"):
        errors.append("b1_binding.wheel.size_bytes does not match b1-wheel artifact")
    elif (artifact_locator := _mapping(wheel_artifact.get("locator"))) is None:
        errors.append("b1-wheel artifact locator is malformed")
    else:
        expected_locator = (
            artifact_locator.get("path")
            if artifact_locator.get("kind") == "local"
            else artifact_locator.get("uri")
        )
        if wheel.get("locator") != expected_locator:
            errors.append("b1_binding.wheel.locator does not match b1-wheel artifact")
    lock_artifact = artifacts.get("dependency-lock")
    if lock_artifact is None:
        errors.append("b1 dependency lock must be bound to the dependency-lock artifact")
    elif not _same_digest(b1.get("dependency_lock_sha256"), lock_artifact.get("sha256")):
        errors.append("b1 dependency lock hash does not match dependency-lock artifact")

    contracts = _mapping(packet.get("contracts"))
    if contracts is None:
        errors.append("contracts must be an object")
        return
    for contract_name, expected_id, expected_role in (
        ("capture_config", "capture-config", "capture-config"),
        ("calibration", "calibration", "calibration"),
    ):
        ref = _mapping(contracts.get(contract_name))
        artifact = artifacts.get(expected_id)
        if ref is None or artifact is None:
            errors.append(f"contracts.{contract_name} must be bound to {expected_id}")
            continue
        if ref.get("artifact_id") != expected_id or not _same_digest(
            ref.get("sha256"), artifact.get("sha256")
        ):
            errors.append(f"contracts.{contract_name} hash/id does not match artifact")
        if artifact.get("role") != expected_role:
            errors.append(f"contracts.{contract_name} artifact role mismatch")
        locator = _mapping(artifact.get("locator"))
        if (
            locator is not None
            and locator.get("kind") == "local"
            and ref.get("path") != locator.get("path")
        ):
            errors.append(f"contracts.{contract_name}.path does not match artifact locator")

    pixel = _mapping(contracts.get("pixel_contract"))
    pixel_artifact = artifacts.get("pixel-contract-schema")
    if pixel is None or pixel_artifact is None:
        errors.append("contracts.pixel_contract must be bound to pixel-contract-schema")
    else:
        if not _same_digest(pixel.get("schema_sha256"), pixel_artifact.get("sha256")):
            errors.append("contracts.pixel_contract.schema_sha256 does not match artifact")
        locator = _mapping(pixel_artifact.get("locator"))
        if (
            locator is not None
            and locator.get("kind") == "local"
            and pixel.get("schema_path") != locator.get("path")
        ):
            errors.append("contracts.pixel_contract.schema_path does not match artifact locator")


def _normalise_evidence_role(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.casefold().replace("-", "_")


def _verify_full_b2_cross_links(
    packet: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
    loaded: Mapping[str, Mapping[str, Any]],
    loaded_paths: Mapping[str, Path],
    hardware: Mapping[str, Any] | None,
    *,
    full_cas_root: Path | None,
    full_truth_root: Path | None,
    full_event_tapes: Sequence[Path],
    errors: list[str],
) -> None:
    """Bind the live corpus, VC-003 report, and packet artifacts in full mode."""

    manifest = loaded.get("corpus_manifest")
    provenance = loaded.get("source_provenance")
    if manifest is None:
        return
    raw_sources = manifest.get("sources")
    raw_sessions = manifest.get("sessions")
    sources = (
        [row for row in raw_sources if isinstance(row, Mapping)]
        if isinstance(raw_sources, list)
        else []
    )
    sessions = (
        [row for row in raw_sessions if isinstance(row, Mapping)]
        if isinstance(raw_sessions, list)
        else []
    )
    live_source_ids = {
        cast(str, row.get("source_id"))
        for row in sources
        if row.get("locator_kind") == "live_session" and isinstance(row.get("source_id"), str)
    }
    live_sessions = {
        cast(str, row.get("session_id")): row
        for row in sessions
        if row.get("source_id") in live_source_ids and isinstance(row.get("session_id"), str)
    }
    if not live_source_ids:
        errors.append("full B2 cross-link requires a live_session corpus source")
    if not any(
        type(row.get("sample_count")) is int and cast(int, row["sample_count"]) >= 100
        for row in live_sessions.values()
    ):
        errors.append("full B2 cross-link requires a live session with at least 100 samples")

    window = _mapping(None if hardware is None else hardware.get("measurement_window"))
    hardware_session = None if window is None else window.get("session_id")
    if hardware_session not in live_sessions:
        errors.append(
            "hardware measurement_window.session_id must identify a live_session corpus session"
        )
    session_row = live_sessions.get(cast(str, hardware_session))
    source_id = None if session_row is None else session_row.get("source_id")
    source_row = next(
        (row for row in sources if row.get("source_id") == source_id),
        None,
    )
    if source_row is None:
        errors.append("live session source binding is missing from the corpus manifest")

    hardware_source = _mapping(None if hardware is None else hardware.get("source"))
    hardware_logical_source = (
        None if hardware_source is None else hardware_source.get("logical_source_id")
    )
    if hardware_logical_source != source_id:
        errors.append("hardware source.logical_source_id does not match the live corpus source_id")

    if provenance is not None:
        if provenance.get("session_id") != hardware_session:
            errors.append(
                "source_provenance.session_id does not match measurement_window.session_id"
            )
        if provenance.get("source_id") != source_id:
            errors.append("source_provenance.source_id does not match the live corpus source")
        if provenance.get("source_id") != hardware_logical_source:
            errors.append(
                "source_provenance.source_id does not match hardware source.logical_source_id"
            )
        expected_source_artifact = None if source_row is None else source_row.get("artifact_sha256")
        if not _same_digest(provenance.get("source_artifact_sha256"), expected_source_artifact):
            errors.append(
                "source_provenance.source_artifact_sha256 does not match the live source artifact"
            )
        hardware_fingerprint = (
            None if hardware_source is None else hardware_source.get("device_fingerprint_sha256")
        )
        if not _same_digest(
            provenance.get("physical_device_fingerprint_sha256"),
            hardware_fingerprint,
        ):
            errors.append(
                "source_provenance device fingerprint does not match hardware source fingerprint"
            )

    b1 = _mapping(packet.get("b1_binding"))
    wheel = _mapping(None if b1 is None else b1.get("wheel"))
    packet_wheel_sha = None if wheel is None else wheel.get("sha256")
    if hardware is None or not _same_digest(hardware.get("wheel_sha256"), packet_wheel_sha):
        errors.append("hardware report wheel_sha256 does not match packet b1 wheel artifact")

    # The packet carries one artifact row per evidence role.  A hardware
    # report may list producer artifacts as well; in full B2 both frame ledger
    # and Event Tape must be present exactly once and have the same hashes as
    # the packet rows.  This catches a re-signed report that swaps either file.
    hardware_artifacts = []
    if hardware is not None and isinstance(hardware.get("artifacts"), list):
        hardware_artifacts = [
            row for row in cast(list[Any], hardware["artifacts"]) if isinstance(row, Mapping)
        ]
    for expected_role in ("frame_ledger", "event_tape"):
        matches = [
            row
            for row in hardware_artifacts
            if any(
                _normalise_evidence_role(row.get(field)) == expected_role
                for field in ("role", "artifact_id")
            )
        ]
        if len(matches) != 1:
            errors.append(
                f"full B2 hardware report artifacts must contain exactly one {expected_role}"
            )
            continue
        packet_rows = [
            row
            for row in artifacts.values()
            if _normalise_evidence_role(row.get("role")) == expected_role
        ]
        if len(packet_rows) != 1:
            errors.append(f"packet must contain exactly one {expected_role} artifact")
            continue
        if not _same_digest(matches[0].get("sha256"), packet_rows[0].get("sha256")):
            errors.append(
                f"hardware report {expected_role} artifact hash does not match packet artifact"
            )

    packet_event_rows = [
        row
        for row in artifacts.values()
        if _normalise_evidence_role(row.get("role")) == "event_tape"
    ]
    if len(full_event_tapes) == 1 and len(packet_event_rows) == 1:
        try:
            actual_event_hash = sha256_file(full_event_tapes[0])
        except OSError as exc:
            errors.append(f"explicit Event Tape path could not be hashed: {exc}")
        else:
            if not _same_digest(actual_event_hash, packet_event_rows[0].get("sha256")):
                errors.append(
                    "explicit Event Tape path hash does not match packet event_tape artifact"
                )

    # A signed report is not sufficient evidence.  Rebuild it from the
    # caller-supplied manifest, truth root, private CAS and Event Tape paths;
    # this is deliberately delegated to the canonical audit implementation so
    # a correctly re-signed contradiction remains rejected.
    audit = loaded.get("provenance_audit")
    manifest_path = loaded_paths.get("corpus_manifest")
    if (
        audit is None
        or manifest_path is None
        or full_cas_root is None
        or full_truth_root is None
        or not full_event_tapes
    ):
        return
    try:
        audit_frame_provenance.verify_report(
            audit,
            manifest_path=manifest_path,
            truth_root=full_truth_root,
            event_tapes=full_event_tapes,
            cas_root=full_cas_root,
            schema_path=ROOT / "schemas" / "frame-provenance-audit-report.schema.json",
        )
    except (FrameCorpusError, OSError, TypeError, ValueError, KeyError) as exc:
        errors.append(f"full B2 provenance audit verify_report recomputation failed: {exc}")


def _verify_cross_links(
    packet: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
    *,
    repo_root: Path | None,
    external_roots: Mapping[str, Path],
    metadata_only: bool,
    full_cas_root: Path | None,
    full_truth_root: Path | None,
    full_event_tapes: Sequence[Path],
    errors: list[str],
) -> None:
    evidence = _mapping(packet.get("evidence"))
    if evidence is None:
        errors.append("evidence must be an object")
        return
    loaded: dict[str, dict[str, Any]] = {}
    loaded_paths: dict[str, Path] = {}
    for role in REQUIRED_EVIDENCE_ROLES:
        artifact = _ref_artifact(evidence.get(role), role=role, artifacts=artifacts, errors=errors)
        if artifact is None:
            continue
        value, artifact_path = _load_json_artifact(
            artifact,
            repo_root=repo_root,
            external_roots=external_roots,
            metadata_only=metadata_only,
            errors=errors,
            field=f"evidence.{role}",
        )
        if value is not None:
            loaded[role] = value
            if artifact_path is not None:
                loaded_paths[role] = artifact_path
            _zero_input_scan(value, f"evidence.{role}", errors)
            _privacy_scan(value, f"evidence.{role}", errors)
            privacy_status = value.get("status", value.get("privacy_status"))
            if (
                role == "privacy_audit"
                and privacy_status is not None
                and (
                    not isinstance(privacy_status, str)
                    or privacy_status not in {"PASS", "passed", "pass"}
                )
            ):
                errors.append("evidence.privacy_audit.status is not PASS")
            digest = _generic_digest(value)
            if digest is not None:
                for digest_field in (
                    "report_digest",
                    "canonical_report_sha256",
                    "record_digest",
                    "corpus_digest",
                    "snapshot_digest",
                    "audit_digest",
                ):
                    if digest_field in value and not _same_digest(value.get(digest_field), digest):
                        errors.append(
                            f"evidence.{role}.{digest_field} does not match canonical content"
                        )

    b1 = _mapping(packet.get("b1_binding"))
    if b1 is None:
        return
    source_commit = b1.get("source_commit")
    wheel = _mapping(b1.get("wheel"))
    wheel_sha = None if wheel is None else wheel.get("sha256")
    lock_sha = b1.get("dependency_lock_sha256")
    contracts = _mapping(packet.get("contracts"))
    config = _mapping(None if contracts is None else contracts.get("capture_config"))
    calibration = _mapping(None if contracts is None else contracts.get("calibration"))
    config_sha = None if config is None else config.get("sha256")
    calibration_sha = None if calibration is None else calibration.get("sha256")
    for role, value in loaded.items():
        report_source = value.get("source_commit")
        if report_source is not None and str(report_source).lower() != str(source_commit).lower():
            errors.append(f"evidence.{role}.source_commit does not match B1 source commit")
        for key, expected in (
            ("wheel_sha256", wheel_sha),
            ("dependency_lock_sha256", lock_sha),
            ("config_sha256", config_sha),
            ("calibration_sha256", calibration_sha),
        ):
            if key in value and expected is not None:
                actual = value.get(key)
                matches = (
                    _same_digest(actual, expected) if key.endswith("sha256") else actual == expected
                )
                if not matches:
                    errors.append(f"evidence.{role}.{key} does not match packet binding")
        # A number of provenance reports use a nested bindings object rather
        # than generic top-level fields; check those without weakening the
        # top-level form.
        bindings = _mapping(value.get("b1_binding")) or _mapping(value.get("bindings"))
        if bindings is not None:
            binding_source = bindings.get("source_commit")
            if (
                binding_source is not None
                and str(binding_source).lower() != str(source_commit).lower()
            ):
                errors.append(f"evidence.{role}.b1_binding.source_commit mismatch")
            binding_wheel = bindings.get("wheel_sha256")
            if binding_wheel is not None and not _same_digest(binding_wheel, wheel_sha):
                errors.append(f"evidence.{role}.b1_binding.wheel_sha256 mismatch")
            binding_lock = bindings.get("dependency_lock_sha256")
            if binding_lock is not None and not _same_digest(binding_lock, lock_sha):
                errors.append(f"evidence.{role}.b1_binding.dependency_lock_sha256 mismatch")
    # A hardware report is independently checked, including its own status,
    # counters and rate recomputation.  External reports are intentionally
    # deferred in metadata-only mode.
    hardware_artifact = _ref_artifact(
        evidence.get("hardware_smoke"), role="hardware_smoke", artifacts=artifacts, errors=[]
    )
    hardware_value: dict[str, Any] | None = None
    if hardware_artifact is not None:
        value, artifact_path = _load_json_artifact(
            hardware_artifact,
            repo_root=repo_root,
            external_roots=external_roots,
            metadata_only=metadata_only,
            errors=errors,
            field="evidence.hardware_smoke",
        )
        if value is not None:
            hardware_value = value
            if artifact_path is not None:
                loaded_paths["hardware_smoke"] = artifact_path
            errors.extend(
                f"hardware smoke: {error}"
                for error in verify_hardware_smoke_report(
                    value,
                    repo_root=repo_root,
                    external_roots=external_roots,
                    metadata_only=metadata_only,
                )
            )

    if not metadata_only:
        audit = loaded.get("provenance_audit")
        if audit is None:
            errors.append("full B2 verification requires a readable provenance_audit JSON")
        else:
            if audit.get("status") != "PASS":
                errors.append("full B2 provenance_audit.status must be PASS")
            if audit.get("verification_profile") != "b2_gate":
                errors.append("full B2 provenance audit must use verification_profile=b2_gate")
            cas_verification = _mapping(audit.get("cas_verification"))
            if cas_verification is None or cas_verification.get("mode") != "full_cas":
                errors.append("full B2 provenance audit must recompute the private CAS")
            corpus_summary = _mapping(audit.get("corpus"))
            if corpus_summary is None:
                errors.append("full B2 provenance audit is missing corpus summary")
            else:
                for field, minimum in (
                    ("sample_count", 300),
                    ("unique_pixel_count", 300),
                    ("session_count", 3),
                    ("independent_session_count", 3),
                    ("independent_fraction_ppm", 200_000),
                    ("category_count", 6),
                    ("wrong_size_negative_count", 1),
                    ("live_session_count", 1),
                    ("live_session_sample_count", 100),
                ):
                    value = corpus_summary.get(field)
                    if type(value) is not int or value < minimum:
                        errors.append(
                            f"full B2 provenance audit {field} must be at least {minimum}"
                        )

        corpus_manifest = loaded.get("corpus_manifest")
        if corpus_manifest is None:
            errors.append("full B2 verification requires a readable corpus_manifest JSON")
        else:
            samples = corpus_manifest.get("samples")
            sessions = corpus_manifest.get("sessions")
            if not isinstance(samples, list) or len(samples) < 300:
                errors.append("full B2 corpus manifest must contain at least 300 samples")
            else:
                pixel_digests = {
                    row.get("pixel_digest")
                    for row in samples
                    if isinstance(row, Mapping) and isinstance(row.get("pixel_digest"), str)
                }
                if len(pixel_digests) < 300:
                    errors.append("full B2 corpus manifest must contain 300 unique pixels")
            if not isinstance(sessions, list) or len(sessions) < 3:
                errors.append("full B2 corpus manifest must contain at least three sessions")
            elif (
                sum(
                    1
                    for row in sessions
                    if isinstance(row, Mapping) and row.get("independent") is True
                )
                < 3
            ):
                errors.append("full B2 corpus manifest must contain three independent sessions")

        provenance = loaded.get("source_provenance")
        if provenance is None:
            errors.append("full B2 verification requires readable source_provenance JSON")
        else:
            backend_version = provenance.get("backend_version")
            if not isinstance(backend_version, str) or backend_version.casefold() in {
                "unknown",
                "unbound-offline",
                "unmeasured",
            }:
                errors.append("full B2 source provenance requires a measured backend version")
            fingerprint = provenance.get("physical_device_fingerprint_sha256")
            if fingerprint in {None, "0" * 64, sha256(b"unknown").hexdigest()}:
                errors.append("full B2 source provenance requires a measured device fingerprint")
            requested = _mapping(provenance.get("requested"))
            negotiated = _mapping(provenance.get("negotiated"))
            if requested is None or negotiated is None or dict(requested) != dict(negotiated):
                errors.append("full B2 requested and negotiated source formats must match exactly")

        _verify_full_b2_cross_links(
            packet,
            artifacts,
            loaded,
            loaded_paths,
            hardware_value,
            full_cas_root=full_cas_root,
            full_truth_root=full_truth_root,
            full_event_tapes=full_event_tapes,
            errors=errors,
        )


def _verify_baseline(
    packet: Mapping[str, Any],
    *,
    repo_root: Path | None,
    errors: list[str],
) -> None:
    baseline = _mapping(packet.get("g0_baseline"))
    if baseline is None:
        errors.append("g0_baseline must be an object")
        return
    if baseline.get("immutable") is not True:
        errors.append("G0 baseline must be marked immutable")
    for field in ("source_commit",):
        value = baseline.get(field)
        if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
            errors.append(f"g0_baseline.{field} is not a git commit")
    for field in ("packet_sha256", "bundle_sha256"):
        value = baseline.get(field)
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            errors.append(f"g0_baseline.{field} is not a SHA-256 digest")
    if repo_root is None:
        return
    for ref_field, hash_field in (("packet_ref", "packet_sha256"), ("bundle_ref", "bundle_sha256")):
        path, path_error = _safe_local_path(
            repo_root, baseline.get(ref_field), f"g0_baseline.{ref_field}"
        )
        if path_error:
            errors.append(path_error)
            continue
        assert path is not None
        _check_hash_size(
            path,
            baseline.get(hash_field),
            path.stat().st_size if path.exists() else None,
            f"g0_baseline.{ref_field}",
            errors,
        )


def verify_g1_frame_candidate(
    packet: Mapping[str, Any] | Path | str,
    *,
    repo_root: Path | None = None,
    external_roots: Mapping[str, Path | str] | None = None,
    metadata_only: bool = True,
    private_cas_root: Path | str | None = None,
    truth_root: Path | str | None = None,
    event_tape_paths: Sequence[Path | str] | Path | str | None = None,
    # Compatibility aliases for callers that use the audit tool's shorter
    # parameter names.
    cas_root: Path | str | None = None,
    event_tapes: Sequence[Path | str] | Path | str | None = None,
    event_tape_path: Path | str | None = None,
    schema_path: Path = SCHEMA_PATH,
) -> list[str]:
    """Return all errors in a candidate packet.

    ``repo_root`` is required to verify local packet artifacts.
    ``external_roots`` maps the root name in ``external://ROOT/path`` to a
    private filesystem root.  Full mode additionally requires explicit
    ``private_cas_root``, ``truth_root`` and one or more ``event_tape_paths``;
    those paths are passed to :func:`audit_frame_provenance.verify_report` for
    a fresh manifest/truth/CAS/Event Tape recomputation.
    """

    if isinstance(packet, Path | str):
        try:
            packet = read_json(Path(packet).resolve(strict=True))
        except (OSError, ValueError) as exc:
            return [f"packet could not be read: {exc}"]
    if not isinstance(packet, Mapping):
        return ["packet must be a JSON object"]
    payload = cast(Mapping[str, Any], packet)
    schema_path = Path(schema_path)
    errors = _schema_errors(payload, schema_path)
    full_cas_root: Path | None = None
    full_truth_root: Path | None = None
    full_event_tapes: list[Path] = []
    if not metadata_only:
        (
            full_cas_root,
            full_truth_root,
            full_event_tapes,
        ) = _normalise_full_inputs(
            private_cas_root=private_cas_root,
            cas_root=cas_root,
            truth_root=truth_root,
            event_tape_paths=event_tape_paths,
            event_tapes=event_tapes,
            event_tape_path=event_tape_path,
            errors=errors,
        )
    try:
        expected_digest = canonical_packet_digest(payload)
    except (TypeError, ValueError) as exc:
        expected_digest = None
        errors.append(f"packet canonical digest could not be computed: {exc}")
    if expected_digest is None or payload.get("packet_digest") != expected_digest:
        errors.append("packet_digest does not match canonical packet content")
    if payload.get("overall_g1_state") != "In Progress":
        errors.append("overall_g1_state must remain exactly 'In Progress'")
    if payload.get("scope") != "G1-FRM":
        errors.append("scope must be G1-FRM")
    input_audit = _mapping(payload.get("input_audit"))
    expected_input: dict[str, object] = {
        "input_owner": "legacy",
        "real_input_enabled": False,
        "real_input_call_count": 0,
        "core_v2_real_input_call_count": 0,
        "receiver_connect_count": 0,
        "window_write_count": 0,
        "double_write_event_count": 0,
    }
    if input_audit is None:
        errors.append("input_audit must be an object")
    else:
        for field, expected in expected_input.items():
            actual = input_audit.get(field)
            matches = (
                type(actual) is int and actual == expected
                if type(expected) is int
                else actual == expected
            )
            if not matches:
                errors.append(f"input_audit.{field} contradicts zero-input policy")

    root = None if repo_root is None else Path(repo_root).expanduser()
    roots = {name: Path(value).expanduser() for name, value in (external_roots or {}).items()}
    artifacts = _verify_artifacts(
        payload,
        repo_root=root,
        external_roots=roots,
        metadata_only=metadata_only,
        errors=errors,
    )
    _verify_contract_links(payload, artifacts, errors)
    _verify_baseline(payload, repo_root=root, errors=errors)
    _verify_cross_links(
        payload,
        artifacts,
        repo_root=root,
        external_roots=roots,
        metadata_only=metadata_only,
        full_cas_root=full_cas_root,
        full_truth_root=full_truth_root,
        full_event_tapes=full_event_tapes,
        errors=errors,
    )
    return errors


# Public aliases used by callers that name the tool after its CLI command.
verify_packet = verify_g1_frame_candidate
verify_candidate = verify_g1_frame_candidate
verify = verify_g1_frame_candidate


def assert_valid(
    packet: Mapping[str, Any],
    *,
    repo_root: Path | None = None,
    external_roots: Mapping[str, Path | str] | None = None,
    metadata_only: bool = True,
    private_cas_root: Path | str | None = None,
    truth_root: Path | str | None = None,
    event_tape_paths: Sequence[Path | str] | Path | str | None = None,
    cas_root: Path | str | None = None,
    event_tapes: Sequence[Path | str] | Path | str | None = None,
    event_tape_path: Path | str | None = None,
    schema_path: Path = SCHEMA_PATH,
) -> None:
    errors = verify_g1_frame_candidate(
        packet,
        repo_root=repo_root,
        external_roots=external_roots,
        metadata_only=metadata_only,
        private_cas_root=private_cas_root,
        truth_root=truth_root,
        event_tape_paths=event_tape_paths,
        cas_root=cas_root,
        event_tapes=event_tapes,
        event_tape_path=event_tape_path,
        schema_path=schema_path,
    )
    if errors:
        raise CandidatePacketVerificationError("; ".join(errors))


def _parse_roots(values: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--root expects NAME=PATH")
        name, path = value.split("=", 1)
        if not name or not path or name in roots:
            raise ValueError("--root expects unique NAME=PATH")
        roots[name] = Path(path)
    return roots


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", "--report", dest="packet", type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--root", action="append", default=[], metavar="NAME=PATH")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--metadata-only",
        action="store_true",
        help="Verify packet metadata/local artifacts without opening restricted evidence roots.",
    )
    mode.add_argument(
        "--full",
        action="store_true",
        help="Recompute the B2 audit from explicit private CAS, truth root and Event Tape paths.",
    )
    parser.add_argument(
        "--private-cas-root",
        "--cas-root",
        dest="private_cas_root",
        type=Path,
        help="Controlled private PixelStore/CAS root required by --full.",
    )
    parser.add_argument(
        "--truth-root",
        type=Path,
        help="Controlled truth-artifact root required by --full.",
    )
    parser.add_argument(
        "--event-tape",
        "--event-tape-path",
        dest="event_tape_paths",
        action="append",
        type=Path,
        default=[],
        help="Explicit Event Tape path; repeat for each tape required by --full.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        packet = read_json(args.packet.resolve(strict=True))
        roots = _parse_roots(args.root)
        errors = verify_g1_frame_candidate(
            packet,
            repo_root=args.repo_root.resolve(),
            external_roots=roots,
            metadata_only=not args.full,
            private_cas_root=args.private_cas_root,
            truth_root=args.truth_root,
            event_tape_paths=args.event_tape_paths,
            schema_path=args.schema.resolve(),
        )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if errors:
        print(f"G1 Frame Candidate verification failed ({len(errors)} error(s)):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("G1 Frame Candidate packet verified (metadata-only/full roots as requested).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
