from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

try:
    from .bundle_common import read_json, safe_relative_path, sha256_file
    from .report_binding import canonical_report_digest
except ImportError:  # pragma: no cover - exercised when invoked as a script
    from bundle_common import (  # type: ignore[import-not-found,no-redef]
        read_json,
        safe_relative_path,
        sha256_file,
    )
    from report_binding import canonical_report_digest  # type: ignore[import-not-found]


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
    "ci",
}
REQUIRED_G0_REPORT_KINDS = REPORT_KINDS - {"ci"}
EXPECTED_MANIFEST_BINDINGS: dict[str, tuple[str, str]] = {
    "effective_config_sha256": ("effective-config", "config"),
    "profile_sha256": ("profile", "profile"),
    "model_sha256": ("model", "model"),
    "classes_sha256": ("classes", "classes"),
    "split_sha256": ("dataset-split", "dataset-split"),
    "map_fingerprint": ("map-registry", "map-registry"),
    "platform_graph_sha256": ("platform-graph", "platform-graph"),
    "route_manifest_sha256": ("route-manifest", "route-manifest"),
    "movement_profile_sha256": ("movement-profile", "movement-profile"),
    "receiver_hash": ("receiver", "receiver"),
}
CORE_REPORT_FIELDS = {
    "test_report_id": "test",
    "replay_report_id": "replay",
    "shadow_report_id": "shadow",
    "clean_report_id": "clean-smoke",
}
FIXTURE_PATH = "fixtures/golden/pilot_minimal_v1.json"


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
    parser.add_argument(
        "--strict-g0",
        action="store_true",
        help="Require a complete, passed G0 evidence graph and raw-result bindings.",
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
    expected_paths: set[str] | None = None,
) -> None:
    if not checksum_path.is_file():
        errors.append(f"checksums: file does not exist: {checksum_path}")
        return
    observed_paths: set[str] = set()
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
            normalized_path = safe_relative_path(raw_path)
        except ValueError as exc:
            errors.append(f"checksums:{line_number}: {exc}")
            continue
        if normalized_path in observed_paths:
            errors.append(f"checksums:{line_number}: duplicate path {normalized_path}")
            continue
        observed_paths.add(normalized_path)
        try:
            path = _repo_path(repo_root, normalized_path)
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
    if expected_paths is not None and observed_paths != expected_paths:
        missing = sorted(expected_paths - observed_paths)
        extra = sorted(observed_paths - expected_paths)
        errors.append(f"checksums: exact path set required; missing={missing}, extra={extra}")


def _load_wrapped_artifact(
    *,
    repo_root: Path,
    report: dict[str, Any],
    label: str,
    errors: list[str],
) -> tuple[Path, dict[str, Any]] | None:
    artifacts = report.get("artifacts")
    candidates = (
        [
            item
            for item in artifacts
            if isinstance(item, dict) and item.get("kind") == "evidence-report"
        ]
        if isinstance(artifacts, list)
        else []
    )
    if len(candidates) != 1:
        errors.append(f"{label}: expected exactly one raw evidence-report artifact")
        return None
    artifact = candidates[0]
    try:
        path = _repo_path(repo_root, artifact["path"])
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"{label}: invalid raw report path: {exc}")
        return None
    _check_file(
        path,
        expected_size=artifact.get("size_bytes"),
        expected_sha256=artifact.get("sha256"),
        label=f"{label} raw report",
        errors=errors,
    )
    try:
        return path, read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: failed to read raw report: {exc}")
        return None


def _verify_bound_raw_report(
    *,
    repo_root: Path,
    bundle: dict[str, Any],
    manifest: dict[str, Any],
    wrapper: dict[str, Any],
    report_kind: str,
    errors: list[str],
) -> None:
    loaded = _load_wrapped_artifact(
        repo_root=repo_root,
        report=wrapper,
        label=f"strict G0 {report_kind}",
        errors=errors,
    )
    if loaded is None:
        return
    _, raw = loaded
    expected_type = "golden_replay" if report_kind == "replay" else "shadow"
    expected_report_id = f"{report_kind}-golden-pilot-minimal-v1-{bundle.get('release_id')}"
    expected_manifest_path = bundle.get("manifest_path")
    expected_manifest_sha256 = bundle.get("manifest_sha256")
    expected_source = bundle.get("source", {}).get("core_commit")
    for field, expected in (
        ("report_type", expected_type),
        ("status", "PASS"),
        ("report_id", expected_report_id),
        ("fixture_id", "golden-pilot-minimal-v1"),
        ("session_id", "golden-session-001"),
        ("bundle_id", bundle.get("bundle_id")),
        ("release_id", bundle.get("release_id")),
        ("source_commit", expected_source),
        ("runtime_manifest_path", expected_manifest_path),
        ("runtime_manifest_sha256", expected_manifest_sha256),
    ):
        if raw.get(field) != expected:
            errors.append(f"strict G0 {report_kind}: raw {field} binding mismatch")
    binding = raw.get("candidate_binding")
    if not isinstance(binding, dict):
        errors.append(f"strict G0 {report_kind}: candidate_binding is missing")
        binding = {}
    for field, expected in (
        ("bundle_id", bundle.get("bundle_id")),
        ("release_id", bundle.get("release_id")),
        ("source_commit", expected_source),
        ("runtime_manifest_path", expected_manifest_path),
        ("runtime_manifest_sha256", expected_manifest_sha256),
    ):
        if binding.get(field) != expected:
            errors.append(f"strict G0 {report_kind}: candidate_binding.{field} mismatch")
    declared_digest = raw.get("report_digest")
    if not isinstance(declared_digest, str) or declared_digest != canonical_report_digest(raw):
        errors.append(f"strict G0 {report_kind}: canonical report_digest mismatch")
    fixture_path = _repo_path(repo_root, FIXTURE_PATH)
    if raw.get("fixture_file_sha256") != sha256_file(fixture_path):
        errors.append(f"strict G0 {report_kind}: fixture file SHA-256 mismatch")
    checks = wrapper.get("checks")
    details = checks[0].get("details", {}) if isinstance(checks, list) and checks else {}
    if not isinstance(details, dict):
        details = {}
    if details.get("report_id") != raw.get("report_id"):
        errors.append(f"strict G0 {report_kind}: wrapper/raw report_id mismatch")
    if details.get("report_digest") != declared_digest:
        errors.append(f"strict G0 {report_kind}: wrapper/raw report_digest mismatch")

    if report_kind == "replay":
        runs = raw.get("runs")
        repeat_count = raw.get("repeat_count")
        if (
            raw.get("deterministic") is not True
            or type(repeat_count) is not int
            or repeat_count < 3
        ):
            errors.append("strict G0 replay: deterministic repeat_count>=3 is required")
        if not isinstance(runs, list) or len(runs) != repeat_count or not runs:
            errors.append("strict G0 replay: runs must match repeat_count and be non-empty")
        else:
            if any(not isinstance(item, dict) for item in runs):
                errors.append("strict G0 replay: every run must be an object")
            for digest_key in ("output_digest", "event_digest", "event_sequence_digest"):
                values = {item.get(digest_key) for item in runs if isinstance(item, dict)}
                if len(values) != 1 or None in values:
                    errors.append(f"strict G0 replay: {digest_key} is not deterministic")
            for index, item in enumerate(runs):
                if not isinstance(item, dict):
                    continue
                if (
                    item.get("run_index") != index
                    or type(item.get("event_count")) is not int
                    or item.get("event_count", 0) <= 0
                    or type(item.get("planned_action_count")) is not int
                    or item.get("planned_action_count", 0) <= 0
                ):
                    errors.append("strict G0 replay: run index/events/actions are invalid")
                    break
    else:
        audit = raw.get("input_audit")
        if not isinstance(audit, dict):
            errors.append("strict G0 shadow: input_audit is missing")
            audit = {}
        zero_fields = (
            "core_v2_real_input_call_count",
            "real_input_call_count",
            "keyboard_call_count",
            "mouse_call_count",
            "receiver_call_count",
            "window_call_count",
            "double_write_event_count",
            "core_execution_event_count",
        )
        for field in zero_fields:
            if audit.get(field) != 0:
                errors.append(f"strict G0 shadow: {field} must be zero")
        if audit.get("boundary_attempts") != [] or audit.get("connected") is not False:
            errors.append("strict G0 shadow: boundary ledger/disconnect invariant failed")
        summary = raw.get("diff_summary")
        diffs = raw.get("diffs")
        if (
            not isinstance(summary, dict)
            or summary.get("unclassified_diff_count") != 0
            or not isinstance(diffs, list)
            or not diffs
        ):
            errors.append("strict G0 shadow: classified non-empty diff evidence is required")


def _verify_clean_raw_report(
    *,
    repo_root: Path,
    bundle: dict[str, Any],
    wrapper: dict[str, Any],
    errors: list[str],
) -> None:
    loaded = _load_wrapped_artifact(
        repo_root=repo_root,
        report=wrapper,
        label="strict G0 clean-smoke",
        errors=errors,
    )
    if loaded is None:
        return
    _, raw = loaded
    _validate_document(
        raw,
        _load_schema("evidence-report.schema.json"),
        "strict G0 raw clean-smoke",
        errors,
    )
    source_commit = bundle.get("source", {}).get("core_commit")
    for field, expected in (
        ("report_kind", "clean-smoke"),
        ("status", "passed"),
        ("bundle_id", bundle.get("bundle_id")),
        ("release_id", bundle.get("release_id")),
        ("source_commit", source_commit),
    ):
        if raw.get(field) != expected:
            errors.append(f"strict G0 clean-smoke: raw {field} binding mismatch")
    summary = raw.get("summary")
    if not isinstance(summary, dict):
        errors.append("strict G0 clean-smoke: summary is missing")
        summary = {}
    if summary.get("runtime_manifest_path") != bundle.get("manifest_path"):
        errors.append("strict G0 clean-smoke: runtime manifest path mismatch")
    if summary.get("runtime_manifest_sha256") != bundle.get("manifest_sha256"):
        errors.append("strict G0 clean-smoke: runtime manifest SHA-256 mismatch")
    if summary.get("pip_cache") != "disabled" or summary.get("wheel_install") is not True:
        errors.append("strict G0 clean-smoke: cacheless wheel-install proof is missing")
    checks = raw.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(not isinstance(item, dict) or item.get("status") != "passed" for item in checks)
    ):
        errors.append("strict G0 clean-smoke: every raw check must pass")
    for artifact in raw.get("artifacts", []):
        if not isinstance(artifact, dict):
            errors.append("strict G0 clean-smoke: artifact entry must be an object")
            continue
        try:
            artifact_path = _repo_path(repo_root, artifact["path"])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"strict G0 clean-smoke: invalid artifact path: {exc}")
            continue
        _check_file(
            artifact_path,
            expected_size=artifact.get("size_bytes"),
            expected_sha256=artifact.get("sha256"),
            label=f"strict G0 clean artifact {artifact.get('artifact_id')}",
            errors=errors,
        )


def _verify_ci_raw_report(
    *,
    repo_root: Path,
    bundle: dict[str, Any],
    wrapper: dict[str, Any],
    errors: list[str],
) -> None:
    loaded = _load_wrapped_artifact(
        repo_root=repo_root,
        report=wrapper,
        label="strict G0 remote CI",
        errors=errors,
    )
    if loaded is None:
        return
    _, raw = loaded
    _validate_document(
        raw,
        _load_schema("ci-evidence.schema.json"),
        "strict G0 raw CI evidence",
        errors,
    )
    for field, expected in (
        ("status", "passed"),
        ("bundle_id", bundle.get("bundle_id")),
        ("release_id", bundle.get("release_id")),
        ("runner_os", "Windows"),
        ("dependency_install_result", "passed"),
    ):
        if raw.get(field) != expected:
            errors.append(f"strict G0 remote CI: raw {field} binding mismatch")
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id.isdigit() or int(run_id) <= 0:
        errors.append("strict G0 remote CI: run_id must identify a remote run")
    checks = raw.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(not isinstance(item, dict) or item.get("status") != "passed" for item in checks)
    ):
        errors.append("strict G0 remote CI: every recorded check must pass")
    artifacts = raw.get("artifacts")
    artifact_kinds = (
        {item.get("kind") for item in artifacts if isinstance(item, dict)}
        if isinstance(artifacts, list)
        else set()
    )
    for required_kind in ("junit", "coverage", "wheel", "sdist", "evidence-report"):
        if required_kind not in artifact_kinds:
            errors.append(f"strict G0 remote CI: missing {required_kind} artifact inventory")

    checkout_commit = raw.get("source_commit")
    candidate_commit = bundle.get("source", {}).get("core_commit")
    if not isinstance(checkout_commit, str) or not isinstance(candidate_commit, str):
        errors.append("strict G0 remote CI: source lineage is missing")
        return
    exists = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{checkout_commit}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    lineage = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "merge-base",
            "--is-ancestor",
            candidate_commit,
            checkout_commit,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    changed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--name-only",
            f"{candidate_commit}..{checkout_commit}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    changed_paths = [line.strip().replace("\\", "/") for line in changed.stdout.splitlines()]
    unexpected = [
        path
        for path in changed_paths
        if path and not path.startswith(("bundles/", "evidence/", "docs/"))
    ]
    if exists.returncode != 0 or lineage.returncode != 0 or changed.returncode != 0:
        errors.append("strict G0 remote CI: checkout is not a known candidate descendant")
    if unexpected:
        errors.append(
            "strict G0 remote CI: checkout contains non-packaging changes: " + ", ".join(unexpected)
        )


def _verify_strict_g0(
    *,
    repo_root: Path,
    bundle: dict[str, Any],
    manifest: dict[str, Any],
    evidence_index: dict[str, Any],
    evidence_entries: list[dict[str, Any]],
    reports: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    bundle_evidence = bundle.get("evidence", {})
    if evidence_index.get("evidence_index_id") != bundle_evidence.get("index_id"):
        errors.append("strict G0: bundle evidence index ID mismatch")
    entries_by_id = {entry.get("evidence_id"): entry for entry in evidence_entries}
    for manifest_field, expected_kind in CORE_REPORT_FIELDS.items():
        report_id = bundle_evidence.get(manifest_field)
        if manifest.get(manifest_field) != report_id:
            errors.append(f"strict G0: manifest.{manifest_field} does not match bundle")
        entry = entries_by_id.get(report_id)
        report = reports.get(str(report_id))
        if entry is None:
            errors.append(f"strict G0: evidence entry is missing for {manifest_field}")
            continue
        if entry.get("kind") != expected_kind or entry.get("status") != "passed":
            errors.append(f"strict G0: {manifest_field} entry must be passed {expected_kind}")
        if report is None:
            errors.append(f"strict G0: report document is missing for {manifest_field}")
        elif report.get("status") != "passed":
            errors.append(f"strict G0: report {report_id} must be passed")

    reports_by_kind: dict[str, list[dict[str, Any]]] = {}
    for report in reports.values():
        reports_by_kind.setdefault(str(report.get("report_kind")), []).append(report)
        checks = report.get("checks")
        if (
            report.get("status") != "passed"
            or not isinstance(checks, list)
            or any(not isinstance(item, dict) or item.get("status") != "passed" for item in checks)
        ):
            errors.append(f"strict G0: every check in {report.get('evidence_id')} must be passed")
    for kind in REQUIRED_G0_REPORT_KINDS:
        if len(reports_by_kind.get(kind, [])) != 1:
            errors.append(f"strict G0: expected exactly one {kind} evidence report")

    ci_report_id = bundle_evidence.get("ci_report_id")
    if ci_report_id is not None:
        ci_entry = entries_by_id.get(ci_report_id)
        ci_report = reports.get(str(ci_report_id))
        if (
            ci_entry is None
            or ci_entry.get("kind") != "ci"
            or ci_entry.get("status") != "passed"
            or ci_report is None
            or ci_report.get("status") != "passed"
        ):
            errors.append("strict G0: bound remote CI report must be present and passed")
        elif len(reports_by_kind.get("ci", [])) != 1:
            errors.append("strict G0: expected exactly one bound CI evidence report")
        else:
            _verify_ci_raw_report(
                repo_root=repo_root,
                bundle=bundle,
                wrapper=ci_report,
                errors=errors,
            )

    local_paths = [item.get("path") for item in bundle.get("local_files", [])]
    if len(set(local_paths)) != len(local_paths):
        errors.append("strict G0: bundle.local_files paths must be unique")
    required_local_paths = {
        bundle.get("manifest_path"),
        bundle.get("asset_index_path"),
        bundle.get("evidence_index_path"),
        *(entry.get("path") for entry in evidence_entries),
    }
    missing_local_paths = sorted(
        str(path) for path in required_local_paths if path not in set(local_paths)
    )
    if missing_local_paths:
        errors.append(
            "strict G0: local_files missing indexed paths: " + ", ".join(missing_local_paths)
        )

    for field, kind in (
        ("replay_report_id", "replay"),
        ("shadow_report_id", "shadow"),
    ):
        report = reports.get(str(bundle_evidence.get(field)))
        if report is not None:
            _verify_bound_raw_report(
                repo_root=repo_root,
                bundle=bundle,
                manifest=manifest,
                wrapper=report,
                report_kind=kind,
                errors=errors,
            )
    clean_report = reports.get(str(bundle_evidence.get("clean_report_id")))
    if clean_report is not None:
        _verify_clean_raw_report(
            repo_root=repo_root,
            bundle=bundle,
            wrapper=clean_report,
            errors=errors,
        )


def verify_bundle(
    *,
    bundle_dir: Path,
    repo_root: Path,
    roots: dict[str, Path] | None = None,
    metadata_only: bool = False,
    strict_g0: bool = False,
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
    if manifest.get("asset_index_path") != bundle.get("asset_index_path"):
        errors.append("metadata binding: manifest.asset_index_path does not match bundle")
    if manifest.get("asset_index_sha256") != bundle.get("asset_index_sha256"):
        errors.append("metadata binding: manifest.asset_index_sha256 does not match bundle")
    if manifest.get("evidence_index_path") != bundle.get("evidence_index_path"):
        errors.append("metadata binding: manifest.evidence_index_path does not match bundle")
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
    manifest_bindings = bundle.get("manifest_asset_bindings", {})
    if set(manifest_bindings) != set(EXPECTED_MANIFEST_BINDINGS):
        missing = sorted(set(EXPECTED_MANIFEST_BINDINGS) - set(manifest_bindings))
        extra = sorted(set(manifest_bindings) - set(EXPECTED_MANIFEST_BINDINGS))
        errors.append(
            f"manifest bindings: exact field set required; missing={missing}, extra={extra}"
        )
    for field, asset_id in manifest_bindings.items():
        entry = assets_by_id.get(asset_id)
        if entry is None:
            errors.append(f"manifest binding: unknown asset_id {asset_id!r} for {field}")
            continue
        if manifest.get(field) != entry.get("sha256"):
            errors.append(f"manifest binding: {field} does not match asset {asset_id}")
        expected = EXPECTED_MANIFEST_BINDINGS.get(field)
        if expected is not None:
            expected_asset_id, expected_role = expected
            if asset_id != expected_asset_id:
                errors.append(f"manifest binding: {field} must reference asset {expected_asset_id}")
            if entry.get("role") != expected_role:
                errors.append(f"manifest binding: asset {asset_id} role must be {expected_role}")

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
    evidence_ids = [entry.get("evidence_id") for entry in evidence_entries]
    evidence_paths = [entry.get("path") for entry in evidence_entries]
    if len(set(evidence_ids)) != len(evidence_ids):
        errors.append("evidence-index: evidence_id values must be unique")
    if len(set(evidence_paths)) != len(evidence_paths):
        errors.append("evidence-index: path values must be unique")
    parsed_reports: dict[str, dict[str, Any]] = {}
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
        claims = entry.get("claims", [])
        is_report_entry = (
            entry.get("kind") in REPORT_KINDS
            and evidence_path.suffix.lower() == ".json"
            and (
                f"report_kind:{entry.get('kind')}" in claims
                or "-report-" in str(entry.get("evidence_id", ""))
            )
        )
        if is_report_entry:
            try:
                report = read_json(evidence_path)
                _validate_document(
                    report,
                    _load_schema("evidence-report.schema.json"),
                    f"evidence-report {entry.get('evidence_id')}",
                    errors,
                )
                report_id = str(entry.get("evidence_id"))
                parsed_reports[report_id] = report
                for field, expected in (
                    ("evidence_id", entry.get("evidence_id")),
                    ("report_kind", entry.get("kind")),
                    ("status", entry.get("status")),
                    ("bundle_id", bundle.get("bundle_id")),
                    ("release_id", bundle.get("release_id")),
                    ("source_commit", source.get("core_commit")),
                    ("execution_mode", bundle.get("execution_mode")),
                    ("subject_id", bundle.get("subject_id")),
                ):
                    if report.get(field) != expected:
                        errors.append(
                            f"evidence-report {entry.get('evidence_id')}: {field} binding mismatch"
                        )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"evidence-report {entry.get('evidence_id')}: {exc}")

    if strict_g0:
        _verify_strict_g0(
            repo_root=repo_root,
            bundle=bundle,
            manifest=manifest,
            evidence_index=evidence_index,
            evidence_entries=evidence_entries,
            reports=parsed_reports,
            errors=errors,
        )

    expected_checksum_paths = {str(item.get("path")) for item in bundle.get("local_files", [])}
    expected_checksum_paths.add(safe_relative_path(bundle_path.relative_to(repo_root).as_posix()))
    _verify_checksums(
        repo_root,
        bundle_dir / "checksums.sha256",
        errors,
        expected_checksum_paths,
    )
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
            strict_g0=args.strict_g0,
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
    if args.strict_g0:
        mode += ", strict G0"
    print(f"Bundle verified ({mode}): {args.bundle_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
