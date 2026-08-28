from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    manifest = _read_object(manifest_path)
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
    bound["report_digest"] = _canonical_digest(bound)
    return bound


def write_report(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path
