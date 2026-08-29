"""Build a metadata-safe G1 Frame Candidate packet.

This is packaging-only code.  It hashes already-produced evidence and writes a
new packet; it deliberately never opens a capture backend or any input sink.
Local paths are repository-relative and must be regular, non-symlink files.
Restricted evidence may instead use an ``external://`` locator plus an
attested hash, which is suitable for public metadata-only verification.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

try:
    from .bundle_common import isoformat_utc, read_json, safe_relative_path, sha256_file, write_json
except ImportError:  # pragma: no cover - direct script execution
    from bundle_common import (  # type: ignore[import-not-found,no-redef]
        isoformat_utc,
        read_json,
        safe_relative_path,
        sha256_file,
        write_json,
    )


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "g1-frame-candidate-packet.schema.json"
SCHEMA_VERSION = "1.0.0"
PACKET_TYPE = "g1_frame_candidate"
G1_SCOPE = "G1-FRM"
G0_SOURCE_COMMIT = "7da29f4cfae0bd984b00c394b78e637088a7e452"
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[A-Fa-f0-9]{40}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")

EVIDENCE_ROLES = (
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


class CandidatePacketBuildError(ValueError):
    """Raised when packet inputs are incomplete or unsafe."""


def canonical_packet_digest(payload: Mapping[str, Any]) -> str:
    """Compute packet digest after removing the self-referential field."""

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


def _require_id(value: object, field: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise CandidatePacketBuildError(f"{field} must be a portable identifier")
    return value


def _require_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise CandidatePacketBuildError(f"{field} must be a SHA-256 digest")
    return value.lower()


def _require_commit(value: object, field: str) -> str:
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        raise CandidatePacketBuildError(f"{field} must be a 40-character git commit")
    return value.lower()


def _repo_relative(
    root: Path,
    path_value: Path | str,
    field: str,
    *,
    require_file: bool = True,
) -> tuple[Path, str]:
    """Return a safe local path and its portable repository-relative spelling."""

    raw_root = root.expanduser()
    if raw_root.is_symlink():
        raise CandidatePacketBuildError(f"{field} root must not be a symlink")
    root_resolved = raw_root.resolve()
    raw = Path(path_value).expanduser()
    candidate = raw if raw.is_absolute() else root_resolved / raw
    # ``abspath`` normalises ``.``/``..`` lexically but deliberately does not
    # follow symlinks.  Resolving before this check would hide an in-root link.
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root_resolved).as_posix()
    except ValueError as exc:
        raise CandidatePacketBuildError(f"{field} must be inside repository root") from exc
    try:
        relative = safe_relative_path(relative)
    except ValueError as exc:
        raise CandidatePacketBuildError(f"{field} unsafe path: {exc}") from exc
    current = root_resolved
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise CandidatePacketBuildError(f"{field} must not traverse a symlink: {relative}")
    if require_file and (not candidate.exists() or not candidate.is_file()):
        raise CandidatePacketBuildError(f"{field} is not a regular file: {relative}")
    if (
        not require_file
        and candidate.exists()
        and (candidate.is_symlink() or not candidate.is_file())
    ):
        raise CandidatePacketBuildError(f"{field} is not a regular file: {relative}")
    return candidate, relative


def _canonical_file(root: Path, path_value: Path | str, field: str) -> tuple[Path, str, str, int]:
    path, relative = _repo_relative(root, path_value, field)
    return path, relative, sha256_file(path), path.stat().st_size


def _normalise_locator(value: object, field: str) -> tuple[str, dict[str, Any]]:
    """Normalise a local path or restricted external locator descriptor."""

    if isinstance(value, Mapping):
        locator = value.get("locator", value)
        if isinstance(locator, Mapping):
            kind = locator.get("kind")
            if kind == "local":
                path = locator.get("path")
                if isinstance(path, str):
                    return "local", {"path": path}
            if kind == "external":
                uri = locator.get("uri")
                access = locator.get("access_class", "restricted")
                if isinstance(uri, str) and uri.startswith("external://"):
                    return "external", {"uri": uri, "access_class": access}
        if isinstance(locator, str):
            value = locator
        else:
            path = value.get("path")
            if isinstance(path, str | Path):
                return "local", {"path": str(path)}
            uri = value.get("external_locator", value.get("uri"))
            if isinstance(uri, str) and uri.startswith("external://"):
                return "external", {
                    "uri": uri,
                    "access_class": value.get("access_class", "restricted"),
                }
    if isinstance(value, Path):
        return "local", {"path": str(value)}
    if isinstance(value, str):
        if value.startswith("external://"):
            return "external", {"uri": value, "access_class": "restricted"}
        return "local", {"path": value}
    raise CandidatePacketBuildError(f"{field} must be a path or external locator")


def _artifact_input(
    artifact_id: str,
    role: str,
    value: object,
    *,
    repo_root: Path,
    default_privacy: str = "restricted",
    default_retention: str = "persistent",
) -> dict[str, Any]:
    artifact_id = _require_id(artifact_id, "artifact_id")
    kind, locator = _normalise_locator(value, artifact_id)
    descriptor = value if isinstance(value, Mapping) else {}
    privacy = (
        descriptor.get("privacy_class", default_privacy)
        if isinstance(descriptor, Mapping)
        else default_privacy
    )
    retention = (
        descriptor.get("retention_class", default_retention)
        if isinstance(descriptor, Mapping)
        else default_retention
    )
    if kind == "local":
        path_value = locator["path"]
        path, relative, digest, size = _canonical_file(
            repo_root, cast(str, path_value), artifact_id
        )
        del path
        return {
            "artifact_id": artifact_id,
            "role": role,
            "locator": {
                "kind": "local",
                "path": relative,
                "access_class": descriptor.get("access_class", "restricted")
                if isinstance(descriptor, Mapping)
                else "restricted",
            },
            "sha256": digest,
            "size_bytes": size,
            "privacy_class": privacy,
            "retention_class": retention,
            "restricted": privacy in {"restricted", "private"},
        }
    uri = cast(str, locator["uri"])
    provided_sha = descriptor.get("sha256") if isinstance(descriptor, Mapping) else None
    provided_size = descriptor.get("size_bytes", 0) if isinstance(descriptor, Mapping) else 0
    digest = _require_sha(provided_sha, f"{artifact_id}.sha256")
    if type(provided_size) is not int or provided_size < 0:
        raise CandidatePacketBuildError(f"{artifact_id}.size_bytes must be a non-negative integer")
    access = locator.get("access_class", "restricted")
    if access not in {"restricted", "private"}:
        raise CandidatePacketBuildError(
            f"{artifact_id} external artifacts must be restricted/private"
        )
    return {
        "artifact_id": artifact_id,
        "role": role,
        "locator": {"kind": "external", "uri": uri, "access_class": access},
        "sha256": digest,
        "size_bytes": provided_size,
        "privacy_class": privacy
        if privacy in {"restricted", "private", "hash_only"}
        else "restricted",
        "retention_class": retention,
        "restricted": True,
    }


def _coerce_artifacts(
    artifacts: Mapping[str, object] | Sequence[Mapping[str, Any]] | None,
    *,
    repo_root: Path,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if artifacts is None:
        return result
    entries: list[tuple[str, object, str]] = []
    if isinstance(artifacts, Mapping):
        for artifact_id, value in artifacts.items():
            role = value.get("role", artifact_id) if isinstance(value, Mapping) else artifact_id
            entries.append((str(artifact_id), value, str(role)))
    elif isinstance(artifacts, Sequence) and not isinstance(artifacts, str | bytes | bytearray):
        for index, value in enumerate(artifacts):
            if not isinstance(value, Mapping):
                raise CandidatePacketBuildError(f"artifacts[{index}] must be an object")
            artifact_id = value.get("artifact_id")
            if not isinstance(artifact_id, str):
                raise CandidatePacketBuildError(f"artifacts[{index}] is missing artifact_id")
            entries.append((artifact_id, value, str(value.get("role", artifact_id))))
    else:
        raise CandidatePacketBuildError("artifacts must be a mapping or array")
    for artifact_id, value, role in entries:
        if artifact_id in result:
            raise CandidatePacketBuildError(f"duplicate artifact_id: {artifact_id}")
        result[artifact_id] = _artifact_input(artifact_id, role, value, repo_root=repo_root)
    return result


def _artifact_ref(artifact: Mapping[str, Any]) -> dict[str, str]:
    return {
        "artifact_id": cast(str, artifact["artifact_id"]),
        "sha256": cast(str, artifact["sha256"]),
        "role": cast(str, artifact["role"]),
    }


def _load_json_object(path: Path, field: str) -> Mapping[str, Any]:
    try:
        value = read_json(path)
    except (OSError, ValueError) as exc:
        raise CandidatePacketBuildError(f"{field} is not valid JSON: {path}") from exc
    return value


def build_candidate_packet(
    *,
    repo_root: Path | str,
    output: Path | str | None = None,
    packet_id: str = "g1-frame-candidate-001",
    lifecycle: str = "candidate",
    source_commit: str | None = None,
    b1_source_commit: str | None = None,
    ci_run_id: str = "0",
    ci_run_attempt: str = "1",
    ci_workflow: str = "b1-main",
    ci_status: str = "success",
    wheel_path: Path | str | None = None,
    wheel_sha256: str | None = None,
    wheel_name: str | None = None,
    wheel_locator: str | None = None,
    dependency_lock_path: Path | str | None = None,
    dependency_lock_sha256: str | None = None,
    g0_source_commit: str = G0_SOURCE_COMMIT,
    g0_packet_path: Path | str | None = None,
    g0_bundle_path: Path | str | None = None,
    g0_packet_sha256: str | None = None,
    g0_bundle_sha256: str | None = None,
    g0_packet_ref: str | None = None,
    g0_bundle_ref: str | None = None,
    g0_bundle_id: str = "candidate-core-v2-20260829-shadow",
    capture_config_path: Path | str | None = None,
    calibration_path: Path | str | None = None,
    pixel_contract_path: Path | str | None = None,
    artifacts: Mapping[str, object] | Sequence[Mapping[str, Any]] | None = None,
    evidence: Mapping[str, object] | None = None,
    limitations: Sequence[str] | None = None,
    signoffs: Sequence[Mapping[str, Any]] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build and optionally write one candidate packet.

    The function accepts both the long B1-specific keyword names and the
    shorter aliases used by command-line callers.  Every local hash and size
    is computed from bytes at build time; supplied hashes are ignored for local
    files and checked only for restricted external locators.
    """

    root_input = Path(repo_root).expanduser()
    if root_input.is_symlink():
        raise CandidatePacketBuildError(f"repo_root must not be a symlink: {root_input}")
    root = root_input.resolve()
    if not root.is_dir():
        raise CandidatePacketBuildError(f"repo_root must be a non-symlink directory: {root}")
    packet_id = _require_id(packet_id, "packet_id")
    if lifecycle not in {"candidate", "shadow"}:
        raise CandidatePacketBuildError("lifecycle must be candidate or shadow")
    if (
        source_commit is not None
        and b1_source_commit is not None
        and _require_commit(source_commit, "source_commit")
        != _require_commit(b1_source_commit, "b1_source_commit")
    ):
        raise CandidatePacketBuildError("source_commit and b1_source_commit disagree")
    source = _require_commit(source_commit or b1_source_commit, "b1 source_commit")
    if not re.fullmatch(r"[0-9]+", str(ci_run_id)) or not re.fullmatch(
        r"[0-9]+", str(ci_run_attempt)
    ):
        raise CandidatePacketBuildError("CI run id/attempt must be decimal strings")
    if ci_status not in {"success", "failure", "cancelled", "unknown"}:
        raise CandidatePacketBuildError("unsupported CI status")

    packet_path: Path | None = None
    if output is not None:
        packet_path, _ = _repo_relative(root, Path(output), "output", require_file=False)

    packet_artifacts = _coerce_artifacts(artifacts, repo_root=root)

    # Add the immutable B1 inputs as normal artifact rows.  This means the
    # verifier can apply one hash/size/path policy to both evidence and build
    # products.
    if wheel_path is not None:
        _, wheel_relative, wheel_digest, wheel_size = _canonical_file(root, wheel_path, "wheel")
        wheel_name_value = wheel_name or Path(wheel_relative).name
        wheel_artifact = {
            "artifact_id": "b1-wheel",
            "role": "b1-wheel",
            "locator": {"kind": "local", "path": wheel_relative, "access_class": "restricted"},
            "sha256": wheel_digest,
            "size_bytes": wheel_size,
            "privacy_class": "restricted",
            "retention_class": "persistent",
            "restricted": True,
        }
        wheel_sha_value = wheel_digest
        wheel_locator_value = wheel_relative
        wheel_binding_locator = wheel_relative
        if "b1-wheel" in packet_artifacts and packet_artifacts["b1-wheel"] != wheel_artifact:
            raise CandidatePacketBuildError("b1-wheel is supplied twice with different metadata")
        packet_artifacts["b1-wheel"] = wheel_artifact
    else:
        wheel_sha_value = _require_sha(wheel_sha256, "wheel_sha256")
        wheel_name_value = wheel_name or "B1-wheel.whl"
        wheel_locator_value = wheel_locator or "external/b1-wheel.whl"
        if wheel_locator_value.startswith("external://"):
            remainder = wheel_locator_value[len("external://") :]
            root_name, separator, relative = remainder.partition("/")
            if not separator or not root_name:
                raise CandidatePacketBuildError("wheel_locator is malformed")
            try:
                safe_relative_path(relative)
            except ValueError as exc:
                raise CandidatePacketBuildError("wheel_locator is unsafe") from exc
            wheel_artifact_locator = {
                "kind": "external",
                "uri": wheel_locator_value,
                "access_class": "restricted",
            }
        else:
            safe_relative_path(wheel_locator_value)
            wheel_artifact_locator = {
                "kind": "external",
                "uri": "external://B1/" + Path(wheel_locator_value).name,
                "access_class": "restricted",
            }
        wheel_binding_locator = cast(str, wheel_artifact_locator.get("uri", wheel_locator_value))
        if wheel_artifact_locator.get("kind") == "external" and not isinstance(
            wheel_artifact_locator.get("uri"), str
        ):
            raise CandidatePacketBuildError("external wheel locator is malformed")
        packet_artifacts["b1-wheel"] = {
            "artifact_id": "b1-wheel",
            "role": "b1-wheel",
            "locator": wheel_artifact_locator,
            "sha256": wheel_sha_value,
            "size_bytes": 0,
            "privacy_class": "restricted",
            "retention_class": "persistent",
            "restricted": True,
        }
    if (
        wheel_sha256 is not None
        and wheel_path is not None
        and _require_sha(wheel_sha256, "wheel_sha256") != wheel_sha_value
    ):
        raise CandidatePacketBuildError("supplied wheel_sha256 does not match local wheel")

    if dependency_lock_path is not None:
        _, lock_relative, lock_digest, lock_size = _canonical_file(
            root, dependency_lock_path, "dependency lock"
        )
        lock_artifact = {
            "artifact_id": "dependency-lock",
            "role": "dependency-lock",
            "locator": {"kind": "local", "path": lock_relative, "access_class": "restricted"},
            "sha256": lock_digest,
            "size_bytes": lock_size,
            "privacy_class": "restricted",
            "retention_class": "persistent",
            "restricted": True,
        }
        lock_sha_value = lock_digest
        packet_artifacts["dependency-lock"] = lock_artifact
    else:
        lock_sha_value = _require_sha(dependency_lock_sha256, "dependency_lock_sha256")
        packet_artifacts["dependency-lock"] = {
            "artifact_id": "dependency-lock",
            "role": "dependency-lock",
            "locator": {
                "kind": "external",
                "uri": "external://B1/dependency-lock",
                "access_class": "restricted",
            },
            "sha256": lock_sha_value,
            "size_bytes": 0,
            "privacy_class": "restricted",
            "retention_class": "persistent",
            "restricted": True,
        }
    if (
        dependency_lock_sha256 is not None
        and dependency_lock_path is not None
        and _require_sha(dependency_lock_sha256, "dependency_lock_sha256") != lock_sha_value
    ):
        raise CandidatePacketBuildError("supplied dependency_lock_sha256 does not match local lock")

    def add_contract_artifact(
        artifact_id: str,
        role: str,
        path_value: Path | str | None,
    ) -> tuple[str, str]:
        if path_value is None:
            raise CandidatePacketBuildError(f"{role} path is required")
        _, relative, digest, size = _canonical_file(root, path_value, role)
        packet_artifacts[artifact_id] = {
            "artifact_id": artifact_id,
            "role": role,
            "locator": {"kind": "local", "path": relative, "access_class": "restricted"},
            "sha256": digest,
            "size_bytes": size,
            "privacy_class": "restricted",
            "retention_class": "persistent",
            "restricted": True,
        }
        return relative, digest

    config_relative, config_digest = add_contract_artifact(
        "capture-config", "capture-config", capture_config_path
    )
    calibration_relative, calibration_digest = add_contract_artifact(
        "calibration", "calibration", calibration_path
    )
    pixel_relative, pixel_digest = add_contract_artifact(
        "pixel-contract-schema", "pixel-contract-schema", pixel_contract_path
    )

    if evidence is not None:
        for role, value in evidence.items():
            role_name = str(role)
            artifact_id = (
                value.get("artifact_id", role_name) if isinstance(value, Mapping) else role_name
            )
            if isinstance(value, Mapping) and "path" in value and "locator" not in value:
                descriptor: object = value
            else:
                descriptor = value
            if artifact_id in packet_artifacts:
                continue
            packet_artifacts[artifact_id] = _artifact_input(
                artifact_id, role_name, descriptor, repo_root=root
            )

    missing_roles = [
        role
        for role in EVIDENCE_ROLES
        if not any(row.get("role") == role for row in packet_artifacts.values())
    ]
    if missing_roles:
        raise CandidatePacketBuildError(
            f"missing required evidence artifacts: {', '.join(missing_roles)}"
        )
    refs_by_role: dict[str, dict[str, str]] = {}
    for role in EVIDENCE_ROLES:
        candidates = [row for row in packet_artifacts.values() if row.get("role") == role]
        if len(candidates) != 1:
            raise CandidatePacketBuildError(
                f"expected exactly one artifact for evidence role {role}"
            )
        refs_by_role[role] = _artifact_ref(candidates[0])

    # G0 references are read-only.  A caller may provide either local paths
    # (the builder computes their hashes) or immutable external hash metadata.
    def baseline_ref(
        path_value: Path | str | None, digest_value: str | None, ref_value: str | None, field: str
    ) -> tuple[str, str]:
        if path_value is not None:
            _, relative, digest, _ = _canonical_file(root, path_value, field)
            if digest_value is not None and _require_sha(digest_value, field + ".sha256") != digest:
                raise CandidatePacketBuildError(f"{field} hash does not match local file")
            return relative, digest
        if ref_value is None or digest_value is None:
            raise CandidatePacketBuildError(f"{field} requires a path/ref and SHA-256")
        try:
            relative = safe_relative_path(ref_value)
        except ValueError as exc:
            raise CandidatePacketBuildError(f"{field} unsafe path: {exc}") from exc
        return relative, _require_sha(digest_value, field + ".sha256")

    g0_packet_relative, g0_packet_digest = baseline_ref(
        g0_packet_path, g0_packet_sha256, g0_packet_ref, "g0 packet"
    )
    g0_bundle_relative, g0_bundle_digest = baseline_ref(
        g0_bundle_path, g0_bundle_sha256, g0_bundle_ref, "g0 bundle"
    )
    g0_source = _require_commit(g0_source_commit, "g0 source_commit")
    g0_id = _require_id(g0_bundle_id, "g0 bundle_id")

    pixel_contract_payload = _load_json_object(cast(Path, root / pixel_relative), "pixel contract")
    # A schema file is itself evidence.  The frozen raw spec is kept in the
    # packet so a future schema change cannot silently alter the packet's
    # pixel interpretation.
    raw_spec: dict[str, Any] = {
        "width": 1920,
        "height": 1080,
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": 5760,
        "length": 6_220_800,
    }
    candidate_spec = pixel_contract_payload.get("raw_spec")
    if isinstance(candidate_spec, Mapping):
        raw_spec = dict(candidate_spec)
    if raw_spec != {
        "width": 1920,
        "height": 1080,
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": 5760,
        "length": 6_220_800,
    }:
        raise CandidatePacketBuildError(
            "pixel contract raw_spec must be the frozen 1920x1080 BGR8 Pixel V1 spec"
        )

    packet: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "packet_type": PACKET_TYPE,
        "packet_id": packet_id,
        "lifecycle": lifecycle,
        "scope": G1_SCOPE,
        "overall_g1_state": "In Progress",
        "generated_at": generated_at or isoformat_utc(),
        "b1_binding": {
            "source_commit": source,
            "ci_run": {
                "run_id": str(ci_run_id),
                "run_attempt": str(ci_run_attempt),
                "workflow": ci_workflow,
                "status": ci_status,
            },
            "wheel": {
                "name": wheel_name_value,
                "sha256": wheel_sha_value,
                "size_bytes": packet_artifacts.get("b1-wheel", {}).get("size_bytes", 0),
                "locator": wheel_binding_locator,
            },
            "dependency_lock_sha256": lock_sha_value,
        },
        "g0_baseline": {
            "immutable": True,
            "source_commit": g0_source,
            "packet_sha256": g0_packet_digest,
            "bundle_id": g0_id,
            "bundle_sha256": g0_bundle_digest,
            "packet_ref": g0_packet_relative,
            "bundle_ref": g0_bundle_relative,
        },
        "contracts": {
            "capture_config": {
                "artifact_id": "capture-config",
                "sha256": config_digest,
                "path": config_relative,
            },
            "calibration": {
                "artifact_id": "calibration",
                "sha256": calibration_digest,
                "path": calibration_relative,
            },
            "pixel_contract": {
                "schema_version": str(pixel_contract_payload.get("schema_version", "1.0.0")),
                "schema_path": pixel_relative,
                "schema_sha256": pixel_digest,
                "digest_domain": "MAPLE_PIXEL_V1",
                "raw_spec": raw_spec,
            },
        },
        "evidence": refs_by_role,
        "artifacts": [packet_artifacts[key] for key in sorted(packet_artifacts)],
        "input_audit": {
            "input_owner": "legacy",
            "real_input_enabled": False,
            "real_input_call_count": 0,
            "core_v2_real_input_call_count": 0,
            "receiver_connect_count": 0,
            "window_write_count": 0,
            "double_write_event_count": 0,
        },
        "limitations": list(
            limitations
            or (
                "G1 remains In Progress; this packet is candidate/shadow evidence only.",
                "upstream driver/vendor queue depth is unknown and is not inferred "
                "from Core raw capacity.",
                "packet packaging does not claim sensor latency or live input ownership.",
            )
        ),
        "signoffs": [dict(item) for item in (signoffs or [])],
    }
    packet["packet_digest"] = canonical_packet_digest(packet)
    if packet_path is not None:
        output_resolved = packet_path.resolve(strict=False)
        protected_paths = [
            root / g0_packet_relative,
            root / g0_bundle_relative,
            root / config_relative,
            root / calibration_relative,
            root / pixel_relative,
        ]
        for artifact in packet_artifacts.values():
            locator = artifact.get("locator")
            if isinstance(locator, Mapping) and locator.get("kind") == "local":
                path_value = locator.get("path")
                if isinstance(path_value, str):
                    protected_paths.append(root / path_value)
        if any(output_resolved == path.resolve(strict=False) for path in protected_paths):
            raise CandidatePacketBuildError(
                "output must not overwrite a G0, contract, wheel, lock or evidence artifact"
            )
        write_json(packet_path, packet)
        return read_json(packet_path)
    return packet


# Short aliases retained for callers that use the tool name as a function.
build_packet = build_candidate_packet
build_candidate = build_candidate_packet
build_g1_frame_candidate = build_candidate_packet


def _parse_key_value(items: Sequence[str], field: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise CandidatePacketBuildError(f"{field} expects ID=VALUE")
        key, value = item.split("=", 1)
        if not key or not value:
            raise CandidatePacketBuildError(f"{field} expects non-empty ID=VALUE")
        if key in result:
            raise CandidatePacketBuildError(f"duplicate {field} key: {key}")
        result[key] = value
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--packet-id", default="g1-frame-candidate-001")
    parser.add_argument("--lifecycle", choices=("candidate", "shadow"), default="candidate")
    parser.add_argument(
        "--source-commit", "--b1-source-commit", dest="source_commit", required=True
    )
    parser.add_argument("--ci-run-id", default="0")
    parser.add_argument("--ci-run-attempt", default="1")
    parser.add_argument("--ci-workflow", default="b1-main")
    parser.add_argument(
        "--ci-status", choices=("success", "failure", "cancelled", "unknown"), default="success"
    )
    parser.add_argument("--wheel", "--wheel-path", dest="wheel_path", type=Path)
    parser.add_argument("--wheel-sha256")
    parser.add_argument("--wheel-name")
    parser.add_argument("--wheel-locator")
    parser.add_argument("--lock", "--dependency-lock", dest="lock_path", type=Path)
    parser.add_argument("--dependency-lock-sha256")
    parser.add_argument("--g0-source-commit", default=G0_SOURCE_COMMIT)
    parser.add_argument("--g0-packet", "--g0-packet-path", dest="g0_packet_path", type=Path)
    parser.add_argument("--g0-bundle", "--g0-bundle-path", dest="g0_bundle_path", type=Path)
    parser.add_argument("--g0-packet-sha256")
    parser.add_argument("--g0-bundle-sha256")
    parser.add_argument("--g0-packet-ref")
    parser.add_argument("--g0-bundle-ref")
    parser.add_argument("--g0-bundle-id", default="candidate-core-v2-20260829-shadow")
    parser.add_argument("--capture-config", required=True, type=Path)
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--pixel-contract", required=True, type=Path)
    parser.add_argument("--artifact", action="append", default=[], metavar="ID=PATH")
    parser.add_argument("--evidence-artifact", action="append", default=[], metavar="ROLE=PATH|ID")
    parser.add_argument("--limitation", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        artifact_values = _parse_key_value(args.artifact, "--artifact")
        evidence_values = _parse_key_value(args.evidence_artifact, "--evidence-artifact")
        artifact_inputs: dict[str, object] = {
            artifact_id: {"path": path, "role": artifact_id}
            for artifact_id, path in artifact_values.items()
        }
        evidence: dict[str, object] = {}
        for role, value in evidence_values.items():
            if value in artifact_values:
                descriptor = {
                    "artifact_id": value,
                    "path": artifact_values[value],
                    "role": role,
                }
                artifact_inputs[value] = descriptor
                evidence[role] = descriptor
            else:
                evidence[role] = value
        packet = build_candidate_packet(
            repo_root=args.repo_root,
            output=args.output,
            packet_id=args.packet_id,
            lifecycle=args.lifecycle,
            source_commit=args.source_commit,
            ci_run_id=args.ci_run_id,
            ci_run_attempt=args.ci_run_attempt,
            ci_workflow=args.ci_workflow,
            ci_status=args.ci_status,
            wheel_path=args.wheel_path,
            wheel_sha256=args.wheel_sha256,
            wheel_name=args.wheel_name,
            wheel_locator=args.wheel_locator,
            dependency_lock_path=args.lock_path,
            dependency_lock_sha256=args.dependency_lock_sha256,
            g0_source_commit=args.g0_source_commit,
            g0_packet_path=args.g0_packet_path,
            g0_bundle_path=args.g0_bundle_path,
            g0_packet_sha256=args.g0_packet_sha256,
            g0_bundle_sha256=args.g0_bundle_sha256,
            g0_packet_ref=args.g0_packet_ref,
            g0_bundle_ref=args.g0_bundle_ref,
            g0_bundle_id=args.g0_bundle_id,
            capture_config_path=args.capture_config,
            calibration_path=args.calibration,
            pixel_contract_path=args.pixel_contract,
            artifacts=artifact_inputs,
            evidence=evidence,
            limitations=args.limitation or None,
        )
    except (CandidatePacketBuildError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"G1 Frame Candidate packet written: {args.output} ({packet['packet_digest']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
