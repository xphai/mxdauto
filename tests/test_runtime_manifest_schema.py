from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "runtime-manifest.schema.json"
SAMPLE_PATH = PROJECT_ROOT / "schemas" / "runtime-manifest.example.json"
SCRIPT_PATH = PROJECT_ROOT / "tools" / "validate_runtime_manifest.py"


def _run_cli(
    manifest_path: Path,
    *,
    use_named_manifest: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--schema",
        str(SCHEMA_PATH),
    ]
    if use_named_manifest:
        command.extend(["--manifest", str(manifest_path)])
    else:
        command.append(str(manifest_path))
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )


def test_runtime_manifest_example_is_valid_via_script() -> None:
    result = _run_cli(SAMPLE_PATH, use_named_manifest=True)
    assert result.returncode == 0
    assert "OK: runtime-manifest.example.json validated against schema." in result.stdout


def test_runtime_manifest_cli_accepts_positional_manifest() -> None:
    result = _run_cli(SAMPLE_PATH)
    assert result.returncode == 0
    assert "Validated 1 manifest(s)." in result.stdout


def test_runtime_manifest_schema_rejects_duplicate_classes_and_bad_receiver_hash(
    tmp_path: Path,
) -> None:
    bad_manifest = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    bad_manifest["classes"] = ["player", "player", "monster"]
    bad_manifest["receiver_hash"] = "this-is-not-a-hash"
    bad_path = tmp_path / "invalid-runtime-manifest.json"
    bad_path.write_text(json.dumps(bad_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    result = _run_cli(bad_path)
    assert result.returncode == 1
    assert "classes" in result.stderr
    assert "receiver_hash" in result.stderr


def test_runtime_manifest_schema_rejects_missing_threshold_key() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    payload["thresholds"].pop("detection_confidence")

    errors = list(validator.iter_errors(payload))
    assert errors, "Expected schema validation to fail when detection_confidence is missing."
    assert any(error.message == "'detection_confidence' is a required property" for error in errors)


def test_runtime_manifest_cli_rejects_invalid_created_at(tmp_path: Path) -> None:
    bad_manifest = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    bad_manifest["created_at"] = "not-an-iso8601-timestamp"
    bad_path = tmp_path / "invalid-created-at.json"
    bad_path.write_text(json.dumps(bad_manifest, indent=2), encoding="utf-8")

    result = _run_cli(bad_path, use_named_manifest=True)

    assert result.returncode == 1
    assert "created_at" in result.stderr
