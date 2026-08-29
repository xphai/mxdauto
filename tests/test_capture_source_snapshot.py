from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = ROOT / "tools" / "verify_capture_source_snapshot.py"
SPEC = importlib.util.spec_from_file_location("verify_capture_source_snapshot", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _payload() -> dict[str, Any]:
    return json.loads(
        (ROOT / "configs" / "g1-frame-source-snapshot.json").read_text(encoding="utf-8")
    )


def test_static_snapshot_matches_schema_and_metadata_verifier() -> None:
    payload = _payload()
    schema = json.loads(
        (ROOT / "schemas" / "capture-source-snapshot.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        payload
    )
    MODULE.verify_snapshot(payload, root=None, metadata_only=True)


def test_full_snapshot_verifies_copied_external_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    required: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(payload["entries"]):
        path = tmp_path / Path(*entry["relative_path"].split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"synthetic snapshot entry {index}\n".encode()
        path.write_bytes(content)
        entry["size_bytes"] = len(content)
        entry["sha256"] = hashlib.sha256(content).hexdigest()
        timestamp_ns = MODULE._timestamp_ns(entry["last_write_utc"])
        path.touch()
        os.utime(path, ns=(timestamp_ns, timestamp_ns))
        required[entry["relative_path"]] = {
            key: entry[key]
            for key in ("role", "migration_use", "last_write_utc", "sha256", "size_bytes")
        }
    payload["snapshot_digest"] = MODULE._canonical_digest(payload)
    monkeypatch.setattr(MODULE, "REQUIRED_ENTRIES", required)
    MODULE.verify_snapshot(payload, root=tmp_path.resolve(), metadata_only=False)


def test_resigned_snapshot_tamper_is_rejected() -> None:
    payload = copy.deepcopy(_payload())
    payload["entries"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="snapshot_digest"):
        MODULE.verify_snapshot(payload, root=None, metadata_only=True)


def test_duplicate_role_is_rejected_even_when_resigned() -> None:
    payload = copy.deepcopy(_payload())
    payload["entries"][1]["role"] = payload["entries"][0]["role"]
    payload["snapshot_digest"] = MODULE._canonical_digest(payload)
    with pytest.raises(ValueError, match="Duplicate snapshot role"):
        MODULE.verify_snapshot(payload, root=None, metadata_only=True)


def test_external_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    payload = _payload()
    for entry in payload["entries"]:
        path = tmp_path / Path(*entry["relative_path"].split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="size mismatch"):
        MODULE.verify_snapshot(payload, root=tmp_path.resolve(), metadata_only=False)


def test_resigned_snapshot_cannot_drop_required_entries() -> None:
    payload = copy.deepcopy(_payload())
    payload["entries"] = payload["entries"][:1]
    payload["snapshot_digest"] = MODULE._canonical_digest(payload)
    with pytest.raises(ValueError, match="entry set is not frozen"):
        MODULE.verify_snapshot(payload, root=None, metadata_only=True)


def test_resigned_last_write_timestamp_is_rejected() -> None:
    payload = copy.deepcopy(_payload())
    payload["entries"][0]["last_write_utc"] = "2026-08-21T08:42:54.010889Z"
    payload["snapshot_digest"] = MODULE._canonical_digest(payload)
    with pytest.raises(ValueError, match="frozen reference"):
        MODULE.verify_snapshot(payload, root=None, metadata_only=True)


def test_resigned_top_level_provenance_claims_are_frozen() -> None:
    payload = copy.deepcopy(_payload())
    payload["upstream_reference"]["repository"] = "https://github.com/attacker/other.git"
    payload["upstream_reference"]["commit"] = "0" * 40
    payload["limitations"] = ["x", "y", "z"]
    payload["snapshot_digest"] = MODULE._canonical_digest(payload)
    with pytest.raises(ValueError, match="top-level field"):
        MODULE.verify_snapshot(payload, root=None, metadata_only=True)


@pytest.mark.parametrize(
    "text",
    [
        '{"schema_version":"1.0.0","schema_version":"1.0.0"}',
        '{"value":NaN}',
        '{"value":Infinity}',
    ],
)
def test_snapshot_json_reader_rejects_duplicate_keys_and_constants(
    tmp_path: Path,
    text: str,
) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        MODULE._read_json(path)


def test_snapshot_rejects_symlink_component_before_resolution(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "input").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "src").symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this host")
    with pytest.raises(ValueError, match="symlink/reparse"):
        MODULE._resolve_entry(root, "src/input/frame.py")
