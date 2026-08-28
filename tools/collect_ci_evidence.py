from __future__ import annotations

import argparse
import os
import platform
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

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
        "--passed-check",
        action="append",
        default=[],
        help="Previously completed check encoded as NAME::COMMAND; repeatable.",
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
        "--dependency-install-result",
        choices=("passed", "failed"),
        default="passed",
        help="Result of the lock installation step (default: passed).",
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
    size_bytes, digest, _ = file_metadata(path)
    artifact = {
        "artifact_id": f"{name}-{path.stem.lower().replace('_', '-')}",
        "kind": name,
        "name": path.name,
        "path": _portable_path(repo_root, path),
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
    except ElementTree.ParseError as exc:
        check["status"] = "failed"
        check["details"] = {"reason": f"invalid JUnit XML: {exc}"}
        return check, None
    suites = [root] if root.tag.endswith("testsuite") else list(root.findall(".//testsuite"))
    totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for key in totals:
            try:
                totals[key] += int(suite.attrib.get(key, "0"))
            except ValueError:
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
    except (ElementTree.ParseError, TypeError, ValueError) as exc:
        check["status"] = "failed"
        check["details"] = {"reason": f"invalid coverage XML: {exc}"}
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
    for path in paths:
        if not path.is_file():
            missing.append(str(path))
            continue
        size_bytes, digest, _ = file_metadata(path)
        artifacts.append(
            {
                "artifact_id": f"build-{path.stem.lower().replace('_', '-')}",
                "kind": _artifact_kind(path),
                "name": path.name,
                "path": _portable_path(repo_root, path),
                "sha256": digest,
                "size_bytes": size_bytes,
            }
        )
    status = "failed" if missing else ("passed" if artifacts else "skipped")
    details: dict[str, Any] = {"artifact_count": len(artifacts)}
    if missing:
        details["missing"] = missing
    return (
        {
            "command": "python -m build --wheel --sdist --no-isolation",
            "details": details,
            "name": "build-artifacts",
            "status": status,
        },
        artifacts,
    )


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

    checks: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    for encoded in args.passed_check:
        if "::" not in encoded:
            raise ValueError("--passed-check must use NAME::COMMAND syntax")
        name, command = encoded.split("::", 1)
        if not name.strip() or not command.strip():
            raise ValueError("--passed-check requires a non-empty name and command")
        checks.append(
            {
                "command": command.strip(),
                "details": {"recorded_after_successful_workflow_step": True},
                "name": name.strip(),
                "status": "passed",
            }
        )
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

    for path, name, kind in (
        (_repo_path(repo_root, args.manifest), "manifest", "manifest"),
        (_repo_path(repo_root, args.asset_index), "asset-index", "asset-index"),
        (_repo_path(repo_root, args.evidence_index), "evidence-index", "evidence-index"),
        (_repo_path(repo_root, args.bundle), "bundle", "bundle"),
    ):
        if path is None:
            continue
        check, artifact = _check_file(repo_root, path, name, f"verify {kind} metadata")
        checks.append(check)
        if artifact:
            artifacts.append(artifact)

    for path in evidence_report_paths:
        check, artifact = _check_file(
            repo_root,
            path,
            "evidence-report",
            f"validate machine-readable evidence report {path.name}",
        )
        if check["status"] == "passed":
            try:
                payload = read_json(path)
            except (OSError, ValueError) as exc:
                check["status"] = "failed"
                check["details"] = {"reason": str(exc)}
                artifact = None
            else:
                report_status = payload.get("status")
                if report_status not in {"PASS", "passed"}:
                    check["status"] = "failed"
                check["details"] = {
                    **check["details"],
                    "report_id": payload.get("report_id", payload.get("evidence_id")),
                    "report_kind": payload.get("report_type", payload.get("report_kind")),
                    "report_status": report_status,
                    "source_commit": payload.get("source_commit"),
                }
        checks.append(check)
        if artifact:
            artifacts.append(artifact)

    statuses = [check["status"] for check in checks]
    status = (
        "failed" if args.dependency_install_result == "failed" or "failed" in statuses else "passed"
    )
    now = args.timestamp or isoformat_utc()
    started_at = args.started_at or now
    event = args.event or os.environ.get("GITHUB_EVENT_NAME", "local")
    if event not in {"pull_request", "push", "workflow_dispatch", "local"}:
        event = "local"
    source_commit = args.source_commit or git_commit(repo_root)
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
        "dependency_install_result": args.dependency_install_result,
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
    if args.bundle_id:
        payload["bundle_id"] = args.bundle_id
    if args.release_id:
        payload["release_id"] = args.release_id
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
