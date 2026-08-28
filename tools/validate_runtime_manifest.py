from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError


def _default_schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "schemas" / "runtime-manifest.schema.json"


def _parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(
        description="Validate one or more runtime manifest JSON files against the JSON schema."
    )
    parser.add_argument(
        "--schema",
        default=_default_schema_path(),
        type=Path,
        help=(
            "Path to runtime manifest JSON schema (default: schemas/runtime-manifest.schema.json)."
        ),
    )
    parser.add_argument(
        "manifests",
        nargs="*",
        type=Path,
        help="Path(s) to runtime manifest JSON files (positional form).",
    )
    parser.add_argument(
        "--manifest",
        "--manifests",
        dest="manifest_options",
        action="append",
        default=[],
        type=Path,
        help="Path to a runtime manifest JSON file (repeat for multiple files).",
    )
    args = parser.parse_args(argv)
    manifest_paths = [*args.manifests, *args.manifest_options]
    if not manifest_paths:
        parser.error("at least one manifest is required (provide a path or --manifest PATH)")
    args.manifests = manifest_paths
    return args


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must be a JSON object.")
    return payload


def _load_schema(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    try:
        Draft202012Validator.check_schema(payload)
    except SchemaError as exc:
        raise RuntimeError(f"Invalid JSON schema {path}: {exc.message}") from exc
    return payload


def _format_validation_error(error: ValidationError) -> str:
    location = "/".join(str(p) for p in error.absolute_path)
    if not location:
        location = "<root>"
    return f"{location}: {error.message}"


def validate_manifest(
    manifest: dict[str, Any],
    schema: dict[str, Any],
    manifest_path: Path,
) -> tuple[bool, list[str]]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(manifest), key=lambda err: tuple(map(str, err.path)))
    if errors:
        messages = [_format_validation_error(error) for error in errors]
        return False, messages
    print(f"OK: {manifest_path.name} validated against schema.")
    return True, []


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        schema = _load_schema(args.schema)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    failed = 0
    for manifest_path in args.manifests:
        try:
            manifest = _load_json(manifest_path)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            failed += 1
            continue

        valid, errors = validate_manifest(manifest, schema, manifest_path)
        if not valid:
            failed += 1
            print(f"Invalid: {manifest_path}", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)

    if failed:
        print(f"Validation completed with {failed} invalid manifest(s).", file=sys.stderr)
        return 1

    print(f"Validated {len(args.manifests)} manifest(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
