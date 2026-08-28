from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

try:
    from .bundle_common import read_json, safe_relative_path, sha256_file
except ImportError:  # pragma: no cover - exercised when invoked as a script
    from bundle_common import (  # type: ignore[import-not-found,no-redef]
        read_json,
        safe_relative_path,
        sha256_file,
    )


SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
REPORT_KINDS = {
    "test",
    "replay",
    "shadow",
    "clean-smoke",
    "build",
    "dependency",
    "manifest",
    "hash",
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a portable Candidate/Shadow Runtime Bundle, its local hashes, "
            "and its referenced external asset hashes."
        )
    )
    parser.add_argument(
        "--bundle-dir",
        required=True,
        type=Path,
        help="Bundle directory containing bundle.json (relative to --repo-root).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Core repository root (default: repository containing this tool).",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="ENV=PATH",
        help="External asset root override; repeat for each root environment variable.",
    )
    parser.add_argument(
        "--legacy-root",
        type=Path,
        help="Convenience override for MAPLE_LEGACY_ROOT.",
    )
    parser.add_argument(
        "--model-root",
        type=Path,
        help="Convenience override for MAPLE_MODEL_ROOT.",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Verify local metadata and schemas without reading external asset roots.",
    )
    return parser.parse_args(argv)


def _schema_path(name: str) -> Path:
    return SCHEMA_DIR / name


def _load_schema(name: str) -> dict[str, Any]:
    path = _schema_path(name)
    payload = read_json(path)
    try:
        Draft202012Validator.check_schema(payload)
    except SchemaError as exc:
        raise ValueError(f"Invalid schema {path}: {exc.message}") from exc
    return payload


def _validate_document(
    document: dict[str, Any],
    schema: dict[str, Any],
    label: str,
    errors: list[str],
) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for error in sorted(validator.iter_errors(document), key=lambda item: tuple(item.path)):
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"{label} {location}: {error.message}")


def _repo_path(repo_root: Path, value: str) -> Path:
    normalized = safe_relative_path(value)
    candidate = (repo_root / Path(*normalized.split("/"))).resolve()
    if not candidate.is_relative_to(repo_root.resolve()):
        raise ValueError(f"Portable path escaped repository root: {value!r}")
    return candidate


def _check_file(
    path: Path,
    *,
    expected_size: int | None,
    expected_sha256: str | None,
    label: str,
    errors: list[str],
) -> None:
    if not path.is_file():
        errors.append(f"{label}: file does not exist: {path}")
        return
    actual_size = path.stat().st_size
    actual_sha256 = sha256_file(path)
    if expected_size is not None and actual_size != expected_size:
        errors.append(
            f"{label}: size mismatch (expected {expected_size}, got {actual_size}): {path}"
        )
    if expected_sha256 is not None and actual_sha256.lower() != expected_sha256.lower():
        errors.append(
            f"{label}: SHA-256 mismatch (expected {expected_sha256}, got {actual_sha256}): {path}"
        )


def _parse_root_overrides(values: Iterable[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not name or not raw_path:
            raise ValueError(f"Root override must use ENV=PATH syntax: {value!r}")
        if not name.isidentifier() or name.upper() != name:
            raise ValueError(f"Root environment name must be uppercase: {name!r}")
        root = Path(raw_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"External asset root does not exist: {root}")
        roots[name] = root
    return roots


def _resolve_external_roots(
    overrides: dict[str, Path],
    convenience: dict[str, Path | None],
) -> dict[str, Path]:
    roots: dict[str, Path] = {
        name: Path(value).expanduser().resolve()
        for name, value in os.environ.items()
        if name.startswith("MAPLE_") and value
    }
    roots.update(overrides)
    for name, value in convenience.items():
        if value is not None:
            roots[name] = value.expanduser().resolve()
    return roots


def _verify_checksums(
    repo_root: Path,
    checksum_path: Path,
    errors: list[str],
) -> None:
    if not checksum_path.is_file():
        errors.append(f"checksums: file does not exist: {checksum_path}")
        return
    for line_number, line in enumerate(
        checksum_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            errors.append(f"checksums:{line_number}: expected '<sha256>  <path>'")
            continue
        expected_sha256, raw_path = parts
        try:
            path = _repo_path(repo_root, raw_path)
        except ValueError as exc:
            errors.append(f"checksums:{line_number}: {exc}")
            continue
        _check_file(
            path,
            expected_size=None,
            expected_sha256=expected_sha256,
            label=f"checksums:{line_number}",
            errors=errors,
        )


def verify_bundle(
    *,
    bundle_dir: Path,
    repo_root: Path,
    roots: dict[str, Path] | None = None,
    metadata_only: bool = False,
) -> list[str]:
    """Return all verification errors; an empty list means the bundle is valid."""

    errors: list[str] = []
    repo_root = repo_root.resolve()
    try:
        bundle_dir = bundle_dir if bundle_dir.is_absolute() else repo_root / bundle_dir
        bundle_dir = bundle_dir.resolve()
        if not bundle_dir.is_relative_to(repo_root):
            return [f"Bundle directory must be inside repository root: {bundle_dir}"]
        bundle_path = bundle_dir / "bundle.json"
        bundle = read_json(bundle_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"bundle: failed to read {bundle_dir / 'bundle.json'}: {exc}"]

    _validate_document(bundle, _load_schema("runtime-bundle.schema.json"), "bundle", errors)
    if errors:
        return errors

    try:
        manifest_path = _repo_path(repo_root, bundle["manifest_path"])
        asset_index_path = _repo_path(repo_root, bundle["asset_index_path"])
        evidence_index_path = _repo_path(repo_root, bundle["evidence_index_path"])
    except (KeyError, TypeError, ValueError) as exc:
        return [f"bundle paths: {exc}"]

    try:
        manifest = read_json(manifest_path)
        asset_index = read_json(asset_index_path)
        evidence_index = read_json(evidence_index_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"referenced metadata: failed to read: {exc}")
        return errors

    _validate_document(
        manifest,
        _load_schema("runtime-manifest.schema.json"),
        "manifest",
        errors,
    )
    _validate_document(
        asset_index,
        _load_schema("runtime-asset-index.schema.json"),
        "asset-index",
        errors,
    )
    _validate_document(
        evidence_index,
        _load_schema("evidence-index.schema.json"),
        "evidence-index",
        errors,
    )
    _check_file(
        manifest_path,
        expected_size=None,
        expected_sha256=bundle["manifest_sha256"],
        label="bundle.manifest_sha256",
        errors=errors,
    )
    _check_file(
        asset_index_path,
        expected_size=None,
        expected_sha256=bundle["asset_index_sha256"],
        label="bundle.asset_index_sha256",
        errors=errors,
    )
    _check_file(
        evidence_index_path,
        expected_size=None,
        expected_sha256=bundle["evidence_index_sha256"],
        label="bundle.evidence_index_sha256",
        errors=errors,
    )

    if manifest.get("release_id") != bundle.get("release_id"):
        errors.append("source binding: manifest.release_id does not match bundle.release_id")
    if asset_index.get("bundle_id") != bundle.get("bundle_id"):
        errors.append("source binding: asset-index.bundle_id does not match bundle.bundle_id")
    if asset_index.get("release_id") != bundle.get("release_id"):
        errors.append("source binding: asset-index.release_id does not match bundle.release_id")
    if evidence_index.get("bundle_id") != bundle.get("bundle_id"):
        errors.append("source binding: evidence-index.bundle_id does not match bundle.bundle_id")
    if evidence_index.get("release_id") != bundle.get("release_id"):
        errors.append("source binding: evidence-index.release_id does not match bundle.release_id")
    source = bundle.get("source", {})
    if manifest.get("source_commit") != source.get("core_commit"):
        errors.append("source binding: manifest.source_commit does not match bundle source")
    if manifest.get("upstream_commit") != source.get("upstream_commit"):
        errors.append("source binding: manifest.upstream_commit does not match bundle source")
    if asset_index.get("source_commit") != source.get("core_commit"):
        errors.append("source binding: asset-index.source_commit does not match bundle source")
    if evidence_index.get("source_commit") != source.get("core_commit"):
        errors.append("source binding: evidence-index.source_commit does not match bundle source")
    for field, expected in (
        ("bundle_status", bundle.get("lifecycle")),
        ("execution_mode", bundle.get("execution_mode")),
        ("subject_id", bundle.get("subject_id")),
    ):
        if field in manifest and manifest[field] != expected:
            errors.append(f"policy binding: manifest.{field} does not match bundle")
    if manifest.get("real_input_enabled") is not False:
        errors.append("policy binding: manifest.real_input_enabled must be false")
    input_policy = bundle.get("input_policy", {})
    if manifest.get("input_owner") != input_policy.get("owner"):
        errors.append("policy binding: manifest.input_owner does not match bundle input policy")
    if evidence_index.get("execution_mode") != bundle.get("execution_mode"):
        errors.append("policy binding: evidence-index.execution_mode does not match bundle")
    if evidence_index.get("subject_id") != bundle.get("subject_id"):
        errors.append("policy binding: evidence-index.subject_id does not match bundle")

    asset_entries = asset_index.get("entries", [])
    assets_by_id = {entry.get("asset_id"): entry for entry in asset_entries}
    if len(assets_by_id) != len(asset_entries):
        errors.append("asset-index: asset_id values must be unique")
    for field, asset_id in bundle.get("manifest_asset_bindings", {}).items():
        entry = assets_by_id.get(asset_id)
        if entry is None:
            errors.append(f"manifest binding: unknown asset_id {asset_id!r} for {field}")
            continue
        if manifest.get(field) != entry.get("sha256"):
            errors.append(f"manifest binding: {field} does not match asset {asset_id}")

    resolved_roots = {"MAPLE_CORE_ROOT": repo_root, **(roots or {})}
    for entry in asset_entries:
        source_info = entry.get("source", {})
        root_env = source_info.get("root_env")
        relative_path = source_info.get("relative_path")
        if metadata_only and root_env != "MAPLE_CORE_ROOT":
            continue
        root = resolved_roots.get(root_env)
        if root is None:
            errors.append(f"asset {entry.get('asset_id')}: missing root {root_env}")
            continue
        try:
            root = root.resolve()
            asset_path = (root / Path(*safe_relative_path(relative_path).split("/"))).resolve()
        except (TypeError, ValueError) as exc:
            errors.append(f"asset {entry.get('asset_id')}: invalid relative path: {exc}")
            continue
        if not asset_path.is_relative_to(root):
            errors.append(f"asset {entry.get('asset_id')}: path escaped root")
            continue
        _check_file(
            asset_path,
            expected_size=entry.get("size_bytes"),
            expected_sha256=entry.get("sha256"),
            label=f"asset {entry.get('asset_id')}",
            errors=errors,
        )

    for local_file in bundle.get("local_files", []):
        try:
            local_path = _repo_path(repo_root, local_file["path"])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"bundle.local_files: {exc}")
            continue
        _check_file(
            local_path,
            expected_size=local_file.get("size_bytes"),
            expected_sha256=local_file.get("sha256"),
            label=f"bundle.local_files[{local_file.get('path')}]",
            errors=errors,
        )

    evidence_entries = evidence_index.get("entries", [])
    for entry in evidence_entries:
        try:
            evidence_path = _repo_path(repo_root, entry["path"])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"evidence-index entry: {exc}")
            continue
        _check_file(
            evidence_path,
            expected_size=entry.get("size_bytes"),
            expected_sha256=entry.get("sha256"),
            label=f"evidence {entry.get('evidence_id')}",
            errors=errors,
        )
        if entry.get("kind") in REPORT_KINDS and evidence_path.suffix.lower() == ".json":
            try:
                report = read_json(evidence_path)
                if "report_kind" in report:
                    _validate_document(
                        report,
                        _load_schema("evidence-report.schema.json"),
                        f"evidence-report {entry.get('evidence_id')}",
                        errors,
                    )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"evidence-report {entry.get('evidence_id')}: {exc}")

    _verify_checksums(repo_root, bundle_dir / "checksums.sha256", errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        overrides = _parse_root_overrides(args.root)
        roots = _resolve_external_roots(
            overrides,
            {
                "MAPLE_LEGACY_ROOT": args.legacy_root,
                "MAPLE_MODEL_ROOT": args.model_root,
            },
        )
        errors = verify_bundle(
            bundle_dir=args.bundle_dir,
            repo_root=args.repo_root,
            roots=roots,
            metadata_only=args.metadata_only,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if errors:
        print(f"Bundle verification failed with {len(errors)} error(s):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    mode = "metadata-only" if args.metadata_only else "full external hash"
    print(f"Bundle verified ({mode}): {args.bundle_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
