from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

MTIME_TOLERANCE_NS = 1_000_000  # one millisecond; explicit cross-filesystem precision budget
REQUIRED_ENTRIES: dict[str, dict[str, Any]] = {
    "src/input/capture_card_source.py": {
        "role": "legacy-capture-adapter-reference",
        "migration_use": "clean_room_semantic_reference_only",
        "last_write_utc": "2026-08-21T08:42:53.010889Z",
        "sha256": "a2f312da774ca61e2fae0f043c933f2be90ff08a239fc7d56bb151f36b39050c",
        "size_bytes": 11243,
    },
    "src/input/frame_source.py": {
        "role": "legacy-frame-contract-reference",
        "migration_use": "clean_room_semantic_reference_only",
        "last_write_utc": "2026-08-21T08:17:45.703776Z",
        "sha256": "3cebd1e60d450143fabe7efc9de3c5ac698919e6d33a9024fa43b565b2c7e2d8",
        "size_bytes": 1659,
    },
    "src/input/frame_normalizer.py": {
        "role": "legacy-geometry-reference",
        "migration_use": "clean_room_semantic_reference_only",
        "last_write_utc": "2026-08-23T08:24:23.039699Z",
        "sha256": "8eb0c84caae10dbf3564c52ef770a75e81548801f15fe485d2462c33a26a4be1",
        "size_bytes": 6949,
    },
    "profiles/maple_legacy_cn/profile.yaml": {
        "role": "legacy-capture-profile-reference",
        "migration_use": "configuration_reference_only",
        "last_write_utc": "2026-08-26T13:05:00.117224Z",
        "sha256": "41b28c8d6f09efdbed8c1de80beafdeaac4cc975a3a30226de93f698b511f161",
        "size_bytes": 2405,
    },
}
REQUIRED_TOP_LEVEL: dict[str, Any] = {
    "schema_version": "1.0.0",
    "snapshot_id": "legacy-capture-local-20260829",
    "source_kind": "unversioned_legacy_local_snapshot",
    "source_root_env": "MAPLE_LEGACY_ROOT",
    "generated_at": "2026-08-29T02:00:00Z",
    "upstream_reference": {
        "repository": "https://github.com/kenyu910645/MapleStoryAutoLevelUp.git",
        "commit": "3e19173f8da5aab8405307bb9c6e3741dd3abd6b",
        "relationship": "reference_only_not_provenance",
    },
    "input_audit": {"input_owner": "legacy", "real_input_call_count": 0},
    "review": {
        "reviewer_role": "sol-u-strategic-audit",
        "method": "read_only_structure_and_hash_audit",
        "decision": "accepted_as_migration_reference_only",
    },
    "limitations": [
        "The snapshot root is a local unpacked tree without Git metadata.",
        "Entries are migration references and are not imported as Core runtime truth.",
        "The upstream commit is reference-only and does not attest these local bytes.",
    ],
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=root / "configs" / "g1-frame-source-snapshot.json",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=root / "schemas" / "capture-source-snapshot.schema.json",
    )
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--metadata-only", action="store_true")
    return parser.parse_args(argv)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _canonical_digest(payload: dict[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("snapshot_digest", None)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError as exc:
        raise ValueError(f"Snapshot path cannot be inspected: {path}") from exc
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _resolve_entry(root: Path, relative_path: str) -> Path:
    parts = relative_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe relative_path: {relative_path}")
    lexical_root = root.expanduser().absolute()
    if _is_link_or_reparse(lexical_root):
        raise ValueError("Legacy root must not be a symlink or reparse point.")
    current = lexical_root
    for part in parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _is_link_or_reparse(current):
            raise ValueError(f"Snapshot path contains a symlink/reparse point: {relative_path}")
    try:
        resolved_root = lexical_root.resolve(strict=True)
        candidate = current.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Snapshot entry cannot be resolved: {relative_path}") from exc
    if not candidate.is_relative_to(resolved_root):
        raise ValueError(f"Snapshot entry escaped root: {relative_path}")
    return candidate


def _timestamp_ns(value: str) -> int:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("last_write_utc must include UTC timezone.")
    return int(parsed.timestamp() * 1_000_000_000)


def verify_snapshot(
    payload: dict[str, Any],
    *,
    root: Path | None,
    metadata_only: bool,
) -> None:
    if payload.get("snapshot_digest") != _canonical_digest(payload):
        raise ValueError("snapshot_digest does not match canonical snapshot content.")
    for key, expected_value in REQUIRED_TOP_LEVEL.items():
        if payload.get(key) != expected_value:
            raise ValueError(f"Snapshot top-level field does not match frozen reference: {key}")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("entries must be a list.")
    observed_entries: dict[str, dict[str, Any]] = {}
    paths: set[str] = set()
    roles: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Every snapshot entry must be an object.")
        relative_path = entry.get("relative_path")
        role = entry.get("role")
        if not isinstance(relative_path, str) or not isinstance(role, str):
            raise ValueError("Snapshot entry path and role must be strings.")
        if relative_path in paths:
            raise ValueError(f"Duplicate snapshot path: {relative_path}")
        if role in roles:
            raise ValueError(f"Duplicate snapshot role: {role}")
        paths.add(relative_path)
        roles.add(role)
        observed_entries[relative_path] = entry
        if metadata_only:
            continue
        if root is None:
            raise ValueError("Full verification requires a legacy root.")
        path = _resolve_entry(root, relative_path)
        if not path.is_file():
            raise ValueError(f"Snapshot entry is missing: {relative_path}")
        if path.is_symlink():
            raise ValueError(f"Snapshot entry must not be a symlink: {relative_path}")
        path_stat = path.stat()
        if path_stat.st_size != entry.get("size_bytes"):
            raise ValueError(f"Snapshot entry size mismatch: {relative_path}")
        if _sha256_file(path) != entry.get("sha256"):
            raise ValueError(f"Snapshot entry hash mismatch: {relative_path}")
        expected_mtime_ns = _timestamp_ns(str(entry.get("last_write_utc")))
        if abs(path_stat.st_mtime_ns - expected_mtime_ns) > MTIME_TOLERANCE_NS:
            raise ValueError(f"Snapshot entry last-write time mismatch: {relative_path}")

    if set(observed_entries) != set(REQUIRED_ENTRIES):
        missing = sorted(set(REQUIRED_ENTRIES) - set(observed_entries))
        extra = sorted(set(observed_entries) - set(REQUIRED_ENTRIES))
        raise ValueError(f"Snapshot entry set is not frozen (missing={missing}, extra={extra}).")
    for relative_path, expected in REQUIRED_ENTRIES.items():
        actual = observed_entries[relative_path]
        frozen_actual = {key: actual.get(key) for key in expected}
        if frozen_actual != expected:
            raise ValueError(f"Snapshot entry does not match frozen reference: {relative_path}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = _read_json(args.snapshot.resolve())
    schema = _read_json(args.schema.resolve())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    legacy_value = args.legacy_root or os.environ.get("MAPLE_LEGACY_ROOT")
    legacy_root = None if legacy_value is None else Path(legacy_value).expanduser().absolute()
    if not args.metadata_only and (legacy_root is None or not legacy_root.is_dir()):
        raise ValueError("Full verification requires an existing MAPLE_LEGACY_ROOT.")
    verify_snapshot(payload, root=legacy_root, metadata_only=args.metadata_only)
    mode = "metadata-only" if args.metadata_only else "full external"
    print(f"Capture source snapshot verified ({mode}): {args.snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
