from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover - dependencies are installed in CI
    Draft202012Validator = None  # type: ignore[assignment,misc]
    FormatChecker = None  # type: ignore[assignment,misc]

try:
    from .bundle_common import (
        file_metadata,
        git_commit,
        isoformat_utc,
        read_json,
        safe_relative_path,
        write_json,
    )
except ImportError:  # pragma: no cover - exercised when invoked as a script
    from bundle_common import (  # type: ignore[import-not-found,no-redef]
        file_metadata,
        git_commit,
        isoformat_utc,
        read_json,
        safe_relative_path,
        write_json,
    )


SCHEMA_VERSION = "1.0.0"
REPORT_SCHEMA_NAME = "evidence-report.schema.json"
DEFAULT_FIXTURE = Path("fixtures") / "golden" / "pilot_minimal_v1.json"

# GitHub exposes these values as ``steps.<id>.outcome``.  The evidence schema
# deliberately keeps its smaller passed/failed/skipped vocabulary, while the
# original value is retained in check.details for auditability.
CHECK_OUTCOME_STATUS = {
    "success": "passed",
    "failure": "failed",
    "cancelled": "skipped",
    "skipped": "skipped",
    "neutral": "skipped",
    "timed_out": "failed",
    "action_required": "failed",
    "stale": "failed",
}
WORKFLOW_RESULTS = {"success", "failure", "cancelled"}

# Producers intentionally add binding/provenance fields to the generic
# evidence-report shape.  The base schema is still applied to every report;
# this list lets the collector validate both the base shape and the producer
# extension without weakening checks for unknown fields.
REPORT_EXTENSION_FIELDS = {
    "report_type",
    "report_version",
    "report_id",
    "report_digest",
    "bundle_digest",
    "deterministic",
    "fixture_id",
    "fixture_digest",
    "fixture_file_sha256",
    "output_digest",
    "repeat_count",
    "runs",
    "session_id",
    "runtime_manifest_path",
    "runtime_manifest_sha256",
    "candidate_binding",
    "fixture_bundle",
    "input_audit",
    "diff_summary",
    "diffs",
    "legacy_observed_actions",
    "planned_actions",
    "replay_output_digest",
    # CI-envelope fields are not part of evidence-report.schema.json but may
    # be copied into a producer report by local tooling.
    "workflow_name",
    "event",
    "run_id",
    "run_attempt",
    "dependency_install_result",
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect portable CI JUnit, coverage, lock and build evidence metadata."
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output JSON path, relative to --repo-root.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root (default: repository containing this tool).",
    )
    parser.add_argument("--junit", type=Path, help="JUnit XML path.")
    parser.add_argument("--coverage", type=Path, help="Coverage XML path.")
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        type=Path,
        help="Build artifact path; repeat for each wheel/sdist or other artifact.",
    )
    parser.add_argument(
        "--evidence-report",
        action="append",
        default=[],
        type=Path,
        help="Replay, Shadow or clean-smoke JSON report; repeatable.",
    )
    parser.add_argument(
        "--fixture",
        action="append",
        default=[],
        type=Path,
        help="Fixture JSON used by evidence reports; repeatable (defaults to the golden fixture).",
    )
    parser.add_argument(
        "--check-result",
        action="append",
        default=[],
        help="GitHub step result encoded as NAME::OUTCOME::COMMAND; repeatable.",
    )
    # Keep the old option for local callers.  New workflow invocations use
    # --check-result so a failed/skipped step cannot be mistaken for success.
    parser.add_argument(
        "--passed-check",
        action="append",
        default=[],
        help="Legacy completed check encoded as NAME::COMMAND; repeatable.",
    )
    parser.add_argument("--lock", type=Path, help="Exact dependency lock path.")
    parser.add_argument("--manifest", type=Path, help="Runtime manifest path.")
    parser.add_argument("--asset-index", type=Path, help="Runtime asset index path.")
    parser.add_argument("--evidence-index", type=Path, help="Evidence index path.")
    parser.add_argument("--bundle", type=Path, help="Bundle descriptor path.")
    parser.add_argument("--bundle-id", help="Bundle identifier to bind to this evidence.")
    parser.add_argument("--release-id", help="Release identifier to bind to this evidence.")
    parser.add_argument("--source-commit", help="40-character source commit override.")
    parser.add_argument(
        "--event",
        choices=("pull_request", "push", "workflow_dispatch", "local"),
        help="CI event (defaults to GITHUB_EVENT_NAME or local).",
    )
    parser.add_argument("--workflow-name", help="Workflow name override.")
    parser.add_argument("--run-id", help="Run identifier override.")
    parser.add_argument("--run-attempt", help="Run attempt override.")
    parser.add_argument("--timestamp", help="UTC timestamp for deterministic evidence output.")
    parser.add_argument(
        "--started-at",
        help="UTC start timestamp captured before the workflow quality steps.",
    )
    parser.add_argument(
        "--workflow-result",
        choices=tuple(sorted(WORKFLOW_RESULTS)),
        help="GitHub job result (success, failure or cancelled).",
    )
    parser.add_argument(
        "--dependency-install-result",
        choices=("passed", "failed"),
        default="passed",
        help="Legacy result of the lock installation step (default: passed).",
    )
    return parser.parse_args(argv)


def _repo_path(repo_root: Path, value: Path | None) -> Path | None:
    if value is None:
        return None
    candidate = value if value.is_absolute() else repo_root / value
    resolved = candidate.resolve()
    if not resolved.is_relative_to(repo_root.resolve()):
        raise ValueError(f"Path must be inside repository root: {value}")
    return resolved


def _portable_path(repo_root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"Evidence artifact must be inside repository root: {path}") from exc
    return safe_relative_path(relative.as_posix())


def _check_file(
    repo_root: Path,
    path: Path | None,
    name: str,
    command: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if path is None:
        return (
            {
                "command": command,
                "details": {"reason": f"{name} was not supplied."},
                "name": name,
                "status": "skipped",
            },
            None,
        )
    if not path.is_file():
        return (
            {
                "command": command,
                "details": {"path": str(path), "reason": "file does not exist"},
                "name": name,
                "status": "failed",
            },
            None,
        )
    try:
        size_bytes, digest, _ = file_metadata(path)
        portable = _portable_path(repo_root, path)
    except (OSError, ValueError) as exc:
        return (
            {
                "command": command,
                "details": {"path": str(path), "reason": str(exc)},
                "name": name,
                "status": "failed",
            },
            None,
        )
    artifact = {
        "artifact_id": f"{name}-{path.stem.lower().replace('_', '-')}",
        "kind": name,
        "name": path.name,
        "path": portable,
        "sha256": digest,
        "size_bytes": size_bytes,
    }
    return (
        {
            "command": command,
            "details": {"path": artifact["path"], "sha256": digest, "size_bytes": size_bytes},
            "name": name,
            "status": "passed",
        },
        artifact,
    )


def _read_junit(repo_root: Path, path: Path | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    check, artifact = _check_file(
        repo_root,
        path,
        "junit",
        "python -m pytest --junitxml=evidence/ci/junit.xml",
    )
    if artifact is None or path is None or check["status"] != "passed":
        return check, artifact
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        check["status"] = "failed"
        check["details"] = {"reason": f"invalid JUnit XML: {exc}"}
        return check, None
    suites = [root] if root.tag.endswith("testsuite") else list(root.findall(".//testsuite"))
    totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for key in totals:
            try:
                totals[key] += int(suite.attrib.get(key, "0"))
            except (TypeError, ValueError):
                check["status"] = "failed"
                check["details"] = {"reason": f"invalid JUnit count for {key}"}
                return check, None
    if totals["failures"] or totals["errors"]:
        check["status"] = "failed"
    check["details"] = {**check["details"], **totals}
    return check, artifact


def _read_coverage(
    repo_root: Path,
    path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    check, artifact = _check_file(
        repo_root,
        path,
        "coverage",
        "python -m pytest --cov=maple_automation_core --cov-report=xml:evidence/ci/coverage.xml",
    )
    if artifact is None or path is None or check["status"] != "passed":
        return check, artifact
    try:
        root = ElementTree.parse(path).getroot()
        line_rate = float(root.attrib.get("line-rate", "0"))
        lines_covered = int(root.attrib.get("lines-covered", "0"))
        lines_valid = int(root.attrib.get("lines-valid", "0"))
    except (ElementTree.ParseError, OSError, TypeError, ValueError) as exc:
        check["status"] = "failed"
        check["details"] = {"reason": f"invalid coverage XML: {exc}"}
        return check, None
    if not 0 <= line_rate <= 1 or lines_covered < 0 or lines_valid < 0:
        check["status"] = "failed"
        check["details"] = {"reason": "coverage counts/rate are outside valid ranges"}
        return check, None
    check["details"] = {
        **check["details"],
        "line_percent": round(line_rate * 100, 2),
        "line_rate": line_rate,
        "lines_covered": lines_covered,
        "lines_valid": lines_valid,
    }
    return check, artifact


def _artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".whl":
        return "wheel"
    if suffix in {".gz", ".bz2", ".xz", ".zip"} or path.name.endswith(".tar.gz"):
        return "sdist"
    return "other"


def _read_build_artifacts(
    repo_root: Path,
    paths: list[Path],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    artifacts: list[dict[str, Any]] = []
    missing: list[str] = []
    errors: list[str] = []
    for path in paths:
        if not path.is_file():
            missing.append(str(path))
            continue
        try:
            size_bytes, digest, _ = file_metadata(path)
            portable = _portable_path(repo_root, path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        artifacts.append(
            {
                "artifact_id": f"build-{path.stem.lower().replace('_', '-')}",
                "kind": _artifact_kind(path),
                "name": path.name,
                "path": portable,
                "sha256": digest,
                "size_bytes": size_bytes,
            }
        )
    status = "failed" if missing or errors else ("passed" if artifacts else "skipped")
    details: dict[str, Any] = {"artifact_count": len(artifacts)}
    if missing:
        details["missing"] = missing
    if errors:
        details["errors"] = errors
    return (
        {
            "command": "python -m build --wheel --sdist --no-isolation",
            "details": details,
            "name": "build-artifacts",
            "status": status,
        },
        artifacts,
    )


def _canonical_report_digest(payload: dict[str, Any]) -> str:
    canonical_payload = dict(payload)
    canonical_payload.pop("report_digest", None)
    encoded = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _report_kind(payload: dict[str, Any], path: Path, index: int) -> str:
    value = payload.get("report_kind")
    if isinstance(value, str):
        return value
    report_type = payload.get("report_type")
    if report_type == "golden_replay":
        return "replay"
    if report_type == "shadow":
        return "shadow"
    stem = path.stem.lower()
    if "replay" in stem:
        return "replay"
    if "shadow" in stem:
        return "shadow"
    if "clean" in stem:
        return "clean-smoke"
    return ("replay", "shadow", "clean-smoke")[index] if index < 3 else "evidence"


def _schema_projection(payload: dict[str, Any], report_kind: str) -> dict[str, Any]:
    """Project producer extensions onto the generic evidence-report schema.

    Replay/Shadow producers predate the generic schema and use ``report_type``
    plus a richer payload.  The projection validates the common contract while
    dedicated checks below validate every binding-specific invariant.
    """

    projection = dict(payload)
    if "evidence_id" not in projection and isinstance(projection.get("report_id"), str):
        projection["evidence_id"] = projection["report_id"]
    if "report_kind" not in projection:
        projection["report_kind"] = report_kind
    if projection.get("status") == "PASS":
        projection["status"] = "passed"
    elif projection.get("status") == "FAIL":
        projection["status"] = "failed"
    if "environment" not in projection:
        projection["environment"] = {
            "python_version": platform.python_version(),
            "runner_os": platform.system(),
            "working_directory_policy": "repository-relative",
        }
    if "checks" not in projection:
        mapped_status = CHECK_OUTCOME_STATUS.get(str(projection.get("status")), "failed")
        projection["checks"] = [
            {
                "command": f"validate {report_kind} evidence report",
                "name": report_kind,
                "status": mapped_status,
            }
        ]
    if "artifacts" not in projection:
        projection["artifacts"] = []
    projection.setdefault("schema_version", SCHEMA_VERSION)
    projection.setdefault("execution_mode", "shadow")
    projection.setdefault("subject_id", "subject-ci")
    projection.setdefault("generated_at", isoformat_utc())
    if not isinstance(projection.get("source_commit"), str):
        projection["source_commit"] = "0" * 40
    if not isinstance(projection.get("bundle_id"), str):
        projection["bundle_id"] = "candidate-ci"
    if not isinstance(projection.get("release_id"), str):
        projection["release_id"] = projection["bundle_id"]
    projection.setdefault("workflow_name", "evidence-report")
    projection.setdefault("event", "local")
    projection.setdefault("run_id", "0")
    projection.setdefault("run_attempt", "0")
    projection.setdefault("dependency_install_result", "passed")
    for field in REPORT_EXTENSION_FIELDS:
        projection.pop(field, None)
    projection.pop("report_type", None)
    return projection


def _validate_report_schema(
    payload: dict[str, Any],
    report_kind: str,
    schema: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    if schema is None:
        return False, [f"{REPORT_SCHEMA_NAME} is not available"]
    if Draft202012Validator is None or FormatChecker is None:
        return False, ["jsonschema is not installed"]
    try:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
    except Exception as exc:  # pragma: no cover - malformed schema is deployment failure
        return False, [f"invalid {REPORT_SCHEMA_NAME}: {exc}"]
    errors = sorted(validator.iter_errors(payload), key=lambda error: tuple(error.path))
    if not errors:
        return True, []
    # Allow the known producer extension only after validating the normalized
    # base document.  Unknown extensions remain a schema failure.
    schema_properties = set(schema.get("properties", {}))
    unknown = sorted(set(payload) - schema_properties - REPORT_EXTENSION_FIELDS)
    projection = _schema_projection(payload, report_kind)
    projected_errors = sorted(
        validator.iter_errors(projection), key=lambda error: tuple(error.path)
    )
    if not unknown and not projected_errors:
        return True, []
    messages = [error.message for error in errors]
    messages.extend(f"unknown property: {name}" for name in unknown)
    messages.extend(f"normalized report: {error.message}" for error in projected_errors)
    return False, messages


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return read_json(path), None
    except (OSError, ValueError) as exc:
        return None, str(exc)


def _validate_nested_artifacts(
    repo_root: Path,
    payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    nested = payload.get("artifacts")
    if nested is None:
        return errors
    if not isinstance(nested, list):
        return ["artifacts must be a list"]
    for item in nested:
        if not isinstance(item, dict):
            errors.append("nested artifact must be an object")
            continue
        raw_path = item.get("path")
        declared_sha = item.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(declared_sha, str):
            errors.append("nested artifact requires path and sha256")
            continue
        try:
            path = _repo_path(repo_root, Path(raw_path))
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if path is None or not path.is_file():
            errors.append(f"nested artifact does not exist: {raw_path}")
            continue
        try:
            _, actual_sha, _ = file_metadata(path)
        except OSError as exc:
            errors.append(f"nested artifact cannot be read: {raw_path}: {exc}")
            continue
        if actual_sha != declared_sha:
            errors.append(f"nested artifact sha256 mismatch: {raw_path}")
    return errors


def _fixture_candidates(
    repo_root: Path,
    values: list[Path],
) -> list[tuple[Path, str]]:
    raw_values = values or [DEFAULT_FIXTURE]
    candidates: list[tuple[Path, str]] = []
    for value in raw_values:
        path = _repo_path(repo_root, value)
        if path is None or not path.is_file():
            continue
        try:
            _, digest, _ = file_metadata(path)
        except OSError:
            continue
        candidates.append((path, digest))
    return candidates


def _validate_replay_invariants(payload: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    deterministic = payload.get("deterministic")
    repeat_count = payload.get("repeat_count")
    runs = payload.get("runs")
    if deterministic is not True:
        errors.append("deterministic must be true")
    if type(repeat_count) is not int or repeat_count < 3:
        errors.append("repeat_count must be at least 3")
    if not isinstance(runs, list) or (type(repeat_count) is int and len(runs) != repeat_count):
        errors.append("runs must match repeat_count")
        runs = []
    for digest_key in ("output_digest", "event_digest", "event_sequence_digest"):
        digests = {item.get(digest_key) for item in runs if isinstance(item, dict)}
        if len(digests) != 1 or None in digests:
            errors.append(f"{digest_key} differs across replay runs")
    for index, item in enumerate(runs):
        if not isinstance(item, dict):
            errors.append("replay run must be an object")
            continue
        if item.get("run_index") != index:
            errors.append("replay runs must have sequential indexes")
        for count_key in ("event_count", "planned_action_count"):
            if type(item.get(count_key)) is not int or item.get(count_key, 0) <= 0:
                errors.append(f"replay {count_key} must be a positive integer")
    return {
        "deterministic": deterministic,
        "fixture_digest": payload.get("fixture_digest"),
        "repeat_count": repeat_count,
    }


def _validate_shadow_invariants(payload: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    audit = payload.get("input_audit")
    if not isinstance(audit, dict):
        errors.append("input_audit is missing")
        audit = {}
    counters = (
        "core_v2_real_input_call_count",
        "real_input_call_count",
        "keyboard_call_count",
        "mouse_call_count",
        "receiver_call_count",
        "window_call_count",
        "double_write_event_count",
        "core_execution_event_count",
    )
    for counter in counters:
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
    return {
        "diff_count": len(diffs),
        "fixture_digest": payload.get("fixture_digest"),
        "real_input_call_count": audit.get("core_v2_real_input_call_count"),
        "unclassified_diff_count": len(unclassified),
    }


def _validate_clean_invariants(payload: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    environment = payload.get("environment")
    if not isinstance(environment, dict) or environment.get("runner_os") != "Windows":
        errors.append("runner_os must be Windows")
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty list")
        checks = []
    failed_checks = [
        item.get("name") if isinstance(item, dict) else None
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
    if summary.get("project_venv_reused") is not False:
        errors.append("clean smoke must not reuse a project virtual environment")
    return {
        "checkout_commit": summary.get("checkout_commit"),
        "failed_checks": failed_checks,
        "runner_os": environment.get("runner_os") if isinstance(environment, dict) else None,
    }


def _validate_evidence_report(
    *,
    repo_root: Path,
    path: Path,
    index: int,
    schema: dict[str, Any] | None,
    expected_bundle_id: str | None,
    expected_release_id: str | None,
    expected_source_commit: str,
    manifest_repo_path: str | None,
    manifest_sha256: str | None,
    fixture_candidates: list[tuple[Path, str]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    report_kind_hint = _report_kind({}, path, index)
    check, artifact = _check_file(
        repo_root,
        path,
        "evidence-report",
        f"validate machine-readable evidence report {path.name}",
    )
    check["details"] = {**check["details"], "expected_report_kind": report_kind_hint}
    if check["status"] != "passed":
        return check, artifact
    payload, read_error = _read_json_object(path)
    if payload is None:
        check["status"] = "failed"
        check["details"] = {**check["details"], "reason": read_error or "invalid JSON"}
        return check, artifact

    report_kind = _report_kind(payload, path, index)
    errors: list[str] = []
    schema_valid, schema_errors = _validate_report_schema(payload, report_kind, schema)
    if not schema_valid:
        errors.extend(f"schema: {message}" for message in schema_errors)

    expected_report_type = {"replay": "golden_replay", "shadow": "shadow"}.get(report_kind)
    if expected_report_type and (
        "report_type" in payload and payload.get("report_type") != expected_report_type
    ):
        errors.append(f"report_type must be {expected_report_type}")
    if report_kind not in {"replay", "shadow", "clean-smoke"}:
        errors.append("report_kind must be replay, shadow or clean-smoke")

    status = payload.get("status")
    expected_status = "passed" if report_kind == "clean-smoke" else "PASS"
    if status not in {expected_status, "passed" if expected_status == "PASS" else expected_status}:
        errors.append(f"status must be {expected_status}")

    for field, expected in (
        ("bundle_id", expected_bundle_id),
        ("release_id", expected_release_id),
        ("source_commit", expected_source_commit),
    ):
        if expected is not None and (
            not _same_commit(payload.get(field), expected)
            if field == "source_commit"
            else payload.get(field) != expected
        ):
            errors.append(f"candidate {field} mismatch")

    report_summary = payload.get("summary")
    summary_manifest_sha = (
        report_summary.get("runtime_manifest_sha256") if isinstance(report_summary, dict) else None
    )
    summary_manifest_path = (
        report_summary.get("runtime_manifest_path") if isinstance(report_summary, dict) else None
    )
    declared_manifest_sha = payload.get("runtime_manifest_sha256") or summary_manifest_sha
    declared_manifest_path = payload.get("runtime_manifest_path") or summary_manifest_path
    if manifest_sha256 is not None:
        if declared_manifest_sha != manifest_sha256:
            errors.append("runtime manifest hash mismatch")
    else:
        errors.append("candidate runtime manifest hash is unavailable")
    if manifest_repo_path is not None:
        if declared_manifest_path != manifest_repo_path:
            errors.append("runtime manifest path mismatch")
    else:
        errors.append("candidate runtime manifest path is unavailable")

    binding = payload.get("candidate_binding")
    if not isinstance(binding, dict):
        if report_kind in {"replay", "shadow"}:
            errors.append("candidate_binding is missing")
            binding = {}
        else:
            binding = {
                "bundle_id": payload.get("bundle_id"),
                "release_id": payload.get("release_id"),
                "source_commit": payload.get("source_commit"),
                "runtime_manifest_path": declared_manifest_path,
                "runtime_manifest_sha256": declared_manifest_sha,
            }
    for field, expected in (
        ("bundle_id", expected_bundle_id),
        ("release_id", expected_release_id),
        ("source_commit", expected_source_commit),
        ("runtime_manifest_path", manifest_repo_path),
        ("runtime_manifest_sha256", manifest_sha256),
    ):
        if expected is not None and (
            not _same_commit(binding.get(field), expected)
            if field == "source_commit"
            else binding.get(field) != expected
        ):
            errors.append(f"candidate_binding {field} mismatch")

    fixture_sha = payload.get("fixture_file_sha256")
    if report_kind in {"replay", "shadow"}:
        if not fixture_candidates:
            errors.append("fixture file is missing")
        else:
            _, expected_fixture_sha = fixture_candidates[0]
            if fixture_sha != expected_fixture_sha:
                errors.append("fixture_file_sha256 does not match the repository fixture")
            # ``fixture_digest`` is the canonical digest of the decoded fixture
            # object, whereas ``fixture_file_sha256`` is the byte-level attestation
            # we can recompute here.  They are intentionally distinct digests.
    fixture_bundle = payload.get("fixture_bundle")
    if fixture_bundle is not None and (
        not isinstance(fixture_bundle, dict)
        or not isinstance(fixture_bundle.get("bundle_id"), str)
        or not isinstance(fixture_bundle.get("bundle_digest"), str)
    ):
        errors.append("fixture_bundle binding is malformed")

    declared_digest = payload.get("report_digest")
    computed_digest: str | None = None
    if report_kind in {"replay", "shadow"}:
        try:
            computed_digest = _canonical_report_digest(payload)
        except (TypeError, ValueError):
            computed_digest = None
        if not isinstance(declared_digest, str) or computed_digest is None:
            errors.append("report_digest is missing")
        elif declared_digest != computed_digest:
            errors.append("report_digest does not match canonical report content")

    if report_kind == "replay":
        invariant_details = _validate_replay_invariants(payload, errors)
    elif report_kind == "shadow":
        invariant_details = _validate_shadow_invariants(payload, errors)
    else:
        invariant_details = _validate_clean_invariants(payload, errors)
    errors.extend(_validate_nested_artifacts(repo_root, payload))

    check["status"] = "failed" if errors else "passed"
    check["details"] = {
        **check["details"],
        "candidate_binding": {
            "bundle_id": payload.get("bundle_id"),
            "release_id": payload.get("release_id"),
            "source_commit": payload.get("source_commit"),
            "runtime_manifest_path": declared_manifest_path,
            "runtime_manifest_sha256": declared_manifest_sha,
        },
        "computed_report_digest": computed_digest,
        "fixture_file_sha256": fixture_sha,
        "expected_fixture_file_sha256": (
            fixture_candidates[0][1]
            if report_kind in {"replay", "shadow"} and fixture_candidates
            else None
        ),
        "report_id": payload.get("report_id", payload.get("evidence_id")),
        "report_kind": report_kind,
        "report_status": status,
        "report_digest": declared_digest,
        "report_digest_valid": (
            isinstance(declared_digest, str)
            and computed_digest is not None
            and declared_digest == computed_digest
            if report_kind in {"replay", "shadow"}
            else None
        ),
        "schema_errors": schema_errors,
        "schema_valid": schema_valid,
        **invariant_details,
    }
    if errors:
        check["details"]["errors"] = errors
    return check, artifact


def _metadata_file(
    repo_root: Path,
    path: Path | None,
    name: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    check, artifact = _check_file(repo_root, path, name, f"verify {name} metadata")
    if check["status"] != "passed" or path is None:
        return check, artifact, None
    payload, read_error = _read_json_object(path)
    if payload is None:
        check["status"] = "failed"
        check["details"] = {**check["details"], "reason": read_error or "invalid JSON"}
        return check, artifact, None
    check["details"] = {**check["details"], "json_object": True}
    return check, artifact, payload


def _resolve_source_commit(
    repo_root: Path,
    requested: str | None,
    manifest: dict[str, Any] | None,
) -> tuple[str, str | None]:
    if requested is not None:
        if len(requested) == 40 and all(
            character in "0123456789abcdefABCDEF" for character in requested
        ):
            return requested.lower(), None
        return "0" * 40, "Expected a 40-character source commit"
    manifest_commit = manifest.get("source_commit") if manifest else None
    if (
        isinstance(manifest_commit, str)
        and len(manifest_commit) == 40
        and all(character in "0123456789abcdefABCDEF" for character in manifest_commit)
    ):
        return manifest_commit.lower(), None
    try:
        return git_commit(repo_root).lower(), None
    except ValueError as exc:
        return "0" * 40, str(exc)


def _is_dependency_install_check(name: str) -> bool:
    normalized = name.lower().replace("_", "-")
    return "install" in normalized and (
        "depend" in normalized or normalized in {"install", "install-dependencies"}
    )


def _same_commit(left: object, right: object) -> bool:
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()


def _parse_check_results(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str | None]:
    checks: list[dict[str, Any]] = []
    for encoded in getattr(args, "check_result", []) or []:
        parts = encoded.split("::", 2)
        if len(parts) != 3:
            return [], "--check-result must use NAME::OUTCOME::COMMAND syntax"
        name, outcome, command = (part.strip() for part in parts)
        if not name or not outcome or not command:
            return [], "--check-result requires a non-empty name, outcome and command"
        outcome = outcome.lower()
        status = CHECK_OUTCOME_STATUS.get(outcome)
        if status is None:
            return [], f"unsupported GitHub step outcome: {outcome}"
        checks.append(
            {
                "command": command,
                "details": {"github_step_outcome": outcome, "outcome": outcome},
                "name": name,
                "status": status,
            }
        )
    for encoded in getattr(args, "passed_check", []) or []:
        if "::" not in encoded:
            return [], "--passed-check must use NAME::COMMAND syntax"
        name, command = encoded.split("::", 1)
        if not name.strip() or not command.strip():
            return [], "--passed-check requires a non-empty name and command"
        checks.append(
            {
                "command": command.strip(),
                "details": {
                    "github_step_outcome": "success",
                    "legacy_passed_check": True,
                    "outcome": "success",
                },
                "name": name.strip(),
                "status": "passed",
            }
        )
    return checks, None


def collect_evidence(args: argparse.Namespace) -> Path:
    repo_root = args.repo_root.resolve()
    if not repo_root.is_dir():
        raise ValueError(f"Repository root does not exist: {repo_root}")
    output_path = _repo_path(repo_root, args.output)
    if output_path is None:
        raise ValueError("--output is required")
    junit_path = _repo_path(repo_root, args.junit)
    coverage_path = _repo_path(repo_root, args.coverage)
    lock_path = _repo_path(repo_root, args.lock)
    artifact_paths = [path for value in args.artifact if (path := _repo_path(repo_root, value))]
    evidence_report_paths = [
        path for value in args.evidence_report if (path := _repo_path(repo_root, value))
    ]
    fixture_paths = [
        path for value in getattr(args, "fixture", []) if (path := _repo_path(repo_root, value))
    ]

    checks: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    parsed_checks, parse_error = _parse_check_results(args)
    if parse_error:
        checks.append(
            {
                "command": "collect CI step outcomes",
                "details": {"reason": parse_error},
                "name": "ci-step-outcomes",
                "status": "failed",
            }
        )
    else:
        checks.extend(parsed_checks)

    junit_check, junit_artifact = _read_junit(repo_root, junit_path)
    checks.append(junit_check)
    if junit_artifact:
        artifacts.append(junit_artifact)
    coverage_check, coverage_artifact = _read_coverage(repo_root, coverage_path)
    checks.append(coverage_check)
    if coverage_artifact:
        artifacts.append(coverage_artifact)
    lock_check, lock_artifact = _check_file(
        repo_root,
        lock_path,
        "dependency-lock",
        "python -m pip install --requirement configs/requirements.lock",
    )
    checks.append(lock_check)
    if lock_artifact:
        artifacts.append(lock_artifact)
    build_check, build_artifacts = _read_build_artifacts(repo_root, artifact_paths)
    checks.append(build_check)
    artifacts.extend(build_artifacts)

    manifest_path = _repo_path(repo_root, args.manifest)
    asset_index_path = _repo_path(repo_root, args.asset_index)
    evidence_index_path = _repo_path(repo_root, args.evidence_index)
    bundle_path = _repo_path(repo_root, args.bundle)
    metadata: dict[str, dict[str, Any] | None] = {}
    metadata_paths = (
        ("manifest", manifest_path),
        ("asset-index", asset_index_path),
        ("evidence-index", evidence_index_path),
        ("bundle", bundle_path),
    )
    metadata_artifacts: dict[str, dict[str, Any] | None] = {}
    for name, path in metadata_paths:
        metadata_check, metadata_artifact, payload = _metadata_file(repo_root, path, name)
        checks.append(metadata_check)
        metadata[name] = payload
        metadata_artifacts[name] = metadata_artifact
        if metadata_artifact:
            artifacts.append(metadata_artifact)

    manifest = metadata.get("manifest")
    bundle = metadata.get("bundle")
    manifest_artifact = metadata_artifacts.get("manifest")
    manifest_sha256 = manifest_artifact.get("sha256") if manifest_artifact else None
    manifest_repo_path = _portable_path(repo_root, manifest_path) if manifest_path else None
    expected_bundle_id = (
        args.bundle_id
        or (bundle.get("bundle_id") if isinstance(bundle, dict) else None)
        or (manifest.get("release_id") if isinstance(manifest, dict) else None)
    )
    expected_release_id = (
        args.release_id
        or (bundle.get("release_id") if isinstance(bundle, dict) else None)
        or (manifest.get("release_id") if isinstance(manifest, dict) else None)
    )
    source_commit, source_error = _resolve_source_commit(
        repo_root,
        args.source_commit,
        manifest if isinstance(manifest, dict) else None,
    )

    binding_errors: list[str] = []
    if source_error:
        binding_errors.append(f"source commit: {source_error}")
    if isinstance(manifest, dict):
        if expected_release_id is not None and manifest.get("release_id") != expected_release_id:
            binding_errors.append("manifest release_id does not match candidate release")
        if args.bundle_id is not None and manifest.get("release_id") != args.bundle_id:
            binding_errors.append("manifest release_id does not match candidate bundle_id")
        if not _same_commit(manifest.get("source_commit"), source_commit):
            binding_errors.append("manifest source_commit does not match candidate source")
        if asset_index_path is not None:
            if manifest.get("asset_index_path") != _portable_path(repo_root, asset_index_path):
                binding_errors.append("manifest asset_index_path mismatch")
            asset_artifact = metadata_artifacts.get("asset-index")
            asset_sha = asset_artifact.get("sha256") if asset_artifact else None
            if asset_sha is not None and manifest.get("asset_index_sha256") != asset_sha:
                binding_errors.append("manifest asset_index_sha256 mismatch")
    if isinstance(bundle, dict):
        source = bundle.get("source")
        if expected_bundle_id is not None and bundle.get("bundle_id") != expected_bundle_id:
            binding_errors.append("bundle bundle_id mismatch")
        if expected_release_id is not None and bundle.get("release_id") != expected_release_id:
            binding_errors.append("bundle release_id mismatch")
        if isinstance(source, dict) and not _same_commit(source.get("core_commit"), source_commit):
            binding_errors.append("bundle source.core_commit mismatch")
        if manifest_path is not None and bundle.get("manifest_path") != manifest_repo_path:
            binding_errors.append("bundle manifest_path mismatch")
        if manifest_sha256 is not None and bundle.get("manifest_sha256") != manifest_sha256:
            binding_errors.append("bundle manifest_sha256 mismatch")
        asset_artifact = metadata_artifacts.get("asset-index")
        asset_sha = asset_artifact.get("sha256") if asset_artifact else None
        if asset_sha is not None and bundle.get("asset_index_sha256") != asset_sha:
            binding_errors.append("bundle asset_index_sha256 mismatch")
        evidence_artifact = metadata_artifacts.get("evidence-index")
        evidence_sha = evidence_artifact.get("sha256") if evidence_artifact else None
        if evidence_sha is not None and bundle.get("evidence_index_sha256") != evidence_sha:
            binding_errors.append("bundle evidence_index_sha256 mismatch")
    for name in ("asset-index", "evidence-index"):
        item = metadata.get(name)
        if isinstance(item, dict):
            if expected_bundle_id is not None and item.get("bundle_id") != expected_bundle_id:
                binding_errors.append(f"{name} bundle_id mismatch")
            if expected_release_id is not None and item.get("release_id") != expected_release_id:
                binding_errors.append(f"{name} release_id mismatch")
            if not _same_commit(item.get("source_commit"), source_commit):
                binding_errors.append(f"{name} source_commit mismatch")
    binding_inputs_supplied = any(path is not None for _, path in metadata_paths) or bool(
        evidence_report_paths
    )
    binding_check: dict[str, Any] = {
        "command": "cross-check candidate manifest, bundle and evidence bindings",
        "details": {
            "bundle_id": expected_bundle_id,
            "manifest_path": manifest_repo_path,
            "manifest_sha256": manifest_sha256,
            "release_id": expected_release_id,
            "source_commit": source_commit,
        },
        "name": "candidate-binding",
        "status": (
            "failed" if binding_errors else ("passed" if binding_inputs_supplied else "skipped")
        ),
    }
    if binding_errors:
        binding_check["details"]["errors"] = binding_errors
    checks.append(binding_check)

    schema_path = repo_root / "schemas" / REPORT_SCHEMA_NAME
    schema, schema_error = (
        _read_json_object(schema_path)
        if schema_path.is_file()
        else (None, "schema file does not exist")
    )
    schema_check: dict[str, Any] = {
        "command": f"validate evidence reports against schemas/{REPORT_SCHEMA_NAME}",
        "details": {
            "path": _portable_path(repo_root, schema_path)
            if schema_path.is_file()
            else str(schema_path)
        },
        "name": "evidence-report-schema",
        "status": (
            "passed" if schema is not None else ("failed" if evidence_report_paths else "skipped")
        ),
    }
    if schema_error:
        schema_check["details"]["reason"] = schema_error
    checks.append(schema_check)

    try:
        fixture_candidates = _fixture_candidates(repo_root, fixture_paths)
    except ValueError as exc:
        fixture_candidates = []
        checks.append(
            {
                "command": "resolve evidence fixture",
                "details": {"reason": str(exc)},
                "name": "evidence-fixture",
                "status": "failed",
            }
        )
    report_fixture_check: dict[str, Any] = {
        "command": "resolve Replay/Shadow/Clean fixture SHA",
        "details": {
            "fixture_count": len(fixture_candidates),
            "sha256": fixture_candidates[0][1] if fixture_candidates else None,
        },
        "name": "evidence-fixture",
        "status": (
            "passed" if fixture_candidates else ("failed" if evidence_report_paths else "skipped")
        ),
    }
    checks.append(report_fixture_check)

    for index, path in enumerate(evidence_report_paths):
        report_check, report_artifact = _validate_evidence_report(
            repo_root=repo_root,
            path=path,
            index=index,
            schema=schema,
            expected_bundle_id=expected_bundle_id,
            expected_release_id=expected_release_id,
            expected_source_commit=source_commit,
            manifest_repo_path=manifest_repo_path,
            manifest_sha256=manifest_sha256,
            fixture_candidates=fixture_candidates,
        )
        checks.append(report_check)
        if report_artifact:
            artifacts.append(report_artifact)

    workflow_result = (
        getattr(args, "workflow_result", None)
        or os.environ.get("GITHUB_JOB_STATUS")
        or os.environ.get("GITHUB_WORKFLOW_RESULT")
        or "success"
    ).lower()
    if workflow_result not in WORKFLOW_RESULTS:
        workflow_result = "failure"
        checks.append(
            {
                "command": "record GitHub job status",
                "details": {"reason": "unsupported workflow result"},
                "name": "workflow-result",
                "status": "failed",
            }
        )
    else:
        checks.append(
            {
                "command": "GitHub Actions job status",
                "details": {"outcome": workflow_result, "github_job_status": workflow_result},
                "name": "workflow-result",
                "status": CHECK_OUTCOME_STATUS[workflow_result],
            }
        )
    statuses = [check["status"] for check in checks]
    dependency_result = getattr(args, "dependency_install_result", "passed")
    parsed_dependency = next(
        (
            check["status"]
            for check in checks
            if _is_dependency_install_check(str(check.get("name", "")))
        ),
        None,
    )
    if parsed_dependency is not None:
        dependency_result = "passed" if parsed_dependency == "passed" else "failed"
    if workflow_result == "cancelled":
        status = "cancelled"
    elif (
        workflow_result == "failure"
        or "failed" in statuses
        or any(check["status"] == "failed" for check in checks)
    ):
        status = "failed"
    else:
        status = "passed"
    now = args.timestamp or isoformat_utc()
    started_at = args.started_at or now
    event = args.event or os.environ.get("GITHUB_EVENT_NAME", "local")
    if event not in {"pull_request", "push", "workflow_dispatch", "local"}:
        event = "local"
    source_commit = source_commit.lower()
    run_id = args.run_id or os.environ.get("GITHUB_RUN_ID", "0")
    run_attempt = args.run_attempt or os.environ.get("GITHUB_RUN_ATTEMPT", "0")
    if not run_id.isdigit():
        run_id = "0"
    if not run_attempt.isdigit():
        run_attempt = "0"
    evidence_id = f"ci-evidence-{event}-{run_id}-{run_attempt}"
    payload: dict[str, Any] = {
        "artifacts": artifacts,
        "completed_at": now,
        "dependency_install_result": dependency_result,
        "event": event,
        "evidence_id": evidence_id,
        "generated_at": now,
        "python_version": platform.python_version(),
        "run_attempt": run_attempt,
        "run_id": run_id,
        "runner_os": platform.system(),
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "started_at": started_at,
        "status": status,
        "workflow_name": args.workflow_name or os.environ.get("GITHUB_WORKFLOW", "local-quality"),
    }
    payload["checks"] = checks
    if expected_bundle_id:
        payload["bundle_id"] = expected_bundle_id
    if expected_release_id:
        payload["release_id"] = expected_release_id
    write_json(output_path, payload)
    return output_path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        output_path = collect_evidence(args)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"CI evidence written: {output_path}")
    payload = read_json(output_path)
    if payload.get("status") != "passed":
        print(f"CI evidence status: {payload.get('status', 'missing')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
