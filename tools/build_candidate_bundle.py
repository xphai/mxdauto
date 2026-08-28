from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

try:
    from .bundle_common import (
        file_metadata,
        git_commit,
        read_json,
        resolve_root,
        safe_relative_path,
        sha256_file,
        write_json,
    )
    from .report_binding import canonical_report_digest
    from .verify_dependency_lock import parse_lock, verify_installed
except ImportError:  # pragma: no cover - exercised when invoked as a script
    from bundle_common import (  # type: ignore[import-not-found,no-redef]
        file_metadata,
        git_commit,
        read_json,
        resolve_root,
        safe_relative_path,
        sha256_file,
        write_json,
    )
    from report_binding import canonical_report_digest  # type: ignore[import-not-found]
    from verify_dependency_lock import (  # type: ignore[import-not-found]
        parse_lock,
        verify_installed,
    )


TOOL_VERSION = "1.0.0"
BUNDLE_SCHEMA_VERSION = "1.0.0"
RUNTIME_MANIFEST_VERSION = "1.0.0"
BUNDLE_ID = "candidate-core-v2-20260829-shadow"
RELEASE_ID = BUNDLE_ID
SUBJECT_ID = "subject-01"
PROFILE_ID = "pilot-subject-01"
MODEL_ID = "best_forest_v3-candidate"
MAP_ID = "100040004"
EVIDENCE_INDEX_ID = "evidence-index-g0-candidate-shadow-20260829"
TEST_REPORT_ID = "test-report-g0-candidate-shadow-20260829"
REPLAY_REPORT_ID = "replay-report-g0-candidate-shadow-20260829"
SHADOW_REPORT_ID = "shadow-report-g0-candidate-shadow-20260829"
CLEAN_REPORT_ID = "clean-report-g0-candidate-shadow-20260829"
CI_REPORT_ID = "ci-report-g0-candidate-shadow-20260829"
FIXTURE_RELATIVE_PATH = "fixtures/golden/pilot_minimal_v1.json"
EXPECTED_SESSION_ID = "golden-session-001"
MINIMUM_COVERAGE_RATE = 0.90
ALLOWED_PACKAGING_PREFIXES = ("bundles/", "evidence/", "docs/")


@dataclass(frozen=True, slots=True)
class AssetSpec:
    asset_id: str
    role: str
    root_env: str
    relative_path: str


ASSET_SPECS: tuple[AssetSpec, ...] = (
    AssetSpec(
        "effective-config",
        "config",
        "MAPLE_CORE_ROOT",
        "configs/pilot-subject-01.resolved.json",
    ),
    AssetSpec(
        "profile",
        "profile",
        "MAPLE_CORE_ROOT",
        "configs/pilot-subject-01.profile.json",
    ),
    AssetSpec(
        "classes",
        "classes",
        "MAPLE_LEGACY_ROOT",
        "profiles/maple_legacy_cn/models/classes_v14_mob_only.yaml",
    ),
    AssetSpec(
        "model",
        "model",
        "MAPLE_MODEL_ROOT",
        "mob_synth_v2/weights/best_forest_v3.onnx",
    ),
    AssetSpec(
        "dataset-split",
        "dataset-split",
        "MAPLE_MODEL_ROOT",
        "mob_synth_v2/forest_calibration_v2/data.yaml",
    ),
    AssetSpec(
        "map-registry",
        "map-registry",
        "MAPLE_LEGACY_ROOT",
        "minimaps/iv_20260823_073124/maoxiandao_map.yaml",
    ),
    AssetSpec(
        "platform-graph",
        "platform-graph",
        "MAPLE_LEGACY_ROOT",
        "mapdata/maoxiandao/mxdc_maptools.sqlite3",
    ),
    AssetSpec(
        "route-manifest",
        "route-manifest",
        "MAPLE_LEGACY_ROOT",
        "minimaps/iv_20260823_073124/spawn_route_manifest.yaml",
    ),
    AssetSpec(
        "movement-profile",
        "movement-profile",
        "MAPLE_LEGACY_ROOT",
        "profiles/movement/角色运动档案.yaml",
    ),
    AssetSpec(
        "receiver",
        "receiver",
        "MAPLE_LEGACY_ROOT",
        "receiver/input_receiver.ps1",
    ),
)


MANIFEST_BINDINGS = {
    "effective_config_sha256": "effective-config",
    "profile_sha256": "profile",
    "model_sha256": "model",
    "classes_sha256": "classes",
    "split_sha256": "dataset-split",
    "map_fingerprint": "map-registry",
    "platform_graph_sha256": "platform-graph",
    "route_manifest_sha256": "route-manifest",
    "movement_profile_sha256": "movement-profile",
    "receiver_hash": "receiver",
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a portable Candidate/Shadow Runtime Bundle descriptor."
    )
    parser.add_argument(
        "--core-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Core repository root (default: the repository containing this tool).",
    )
    parser.add_argument(
        "--legacy-root",
        type=Path,
        help="Legacy source root; defaults to MAPLE_LEGACY_ROOT.",
    )
    parser.add_argument(
        "--model-root",
        type=Path,
        help="Model/data root; defaults to MAPLE_MODEL_ROOT.",
    )
    parser.add_argument(
        "--upstream-root",
        type=Path,
        help="Upstream git checkout; defaults to MAPLE_UPSTREAM_ROOT.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("bundles") / BUNDLE_ID,
        help="Bundle output directory, relative to --core-root by default.",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path("evidence") / "g0-candidate-core-v2-20260829",
        help="Evidence output directory, relative to --core-root by default.",
    )
    parser.add_argument(
        "--created-at",
        default="2026-08-29T00:00:00Z",
        help="UTC timestamp to bind all generated metadata (ISO8601).",
    )
    parser.add_argument(
        "--source-commit",
        help="40-character Core source commit override (defaults to repository HEAD).",
    )
    parser.add_argument(
        "--junit",
        type=Path,
        help="JUnit XML produced by the local/CI test run.",
    )
    parser.add_argument(
        "--coverage",
        type=Path,
        help="Coverage XML produced by the local/CI test run.",
    )
    parser.add_argument(
        "--replay-result",
        type=Path,
        help="Candidate-bound Golden Replay result JSON.",
    )
    parser.add_argument(
        "--shadow-result",
        type=Path,
        help="Candidate-bound Shadow result JSON.",
    )
    parser.add_argument(
        "--clean-result",
        type=Path,
        help="Cacheless Windows clean-smoke evidence report JSON.",
    )
    parser.add_argument(
        "--ci-result",
        type=Path,
        help="Downloaded GitHub Actions ci-evidence JSON to bind into the packet.",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        type=Path,
        help="Build artifact to include in the evidence metadata (repeatable).",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path("configs") / "requirements.lock",
        help="Dependency lock file, relative to --core-root by default.",
    )
    parser.add_argument(
        "--dependency-check-installed",
        action="store_true",
        help=(
            "Compare the active environment with every exact lock entry and bind "
            "that measured result into the dependency evidence report."
        ),
    )
    return parser.parse_args(argv)


def _repo_path(core_root: Path, value: Path) -> Path:
    candidate = value if value.is_absolute() else core_root / value
    resolved = candidate.resolve()
    if not resolved.is_relative_to(core_root.resolve()):
        raise ValueError(f"Path must be inside the core repository: {value}")
    return resolved


def _repo_relative(core_root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(core_root.resolve())
    except ValueError as exc:
        raise ValueError(f"Path must be inside the core repository: {path}") from exc
    return safe_relative_path(relative.as_posix())


def _load_asset_metadata(
    specs: Iterable[AssetSpec],
    roots: dict[str, Path],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for spec in specs:
        root = roots[spec.root_env]
        relative_path = safe_relative_path(spec.relative_path)
        path = (root / Path(*relative_path.split("/"))).resolve()
        if not path.is_file():
            raise ValueError(
                f"Required external asset does not exist: {spec.root_env}/{relative_path}"
            )
        if not path.is_relative_to(root):
            raise ValueError(f"Asset escaped its configured root: {spec.root_env}/{relative_path}")
        size_bytes, digest, last_write_utc = file_metadata(path)
        entry = {
            "asset_id": spec.asset_id,
            "exists_at_generation": True,
            "last_write_utc": last_write_utc,
            "role": spec.role,
            "sha256": digest,
            "size_bytes": size_bytes,
            "source": {
                "relative_path": relative_path,
                "root_env": spec.root_env,
                "uri": (
                    f"repo://{spec.root_env}/{relative_path}"
                    if spec.root_env == "MAPLE_CORE_ROOT"
                    else f"external://{spec.root_env}/{relative_path}"
                ),
            },
        }
        entries.append(entry)
        by_id[spec.asset_id] = entry
    return entries, by_id


def _read_junit(path: Path | None) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    if path is None:
        return "not-run", {"reason": "JUnit XML was not supplied."}, []
    if not path.is_file():
        return "failed", {"reason": f"JUnit XML does not exist: {path}"}, []
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag.endswith("testsuite") else list(root.findall(".//testsuite"))
    totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for key in totals:
            try:
                totals[key] += int(suite.attrib.get(key, "0"))
            except ValueError:
                totals[key] += 0
    executed = totals["tests"] - totals["skipped"]
    status = (
        "passed"
        if totals["tests"] > 0
        and executed > 0
        and totals["failures"] == 0
        and totals["errors"] == 0
        else "failed"
    )
    totals["executed"] = executed
    artifact = _artifact_record(path, "junit", "junit")
    return status, totals, [artifact]


def _read_coverage(path: Path | None) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    if path is None:
        return "not-run", {"reason": "Coverage XML was not supplied."}, []
    if not path.is_file():
        return "failed", {"reason": f"Coverage XML does not exist: {path}"}, []
    root = ElementTree.parse(path).getroot()
    try:
        line_rate = float(root.attrib.get("line-rate", "0"))
    except ValueError:
        line_rate = 0.0
    try:
        lines_covered = int(root.attrib.get("lines-covered", "0"))
        lines_valid = int(root.attrib.get("lines-valid", "0"))
    except ValueError:
        lines_covered = 0
        lines_valid = 0
    details = {
        "line_rate": line_rate,
        "line_percent": round(line_rate * 100, 2),
        "lines_covered": lines_covered,
        "lines_valid": lines_valid,
        "minimum_line_rate": MINIMUM_COVERAGE_RATE,
    }
    artifact = _artifact_record(path, "coverage", "coverage")
    status = (
        "passed"
        if lines_valid > 0
        and 0 <= lines_covered <= lines_valid
        and line_rate >= MINIMUM_COVERAGE_RATE
        else "failed"
    )
    return status, details, [artifact]


def _artifact_record(path: Path, kind: str, prefix: str) -> dict[str, Any]:
    resolved = path.resolve()
    size_bytes, digest, _ = file_metadata(resolved)
    return {
        "artifact_id": f"{prefix}-{resolved.stem.lower().replace('_', '-')}",
        "kind": kind,
        "name": resolved.name,
        "path": resolved.as_posix(),
        "sha256": digest,
        "size_bytes": size_bytes,
    }


def _portable_artifact_record(
    core_root: Path, path: Path, kind: str, prefix: str
) -> dict[str, Any]:
    record = _artifact_record(path, kind, prefix)
    record["path"] = _repo_relative(core_root, path)
    return record


def _environment() -> dict[str, str]:
    return {
        "python_version": platform.python_version(),
        "runner_os": platform.system(),
        "working_directory_policy": "repository-relative",
    }


def _write_checksums(core_root: Path, paths: Iterable[Path], output: Path) -> None:
    lines = []
    resolved_paths = {path.resolve() for path in paths}
    for path in sorted(
        resolved_paths,
        key=lambda item: _repo_relative(core_root, item),
    ):
        lines.append(f"{sha256_file(path)}  {_repo_relative(core_root, path)}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _status_from_checks(statuses: Iterable[str]) -> str:
    values = list(statuses)
    if any(value == "failed" for value in values):
        return "failed"
    if values and all(value == "passed" for value in values):
        return "passed"
    return "not-run"


def _report(
    *,
    report_kind: str,
    evidence_id: str,
    status: str,
    bundle_id: str,
    release_id: str,
    source_commit: str,
    generated_at: str,
    checks: list[dict[str, Any]],
    artifacts: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    environment = _environment()
    return {
        "artifacts": artifacts or [],
        "bundle_id": bundle_id,
        "checks": checks,
        "environment": environment,
        "evidence_id": evidence_id,
        "execution_mode": "shadow",
        "generated_at": generated_at,
        "release_id": release_id,
        "report_kind": report_kind,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "source_commit": source_commit,
        "status": status,
        "subject_id": SUBJECT_ID,
        "summary": summary or {},
    }


def _schema_validation_messages(
    *, core_root: Path, schema_name: str, document: dict[str, Any]
) -> list[str]:
    schema = read_json(core_root / "schemas" / schema_name)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda item: tuple(map(str, item.path)))
    return [
        f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
        for error in errors
    ]


def _validate_generated_document(
    *, core_root: Path, schema_name: str, document: dict[str, Any], label: str
) -> None:
    errors = _schema_validation_messages(
        core_root=core_root, schema_name=schema_name, document=document
    )
    if errors:
        raise ValueError(f"Generated {label} failed schema validation: {'; '.join(errors)}")


def _validated_source_commit(value: str | None, core_root: Path) -> str:
    commit = value or git_commit(core_root)
    if len(commit) != 40 or any(character not in "0123456789abcdefABCDEF" for character in commit):
        raise ValueError(f"Expected a 40-character source commit, got {commit!r}")
    commit = commit.lower()
    exists = subprocess.run(
        ["git", "-C", str(core_root), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode != 0:
        raise ValueError(f"Source commit does not exist in the Core repository: {commit}")
    ancestor = subprocess.run(
        ["git", "-C", str(core_root), "merge-base", "--is-ancestor", commit, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ValueError(f"Source commit is not an ancestor of the packaging checkout: {commit}")
    changed = subprocess.run(
        ["git", "-C", str(core_root), "diff", "--name-only", f"{commit}..HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if changed.returncode != 0:
        raise ValueError(f"Could not compare source commit with packaging checkout: {commit}")
    changed_paths = [line.strip().replace("\\", "/") for line in changed.stdout.splitlines()]
    unexpected = [
        path for path in changed_paths if path and not path.startswith(ALLOWED_PACKAGING_PREFIXES)
    ]
    if unexpected:
        raise ValueError(
            "Source commit omits runtime code/config changes: " + ", ".join(unexpected)
        )
    return commit


def _validate_packaging_worktree(core_root: Path) -> None:
    status = subprocess.run(
        ["git", "-C", str(core_root), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        raise ValueError("Could not inspect Core packaging worktree state.")
    dirty_paths: list[str] = []
    for line in status.stdout.splitlines():
        raw_path = line[3:].strip().replace("\\", "/") if len(line) >= 4 else line.strip()
        candidates = raw_path.split(" -> ")
        dirty_paths.extend(path.strip('"') for path in candidates if path)
    unexpected = [path for path in dirty_paths if not path.startswith(ALLOWED_PACKAGING_PREFIXES)]
    if unexpected:
        raise ValueError(
            "Packaging worktree contains uncommitted runtime/config changes: "
            + ", ".join(sorted(unexpected))
        )


def _read_bound_result(
    *,
    core_root: Path,
    path: Path | None,
    report_kind: str,
    source_commit: str,
    manifest_repo_path: str,
    manifest_sha256: str,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    if path is None:
        return "not-run", {"reason": f"{report_kind} result was not supplied."}, []
    if not path.is_file():
        return "failed", {"reason": f"Result does not exist: {path}"}, []
    try:
        payload = read_json(path)
    except (OSError, ValueError) as exc:
        return "failed", {"reason": str(exc)}, []
    errors: list[str] = []
    expected_type = "golden_replay" if report_kind == "replay" else "shadow"
    if payload.get("report_type") != expected_type:
        errors.append(f"report_type must be {expected_type}")
    if payload.get("status") != "PASS":
        errors.append("status must be PASS")
    expected_report_id = f"{report_kind}-golden-pilot-minimal-v1-{RELEASE_ID}"
    if payload.get("report_id") != expected_report_id:
        errors.append(f"report_id must be {expected_report_id}")
    if payload.get("fixture_id") != "golden-pilot-minimal-v1":
        errors.append("fixture_id must be golden-pilot-minimal-v1")
    if payload.get("session_id") != EXPECTED_SESSION_ID:
        errors.append(f"session_id must be {EXPECTED_SESSION_ID}")
    fixture_path = core_root / Path(*FIXTURE_RELATIVE_PATH.split("/"))
    fixture_sha256 = sha256_file(fixture_path)
    if payload.get("fixture_file_sha256") != fixture_sha256:
        errors.append("fixture_file_sha256 does not match the repository fixture")
    declared_digest = payload.get("report_digest")
    if not isinstance(declared_digest, str) or declared_digest != canonical_report_digest(payload):
        errors.append("report_digest does not match canonical report content")
    binding = payload.get("candidate_binding")
    if not isinstance(binding, dict):
        errors.append("candidate_binding is missing")
        binding = {}
    if binding.get("bundle_id") != BUNDLE_ID:
        errors.append("candidate bundle_id mismatch")
    if binding.get("release_id") != RELEASE_ID:
        errors.append("candidate release_id mismatch")
    if binding.get("source_commit") != source_commit:
        errors.append("candidate source_commit mismatch")
    if binding.get("runtime_manifest_sha256") != manifest_sha256:
        errors.append("runtime manifest hash mismatch")
    if binding.get("runtime_manifest_path") != manifest_repo_path:
        errors.append("runtime manifest path mismatch")
    for field, expected in (
        ("bundle_id", BUNDLE_ID),
        ("release_id", RELEASE_ID),
        ("source_commit", source_commit),
        ("runtime_manifest_path", manifest_repo_path),
        ("runtime_manifest_sha256", manifest_sha256),
    ):
        if payload.get(field) != expected:
            errors.append(f"top-level {field} mismatch")

    details: dict[str, Any] = {
        "report_id": payload.get("report_id"),
        "report_digest": payload.get("report_digest"),
        "runtime_manifest_sha256": binding.get("runtime_manifest_sha256"),
    }
    if report_kind == "replay":
        repetitions = payload.get("repeat_count")
        deterministic = payload.get("deterministic")
        runs = payload.get("runs")
        if deterministic is not True:
            errors.append("deterministic must be true")
        if type(repetitions) is not int or repetitions < 3:
            errors.append("repeat_count must be at least 3")
        if not isinstance(runs, list) or len(runs) != repetitions:
            errors.append("runs must match repeat_count")
            runs = []
        for digest_key in ("output_digest", "event_digest", "event_sequence_digest"):
            digests = {item.get(digest_key) for item in runs if isinstance(item, dict)}
            if len(digests) != 1 or None in digests:
                errors.append(f"{digest_key} differs across replay runs")
        if runs and any(
            item.get("run_index") != index
            or type(item.get("event_count")) is not int
            or item.get("event_count", 0) <= 0
            or type(item.get("planned_action_count")) is not int
            or item.get("planned_action_count", 0) <= 0
            for index, item in enumerate(runs)
            if isinstance(item, dict)
        ):
            errors.append("replay runs must have sequential indexes and non-empty events/actions")
        details.update(
            {
                "deterministic": deterministic,
                "fixture_digest": payload.get("fixture_digest"),
                "repeat_count": repetitions,
            }
        )
    else:
        audit = payload.get("input_audit")
        if not isinstance(audit, dict):
            errors.append("input_audit is missing")
            audit = {}
        real_calls = audit.get("core_v2_real_input_call_count")
        if real_calls != 0 or audit.get("real_input_call_count") != 0:
            errors.append("Core v2 real input call count must be zero")
        for counter in (
            "keyboard_call_count",
            "mouse_call_count",
            "receiver_call_count",
            "window_call_count",
            "double_write_event_count",
            "core_execution_event_count",
        ):
            if audit.get(counter) != 0:
                errors.append(f"{counter} must be zero")
        if audit.get("boundary_attempts") != []:
            errors.append("boundary_attempts must be empty")
        if audit.get("connected") is not False:
            errors.append("dry-run sink must be disconnected after Shadow")
        if audit.get("sink_type") != "DryRunInputSink":
            errors.append("Shadow must use the concrete DryRunInputSink")
        diffs = payload.get("diffs")
        if not isinstance(diffs, list):
            errors.append("diffs must be a list")
            diffs = []
        allowed_taxonomy = {"MATCH", "KIND_MISMATCH", "PLANNED_ONLY", "LEGACY_ONLY"}
        unclassified = [
            item
            for item in diffs
            if not isinstance(item, dict) or item.get("taxonomy") not in allowed_taxonomy
        ]
        if unclassified:
            errors.append("Shadow contains unclassified diffs")
        if not diffs:
            errors.append("Shadow must compare at least one planned/observed action")
        diff_summary = payload.get("diff_summary")
        if not isinstance(diff_summary, dict) or diff_summary.get("unclassified_diff_count") != 0:
            errors.append("Shadow diff summary must report zero unclassified diffs")
        details.update(
            {
                "diff_count": len(diffs),
                "fixture_digest": payload.get("fixture_digest"),
                "real_input_call_count": real_calls,
                "unclassified_diff_count": len(unclassified),
            }
        )
    status = "failed" if errors else "passed"
    if errors:
        details["errors"] = errors
    artifact = _portable_artifact_record(
        core_root,
        path,
        "evidence-report",
        f"{report_kind}-result",
    )
    return status, details, [artifact]


def _read_clean_result(
    *,
    core_root: Path,
    path: Path | None,
    source_commit: str,
    manifest_repo_path: str,
    manifest_sha256: str,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[Path]]:
    if path is None:
        return "not-run", {"reason": "clean-smoke result was not supplied."}, [], []
    if not path.is_file():
        return "failed", {"reason": f"Result does not exist: {path}"}, [], []
    try:
        payload = read_json(path)
    except (OSError, ValueError) as exc:
        return "failed", {"reason": str(exc)}, [], []
    errors: list[str] = []
    schema_errors = _schema_validation_messages(
        core_root=core_root,
        schema_name="evidence-report.schema.json",
        document=payload,
    )
    errors.extend(f"schema {message}" for message in schema_errors)
    if payload.get("report_kind") != "clean-smoke":
        errors.append("report_kind must be clean-smoke")
    if payload.get("status") != "passed":
        errors.append("status must be passed")
    if payload.get("bundle_id") != BUNDLE_ID or payload.get("release_id") != RELEASE_ID:
        errors.append("Candidate bundle/release binding mismatch")
    if payload.get("source_commit") != source_commit:
        errors.append("candidate source_commit mismatch")
    environment = payload.get("environment")
    if not isinstance(environment, dict) or environment.get("runner_os") != "Windows":
        errors.append("runner_os must be Windows")
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty list")
        checks = []
    failed_checks = [
        item.get("name")
        for item in checks
        if not isinstance(item, dict) or item.get("status") != "passed"
    ]
    if failed_checks:
        errors.append("all clean-smoke checks must pass")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        errors.append("summary is missing")
        summary = {}
    if summary.get("pip_cache") != "disabled" or summary.get("wheel_install") is not True:
        errors.append("cacheless wheel installation evidence is missing")
    if summary.get("runtime_manifest_path") != manifest_repo_path:
        errors.append("clean-smoke runtime manifest path mismatch")
    if summary.get("runtime_manifest_sha256") != manifest_sha256:
        errors.append("clean-smoke runtime manifest hash mismatch")

    required_checks = {
        "clean-checkout",
        "clean-build-cache-baseline",
        "candidate-source-lineage",
        "create-isolated-venv",
        "cacheless-lock-install",
        "dependency-lock-audit",
        "ruff-lint",
        "ruff-format",
        "mypy",
        "candidate-manifest-schema",
        "candidate-bundle-metadata",
        "pytest-coverage",
        "wheel-sdist-build",
        "normalize-sdist",
        "wheel-install",
        "installed-wheel-import",
        "golden-replay-three-runs",
        "shadow-zero-real-input",
        "rollback-stop-core-keep-legacy-owner",
    }
    observed_check_names = {
        item.get("name")
        for item in checks
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    missing_checks = sorted(required_checks - observed_check_names)
    if missing_checks:
        errors.append("clean-smoke missing required checks: " + ", ".join(missing_checks))

    nested_paths: list[Path] = []
    nested_artifacts = payload.get("artifacts")
    if not isinstance(nested_artifacts, list):
        errors.append("artifacts must be a list")
        nested_artifacts = []
    for item in nested_artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            errors.append("clean-smoke artifact has no portable path")
            continue
        try:
            artifact_path = _repo_path(core_root, Path(item["path"]))
            _repo_relative(core_root, artifact_path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not artifact_path.is_file():
            errors.append(f"clean-smoke artifact does not exist: {item['path']}")
            continue
        if sha256_file(artifact_path) != item.get("sha256"):
            errors.append(f"clean-smoke artifact hash mismatch: {item['path']}")
        if artifact_path.stat().st_size != item.get("size_bytes"):
            errors.append(f"clean-smoke artifact size mismatch: {item['path']}")
        nested_paths.append(artifact_path)

    artifact_kinds = {item.get("kind") for item in nested_artifacts if isinstance(item, dict)}
    for required_kind in ("junit", "coverage", "evidence-report", "wheel", "sdist"):
        if required_kind not in artifact_kinds:
            errors.append(f"clean-smoke missing {required_kind} artifact")

    details: dict[str, Any] = {
        "artifact_count": len(nested_paths),
        "checkout_commit": summary.get("checkout_commit"),
        "failed_checks": failed_checks,
        "report_id": payload.get("evidence_id"),
        "runner_os": environment.get("runner_os") if isinstance(environment, dict) else None,
    }
    if errors:
        details["errors"] = errors
    status = "failed" if errors else "passed"
    artifact = _portable_artifact_record(
        core_root,
        path,
        "evidence-report",
        "clean-result",
    )
    return status, details, [artifact], nested_paths


def _read_ci_result(
    *,
    core_root: Path,
    path: Path,
    candidate_source_commit: str,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    if not path.is_file():
        return "failed", {"reason": f"CI result does not exist: {path}"}, []
    try:
        payload = read_json(path)
    except (OSError, ValueError) as exc:
        return "failed", {"reason": str(exc)}, []
    errors = [
        f"schema {message}"
        for message in _schema_validation_messages(
            core_root=core_root,
            schema_name="ci-evidence.schema.json",
            document=payload,
        )
    ]
    for field, expected in (
        ("status", "passed"),
        ("bundle_id", BUNDLE_ID),
        ("release_id", RELEASE_ID),
        ("runner_os", "Windows"),
        ("dependency_install_result", "passed"),
    ):
        if payload.get(field) != expected:
            errors.append(f"CI {field} must be {expected}")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.isdigit() or int(run_id) <= 0:
        errors.append("CI run_id must identify a remote GitHub Actions run")
    if payload.get("event") not in {"push", "pull_request", "workflow_dispatch"}:
        errors.append("CI event must be a remote GitHub Actions event")
    checks = payload.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(not isinstance(item, dict) or item.get("status") != "passed" for item in checks)
    ):
        errors.append("every recorded CI check must be passed")

    checkout_commit = payload.get("source_commit")
    if not isinstance(checkout_commit, str) or len(checkout_commit) != 40:
        errors.append("CI source_commit must be a 40-character checkout commit")
    else:
        exists = subprocess.run(
            ["git", "-C", str(core_root), "cat-file", "-e", f"{checkout_commit}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        lineage = subprocess.run(
            [
                "git",
                "-C",
                str(core_root),
                "merge-base",
                "--is-ancestor",
                candidate_source_commit,
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
                str(core_root),
                "diff",
                "--name-only",
                f"{candidate_source_commit}..{checkout_commit}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        changed_paths = [line.strip().replace("\\", "/") for line in changed.stdout.splitlines()]
        unexpected = [
            item
            for item in changed_paths
            if item and not item.startswith(ALLOWED_PACKAGING_PREFIXES)
        ]
        if exists.returncode != 0 or lineage.returncode != 0 or changed.returncode != 0:
            errors.append("CI checkout commit is not a known descendant of candidate source")
        if unexpected:
            errors.append("CI checkout includes non-packaging changes: " + ", ".join(unexpected))

    details: dict[str, Any] = {
        "artifact_count": len(payload.get("artifacts", []))
        if isinstance(payload.get("artifacts"), list)
        else 0,
        "checkout_commit": checkout_commit,
        "run_attempt": payload.get("run_attempt"),
        "run_id": run_id,
        "run_url": (
            f"https://github.com/xphai/mxdauto/actions/runs/{run_id}"
            if isinstance(run_id, str) and run_id.isdigit()
            else None
        ),
        "workflow_name": payload.get("workflow_name"),
    }
    if errors:
        details["errors"] = errors
    artifact = _portable_artifact_record(core_root, path, "evidence-report", "ci-result")
    return "failed" if errors else "passed", details, [artifact]


def build_bundle(args: argparse.Namespace) -> Path:
    core_root = args.core_root.resolve()
    if not core_root.is_dir():
        raise ValueError(f"Core root does not exist: {core_root}")
    _validate_packaging_worktree(core_root)
    legacy_root = resolve_root("MAPLE_LEGACY_ROOT", args.legacy_root)
    model_root = resolve_root("MAPLE_MODEL_ROOT", args.model_root)
    upstream_root = resolve_root("MAPLE_UPSTREAM_ROOT", args.upstream_root)
    core_commit = _validated_source_commit(args.source_commit, core_root)
    upstream_commit = git_commit(upstream_root)
    generated_at = args.created_at
    junit_path = _repo_path(core_root, args.junit) if args.junit else None
    coverage_path = _repo_path(core_root, args.coverage) if args.coverage else None
    artifact_paths = [_repo_path(core_root, path) for path in args.artifact]

    output_dir = _repo_path(core_root, args.output)
    evidence_dir = _repo_path(core_root, args.evidence_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    roots = {
        "MAPLE_CORE_ROOT": core_root,
        "MAPLE_LEGACY_ROOT": legacy_root,
        "MAPLE_MODEL_ROOT": model_root,
    }
    asset_entries, assets_by_id = _load_asset_metadata(ASSET_SPECS, roots)
    asset_index = {
        "bundle_id": BUNDLE_ID,
        "entries": asset_entries,
        "generated_at": generated_at,
        "release_id": RELEASE_ID,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "source_commit": core_commit,
    }
    asset_index_path = output_dir / "asset-index.json"
    write_json(asset_index_path, asset_index)
    _validate_generated_document(
        core_root=core_root,
        schema_name="runtime-asset-index.schema.json",
        document=asset_index,
        label="asset index",
    )
    asset_index_sha256 = sha256_file(asset_index_path)

    evidence_index_repo_path = _repo_relative(core_root, evidence_dir / "evidence-index.json")
    manifest_repo_path = _repo_relative(core_root, output_dir / "runtime-manifest.json")
    asset_index_repo_path = _repo_relative(core_root, asset_index_path)
    manifest = {
        "asset_index_path": asset_index_repo_path,
        "asset_index_sha256": asset_index_sha256,
        "bundle_status": "candidate",
        "classes": ["mob"],
        "classes_sha256": assets_by_id[MANIFEST_BINDINGS["classes_sha256"]]["sha256"],
        "config_schema": "2.1.0",
        "created_at": generated_at,
        "dataset_version": "forest-calibration-v2",
        "effective_config_sha256": assets_by_id[MANIFEST_BINDINGS["effective_config_sha256"]][
            "sha256"
        ],
        "evidence_index_path": evidence_index_repo_path,
        "execution_mode": "shadow",
        "input_owner": "legacy",
        "input_size": {"height": 640, "width": 640},
        "key_bindings": {
            "attack": "a",
            "confirm": "space",
            "down": "down",
            "hp": "insert",
            "jump": "alt",
            "left": "left",
            "mp": "delete",
            "party": "p",
            "pickup": "z",
            "right": "right",
            "up": "up",
        },
        "local_urn": f"urn:maple:local:{BUNDLE_ID}:{core_commit}",
        "map_id": MAP_ID,
        "map_fingerprint": assets_by_id[MANIFEST_BINDINGS["map_fingerprint"]]["sha256"],
        "model_id": MODEL_ID,
        "model_sha256": assets_by_id[MANIFEST_BINDINGS["model_sha256"]]["sha256"],
        "movement_profile_sha256": assets_by_id[MANIFEST_BINDINGS["movement_profile_sha256"]][
            "sha256"
        ],
        "platform_graph_sha256": assets_by_id[MANIFEST_BINDINGS["platform_graph_sha256"]]["sha256"],
        "profile_id": PROFILE_ID,
        "profile_sha256": assets_by_id[MANIFEST_BINDINGS["profile_sha256"]]["sha256"],
        "receiver_hash": assets_by_id[MANIFEST_BINDINGS["receiver_hash"]]["sha256"],
        "receiver_version": "legacy-input-receiver-ps1-v1",
        "real_input_enabled": False,
        "release_id": RELEASE_ID,
        "replay_report_id": REPLAY_REPORT_ID,
        "route_manifest_sha256": assets_by_id[MANIFEST_BINDINGS["route_manifest_sha256"]]["sha256"],
        "runtime_manifest_version": RUNTIME_MANIFEST_VERSION,
        "source_commit": core_commit,
        "split_sha256": assets_by_id[MANIFEST_BINDINGS["split_sha256"]]["sha256"],
        "subject_id": SUBJECT_ID,
        "shadow_report_id": SHADOW_REPORT_ID,
        "clean_report_id": CLEAN_REPORT_ID,
        "test_report_id": TEST_REPORT_ID,
        "thresholds": {
            "action_timeout_ms": 1000,
            "detection_confidence": 0.25,
            "observation_stale_ms": 250,
            "recover_stale_ms": 150,
            "tracking_confidence": 0.65,
        },
        "upstream_commit": upstream_commit,
    }
    manifest_path = output_dir / "runtime-manifest.json"
    write_json(manifest_path, manifest)
    _validate_generated_document(
        core_root=core_root,
        schema_name="runtime-manifest.schema.json",
        document=manifest,
        label="runtime manifest",
    )
    manifest_sha256 = sha256_file(manifest_path)

    replay_result_path = (
        _repo_path(core_root, args.replay_result) if args.replay_result is not None else None
    )
    shadow_result_path = (
        _repo_path(core_root, args.shadow_result) if args.shadow_result is not None else None
    )
    clean_result_path = (
        _repo_path(core_root, args.clean_result) if args.clean_result is not None else None
    )
    ci_result_path = _repo_path(core_root, args.ci_result) if args.ci_result is not None else None

    junit_status, junit_details, junit_artifacts = _read_junit(junit_path)
    coverage_status, coverage_details, coverage_artifacts = _read_coverage(coverage_path)
    junit_artifacts = (
        [_portable_artifact_record(core_root, junit_path, "junit", "junit")]
        if junit_path is not None and junit_path.is_file()
        else []
    )
    coverage_artifacts = (
        [_portable_artifact_record(core_root, coverage_path, "coverage", "coverage")]
        if coverage_path is not None and coverage_path.is_file()
        else []
    )
    test_status = _status_from_checks([junit_status, coverage_status])
    if args.junit is None and args.coverage is None:
        test_status = "not-run"
    report_paths: list[Path] = []
    test_report = _report(
        report_kind="test",
        evidence_id=TEST_REPORT_ID,
        status=test_status,
        bundle_id=BUNDLE_ID,
        release_id=RELEASE_ID,
        source_commit=core_commit,
        generated_at=generated_at,
        checks=[
            {
                "command": "python -m pytest --junitxml=<junit.xml> --cov=maple_automation_core",
                "details": junit_details,
                "name": "pytest",
                "status": junit_status,
            },
            {
                "command": "python -m coverage xml",
                "details": coverage_details,
                "name": "coverage",
                "status": coverage_status,
            },
        ],
        artifacts=[*junit_artifacts, *coverage_artifacts],
        summary={"claim_scope": "local quality evidence only; no field or input-control claim."},
    )
    test_report_path = evidence_dir / "test-report.json"
    write_json(test_report_path, test_report)
    report_paths.append(test_report_path)

    replay_status, replay_details, replay_artifacts = _read_bound_result(
        core_root=core_root,
        path=replay_result_path,
        report_kind="replay",
        source_commit=core_commit,
        manifest_repo_path=manifest_repo_path,
        manifest_sha256=manifest_sha256,
    )
    replay_report = _report(
        report_kind="replay",
        evidence_id=REPLAY_REPORT_ID,
        status=replay_status,
        bundle_id=BUNDLE_ID,
        release_id=RELEASE_ID,
        source_commit=core_commit,
        generated_at=generated_at,
        checks=[
            {
                "command": (
                    "python tools/run_golden_replay.py --runs 3 --manifest "
                    "<runtime-manifest> --report <replay-result>"
                ),
                "details": replay_details,
                "name": "deterministic-replay",
                "status": replay_status,
            }
        ],
        artifacts=replay_artifacts,
        summary={"claim_scope": "Deterministic offline Replay only; no field claim."},
    )
    replay_report_path = evidence_dir / "replay-report.json"
    write_json(replay_report_path, replay_report)
    report_paths.append(replay_report_path)

    shadow_status, shadow_details, shadow_artifacts = _read_bound_result(
        core_root=core_root,
        path=shadow_result_path,
        report_kind="shadow",
        source_commit=core_commit,
        manifest_repo_path=manifest_repo_path,
        manifest_sha256=manifest_sha256,
    )
    shadow_report = _report(
        report_kind="shadow",
        evidence_id=SHADOW_REPORT_ID,
        status=shadow_status,
        bundle_id=BUNDLE_ID,
        release_id=RELEASE_ID,
        source_commit=core_commit,
        generated_at=generated_at,
        checks=[
            {
                "command": (
                    "python tools/run_shadow.py --manifest <runtime-manifest> "
                    "--report <shadow-result>"
                ),
                "details": shadow_details,
                "name": "shadow-runner",
                "status": shadow_status,
            }
        ],
        artifacts=shadow_artifacts,
        summary={
            "claim_scope": "Offline Shadow comparison only; no field claim.",
            "input_owner": "legacy",
            "real_input_enabled": False,
        },
    )
    shadow_report_path = evidence_dir / "shadow-report.json"
    write_json(shadow_report_path, shadow_report)
    report_paths.append(shadow_report_path)

    clean_status, clean_details, clean_artifacts, clean_nested_paths = _read_clean_result(
        core_root=core_root,
        path=clean_result_path,
        source_commit=core_commit,
        manifest_repo_path=manifest_repo_path,
        manifest_sha256=manifest_sha256,
    )
    clean_report = _report(
        report_kind="clean-smoke",
        evidence_id=CLEAN_REPORT_ID,
        status=clean_status,
        bundle_id=BUNDLE_ID,
        release_id=RELEASE_ID,
        source_commit=core_commit,
        generated_at=generated_at,
        checks=[
            {
                "command": "python tools/run_clean_smoke.py --output <clean-result>",
                "details": clean_details,
                "name": "cacheless-windows-clean-smoke",
                "status": clean_status,
            }
        ],
        artifacts=clean_artifacts,
        summary={"claim_scope": "Clean install/build/test/Replay/Shadow engineering smoke only."},
    )
    clean_report_path = evidence_dir / "clean-report.json"
    write_json(clean_report_path, clean_report)
    report_paths.append(clean_report_path)

    ci_report: dict[str, Any] | None = None
    if ci_result_path is not None:
        ci_status, ci_details, ci_artifacts = _read_ci_result(
            core_root=core_root,
            path=ci_result_path,
            candidate_source_commit=core_commit,
        )
        ci_report = _report(
            report_kind="ci",
            evidence_id=CI_REPORT_ID,
            status=ci_status,
            bundle_id=BUNDLE_ID,
            release_id=RELEASE_ID,
            source_commit=core_commit,
            generated_at=generated_at,
            checks=[
                {
                    "command": "validate downloaded GitHub Actions ci-evidence metadata",
                    "details": ci_details,
                    "name": "remote-ci-evidence-binding",
                    "status": ci_status,
                }
            ],
            artifacts=ci_artifacts,
            summary={
                "claim_scope": (
                    "Remote Windows CI metadata and content-addressed artifact inventory."
                )
            },
        )
        ci_report_path = evidence_dir / "ci-report.json"
        write_json(ci_report_path, ci_report)
        report_paths.append(ci_report_path)

    lock_path = _repo_path(core_root, args.lock)
    if not lock_path.is_file():
        raise ValueError(f"Dependency lock file does not exist: {lock_path}")
    locked_packages = parse_lock(lock_path)
    installed_errors = verify_installed(locked_packages) if args.dependency_check_installed else []
    installed_status = (
        "passed"
        if args.dependency_check_installed and not installed_errors
        else "failed"
        if args.dependency_check_installed
        else "not-run"
    )
    lock_artifact = _portable_artifact_record(core_root, lock_path, "dependency-lock", "lock")
    dependency_report_id = "dependency-report-g0-candidate-shadow-20260829"
    dependency_report = _report(
        report_kind="dependency",
        evidence_id=dependency_report_id,
        status=installed_status,
        bundle_id=BUNDLE_ID,
        release_id=RELEASE_ID,
        source_commit=core_commit,
        generated_at=generated_at,
        checks=[
            {
                "command": "parse every non-comment lock line as exact package==version",
                "details": {
                    "lock_sha256": lock_artifact["sha256"],
                    "package_count": len(locked_packages),
                },
                "name": "dependency-lock-syntax",
                "status": "passed",
            },
            {
                "command": (
                    "python tools/verify_dependency_lock.py --lock "
                    "configs/requirements.lock --check-installed"
                ),
                "details": {"errors": installed_errors},
                "name": "dependency-lock-installed-versions",
                "status": installed_status,
            },
        ],
        artifacts=[lock_artifact],
    )
    dependency_report_path = evidence_dir / "dependency-report.json"
    write_json(dependency_report_path, dependency_report)
    report_paths.append(dependency_report_path)

    build_artifacts = [
        _portable_artifact_record(core_root, path, _artifact_kind(path), "build")
        for path in artifact_paths
        if path.is_file()
    ]
    build_report_id = "build-report-g0-candidate-shadow-20260829"
    build_kinds = {artifact["kind"] for artifact in build_artifacts}
    clean_artifact_paths = {path.resolve() for path in clean_nested_paths}
    build_paths = {path.resolve() for path in artifact_paths if path.is_file()}
    build_bound_to_clean = bool(build_paths) and build_paths.issubset(clean_artifact_paths)
    build_status = (
        "passed"
        if clean_status == "passed"
        and {"wheel", "sdist"}.issubset(build_kinds)
        and build_bound_to_clean
        else "failed"
        if build_artifacts
        else "not-run"
    )
    build_report = _report(
        report_kind="build",
        evidence_id=build_report_id,
        status=build_status,
        bundle_id=BUNDLE_ID,
        release_id=RELEASE_ID,
        source_commit=core_commit,
        generated_at=generated_at,
        checks=[
            {
                "command": "python -m build --wheel --sdist --no-isolation",
                "details": {
                    "artifact_count": len(build_artifacts),
                    "artifact_kinds": sorted(build_kinds),
                    "bound_to_clean_smoke": build_bound_to_clean,
                },
                "name": "package-build",
                "status": build_status,
            }
        ],
        artifacts=build_artifacts,
    )
    build_report_path = evidence_dir / "build-report.json"
    write_json(build_report_path, build_report)
    report_paths.append(build_report_path)

    manifest_report_id = "manifest-report-g0-candidate-shadow-20260829"
    manifest_report = _report(
        report_kind="manifest",
        evidence_id=manifest_report_id,
        status="passed",
        bundle_id=BUNDLE_ID,
        release_id=RELEASE_ID,
        source_commit=core_commit,
        generated_at=generated_at,
        checks=[
            {
                "command": (
                    "python tools/validate_runtime_manifest.py --schema "
                    "schemas/runtime-manifest.schema.json <manifest>"
                ),
                "details": {"manifest_sha256": manifest_sha256},
                "name": "runtime-manifest-schema",
                "status": "passed",
            }
        ],
        artifacts=[_portable_artifact_record(core_root, manifest_path, "manifest", "manifest")],
    )
    manifest_report_path = evidence_dir / "manifest-report.json"
    write_json(manifest_report_path, manifest_report)
    report_paths.append(manifest_report_path)

    hash_report_id = "hash-report-g0-candidate-shadow-20260829"
    hash_report = _report(
        report_kind="hash",
        evidence_id=hash_report_id,
        status="passed",
        bundle_id=BUNDLE_ID,
        release_id=RELEASE_ID,
        source_commit=core_commit,
        generated_at=generated_at,
        checks=[
            {
                "command": "stream every configured asset byte through SHA-256 during generation",
                "details": {
                    "asset_count": len(asset_entries),
                    "external_asset_count": sum(
                        entry["source"]["root_env"] != "MAPLE_CORE_ROOT" for entry in asset_entries
                    ),
                    "measurement_stage": "generation",
                },
                "name": "asset-byte-sha256-measurement",
                "status": "passed",
            }
        ],
        summary={
            "claim_scope": (
                "Hashes were measured from the configured external roots at generation time."
            )
        },
    )
    hash_report_path = evidence_dir / "hash-report.json"
    write_json(hash_report_path, hash_report)
    report_paths.append(hash_report_path)

    evidence_entries: list[dict[str, Any]] = []
    reports = [
        test_report,
        replay_report,
        shadow_report,
        clean_report,
    ]
    if ci_report is not None:
        reports.append(ci_report)
    reports.extend(
        [
            dependency_report,
            build_report,
            manifest_report,
            hash_report,
        ]
    )
    for report in reports:
        _validate_generated_document(
            core_root=core_root,
            schema_name="evidence-report.schema.json",
            document=report,
            label=f"{report['report_kind']} evidence report",
        )
    for report_path, report in zip(report_paths, reports, strict=True):
        size_bytes, digest, _ = file_metadata(report_path)
        evidence_entries.append(
            {
                "claims": [
                    f"report_status:{report['status']}",
                    f"report_kind:{report['report_kind']}",
                ],
                "evidence_id": report["evidence_id"],
                "kind": report["report_kind"],
                "path": _repo_relative(core_root, report_path),
                "sha256": digest,
                "size_bytes": size_bytes,
                "status": report["status"],
            }
        )
    for path, kind, evidence_id in (
        (manifest_path, "manifest", "manifest-artifact-g0-candidate-shadow-20260829"),
        (asset_index_path, "asset-index", "asset-index-g0-candidate-shadow-20260829"),
        (lock_path, "dependency", "dependency-lock-g0-candidate-shadow-20260829"),
    ):
        size_bytes, digest, _ = file_metadata(path)
        evidence_entries.append(
            {
                "claims": [f"sha256:{digest}"],
                "evidence_id": evidence_id,
                "kind": kind,
                "path": _repo_relative(core_root, path),
                "sha256": digest,
                "size_bytes": size_bytes,
                "status": "passed",
            }
        )
    for artifact in [*junit_artifacts, *coverage_artifacts, *build_artifacts]:
        path = core_root / Path(*artifact["path"].split("/"))
        if not path.is_file():
            continue
        evidence_entries.append(
            {
                "claims": [f"artifact_kind:{artifact['kind']}"],
                "evidence_id": artifact["artifact_id"],
                "kind": artifact["kind"],
                "path": artifact["path"],
                "sha256": artifact["sha256"],
                "size_bytes": artifact["size_bytes"],
                "status": "passed",
            }
        )
    evidence_index = {
        "bundle_id": BUNDLE_ID,
        "entries": evidence_entries,
        "evidence_index_id": EVIDENCE_INDEX_ID,
        "execution_mode": "shadow",
        "generated_at": generated_at,
        "input_policy": {"owner": "legacy", "real_input_enabled": False},
        "lifecycle": "candidate",
        "release_id": RELEASE_ID,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "source_commit": core_commit,
        "subject_id": SUBJECT_ID,
    }
    evidence_index_path = evidence_dir / "evidence-index.json"
    write_json(evidence_index_path, evidence_index)
    _validate_generated_document(
        core_root=core_root,
        schema_name="evidence-index.schema.json",
        document=evidence_index,
        label="evidence index",
    )
    evidence_index_sha256 = sha256_file(evidence_index_path)

    local_files = [
        manifest_path,
        asset_index_path,
        evidence_index_path,
        *report_paths,
        lock_path,
        core_root / "configs" / "pilot-subject-01.profile.json",
        core_root / "configs" / "pilot-subject-01.resolved.json",
        *([junit_path] if junit_path is not None and junit_path.is_file() else []),
        *([coverage_path] if coverage_path is not None and coverage_path.is_file() else []),
        *(
            [replay_result_path]
            if replay_result_path is not None and replay_result_path.is_file()
            else []
        ),
        *(
            [shadow_result_path]
            if shadow_result_path is not None and shadow_result_path.is_file()
            else []
        ),
        *(
            [clean_result_path]
            if clean_result_path is not None and clean_result_path.is_file()
            else []
        ),
        *([ci_result_path] if ci_result_path is not None and ci_result_path.is_file() else []),
        *clean_nested_paths,
        *[path for path in artifact_paths if path.is_file()],
    ]
    local_files = list(dict.fromkeys(path.resolve() for path in local_files))
    local_file_records = []
    for path in sorted(local_files, key=lambda item: _repo_relative(core_root, item)):
        size_bytes, digest, _ = file_metadata(path)
        local_file_records.append(
            {
                "path": _repo_relative(core_root, path),
                "sha256": digest,
                "size_bytes": size_bytes,
            }
        )
    bundle_evidence = {
        "index_id": EVIDENCE_INDEX_ID,
        "replay_report_id": REPLAY_REPORT_ID,
        "shadow_report_id": SHADOW_REPORT_ID,
        "clean_report_id": CLEAN_REPORT_ID,
        "test_report_id": TEST_REPORT_ID,
    }
    if ci_report is not None:
        bundle_evidence["ci_report_id"] = CI_REPORT_ID
    bundle = {
        "asset_index_path": asset_index_repo_path,
        "asset_index_sha256": asset_index_sha256,
        "bundle_id": BUNDLE_ID,
        "evidence": bundle_evidence,
        "evidence_index_path": evidence_index_repo_path,
        "evidence_index_sha256": evidence_index_sha256,
        "execution_mode": "shadow",
        "generated_at": generated_at,
        "generator": {"tool": "build_candidate_bundle.py", "version": TOOL_VERSION},
        "input_policy": {
            "owner": "legacy",
            "real_input_enabled": False,
            "reason": "G0 Candidate/Shadow bundle; Core v2 never owns live input.",
        },
        "lifecycle": "candidate",
        "local_files": local_file_records,
        "manifest_asset_bindings": MANIFEST_BINDINGS,
        "manifest_path": manifest_repo_path,
        "manifest_sha256": manifest_sha256,
        "release_id": RELEASE_ID,
        "rollback": {
            "action": "stop-core-v2-and-keep-legacy-input",
            "target": "legacy-input-owner",
        },
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "source": {
            "core_commit": core_commit,
            "core_repository": "local:maple-automation-core",
            "upstream_commit": upstream_commit,
            "upstream_repository": "git:upstream/MapleStoryAutoLevelUp",
        },
        "subject_id": SUBJECT_ID,
    }
    bundle_path = output_dir / "bundle.json"
    _validate_generated_document(
        core_root=core_root,
        schema_name="runtime-bundle.schema.json",
        document=bundle,
        label="runtime bundle",
    )
    write_json(bundle_path, bundle)
    checksum_path = output_dir / "checksums.sha256"
    _write_checksums(core_root, [*local_files, bundle_path], checksum_path)
    print(f"Generated Candidate/Shadow bundle: {_repo_relative(core_root, bundle_path)}")
    print(f"Manifest SHA-256: {manifest_sha256}")
    print(f"Asset index SHA-256: {asset_index_sha256}")
    print(f"Evidence index SHA-256: {evidence_index_sha256}")
    return bundle_path


def _artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".whl":
        return "wheel"
    if suffix in {".gz", ".bz2", ".xz", ".zip"} or path.name.endswith(".tar.gz"):
        return "sdist"
    return "other"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        build_bundle(args)
    except (OSError, ValueError, ElementTree.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
