from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SHA256_BYTES = 1024 * 1024


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(SHA256_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def isoformat_utc(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def git_commit(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"Could not resolve a git commit in {root}") from exc
    commit = completed.stdout.strip()
    if len(commit) != 40 or any(character not in "0123456789abcdefABCDEF" for character in commit):
        raise ValueError(f"Expected a 40-character commit hash from {root}, got {commit!r}")
    return commit


def resolve_root(root_env: str, explicit: Path | None) -> Path:
    root = explicit or (Path(os.environ[root_env]) if os.environ.get(root_env) else None)
    if root is None:
        raise ValueError(f"Set {root_env} or pass its corresponding --*-root option.")
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"Asset root does not exist: {resolved}")
    return resolved


def safe_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    if normalized.startswith("/") or ":" in normalized:
        raise ValueError(f"Absolute asset paths are not portable: {value!r}")
    if any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"Asset path must be normalized and relative: {value!r}")
    return "/".join(path.parts)


def file_metadata(path: Path) -> tuple[int, str, str]:
    stat = path.stat()
    return (
        stat.st_size,
        sha256_file(path),
        isoformat_utc(datetime.fromtimestamp(stat.st_mtime, UTC)),
    )
