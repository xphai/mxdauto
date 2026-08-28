from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
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


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant is prohibited: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key is prohibited: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


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
    if normalized.startswith("/") or ":" in normalized:
        raise ValueError(f"Absolute asset paths are not portable: {value!r}")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Asset path must be normalized and relative: {value!r}")
    reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    reserved.update(f"COM{index}" for index in range(1, 10))
    reserved.update(f"LPT{index}" for index in range(1, 10))
    for part in parts:
        if part.rstrip(" .") != part or any(ord(character) < 32 for character in part):
            raise ValueError(f"Asset path has non-portable Windows syntax: {value!r}")
        if part.split(".", 1)[0].upper() in reserved:
            raise ValueError(f"Asset path uses a reserved Windows name: {value!r}")
    return "/".join(parts)


def file_metadata(path: Path) -> tuple[int, str, str]:
    stat = path.stat()
    return (
        stat.st_size,
        sha256_file(path),
        isoformat_utc(datetime.fromtimestamp(stat.st_mtime, UTC)),
    )
