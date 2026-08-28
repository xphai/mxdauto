from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from .bundle_common import read_json, sha256_file, write_json
except ImportError:  # pragma: no cover - exercised when invoked as a script
    from bundle_common import (  # type: ignore[import-not-found,no-redef]
        read_json,
        sha256_file,
        write_json,
    )


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_report_digest(payload: dict[str, Any]) -> str:
    """Return the canonical digest for a report, excluding its digest field.

    This is intentionally shared by producers and verifiers so a copied or
    edited report cannot retain a stale self-declared digest.
    """

    canonical_payload = dict(payload)
    canonical_payload.pop("report_digest", None)
    return _canonical_digest(canonical_payload)


def bind_report_to_manifest(
    report: dict[str, Any],
    *,
    manifest_path: Path,
    repo_root: Path,
    report_kind: str,
) -> dict[str, Any]:
    """Bind a deterministic fixture result to one immutable runtime manifest.

    The fixture keeps its own synthetic bundle identity.  Gate evidence is bound
    separately to the real Candidate manifest so the fixture and release
    provenance are never conflated.
    """

    if report_kind not in {"replay", "shadow"}:
        raise ValueError(f"Unsupported report kind: {report_kind}")
    manifest_path = manifest_path.resolve()
    repo_root = repo_root.resolve()
    try:
        relative_manifest = manifest_path.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise ValueError("Runtime manifest must be inside the repository.") from exc
    manifest = read_json(manifest_path)
    required = (
        "release_id",
        "source_commit",
        "runtime_manifest_version",
        "execution_mode",
    )
    missing = [name for name in required if not manifest.get(name)]
    if missing:
        raise ValueError(f"Runtime manifest is missing fields: {', '.join(missing)}")
    if manifest.get("real_input_enabled") is not False:
        raise ValueError("Replay/Shadow binding requires real_input_enabled=false.")

    fixture_bundle = {
        "bundle_id": report.get("bundle_id"),
        "bundle_digest": report.get("bundle_digest"),
    }
    bound = dict(report)
    bound.pop("report_digest", None)
    bound["fixture_bundle"] = fixture_bundle
    bound["bundle_id"] = manifest["release_id"]
    bound["release_id"] = manifest["release_id"]
    bound["source_commit"] = manifest["source_commit"]
    bound["runtime_manifest_path"] = relative_manifest
    bound["runtime_manifest_sha256"] = sha256_file(manifest_path)
    bound["execution_mode"] = manifest["execution_mode"]
    bound["report_id"] = (
        f"{report_kind}-{bound.get('fixture_id', 'fixture')}-{manifest['release_id']}"
    )
    bound["candidate_binding"] = {
        "bundle_id": manifest["release_id"],
        "release_id": manifest["release_id"],
        "source_commit": manifest["source_commit"],
        "runtime_manifest_path": relative_manifest,
        "runtime_manifest_sha256": bound["runtime_manifest_sha256"],
    }
    bound["report_digest"] = canonical_report_digest(bound)
    return bound


def write_report(path: Path, payload: dict[str, Any]) -> Path:
    write_json(path, payload)
    return path
