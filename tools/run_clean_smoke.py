"""Run a cacheless Windows smoke for the G0 seal or the current checkout."""

from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from .bundle_common import (
        file_metadata,
        git_commit,
        read_json,
        safe_relative_path,
        sha256_file,
        write_json,
    )
except ImportError:  # pragma: no cover - exercised when invoked as a script
    from bundle_common import (  # type: ignore[import-not-found,no-redef]
        file_metadata,
        git_commit,
        read_json,
        safe_relative_path,
        sha256_file,
        write_json,
    )


SCHEMA_VERSION = "1.0.0"
STEP_TIMEOUT_SECONDS = 900
_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/][^\r\n\"'<>]+|\\\\[^\\/\s\"'<>]+[\\/][^\r\n\"'<>]+)"
)


class SmokeFailure(RuntimeError):
    pass


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=root)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evidence") / "clean-smoke" / "clean-smoke-report.json",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("bundles") / "candidate-core-v2-20260829-shadow" / "runtime-manifest.json",
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=Path("bundles") / "candidate-core-v2-20260829-shadow",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("fixtures") / "golden" / "pilot_minimal_v1.json",
    )
    parser.add_argument(
        "--mode",
        choices=("g0-seal", "checkout-regression"),
        default="g0-seal",
        help=(
            "g0-seal preserves the packaging-only G0 lineage contract; "
            "checkout-regression builds and tests the current checkout without "
            "rebinding the sealed G0 Candidate."
        ),
    )
    return parser.parse_args(argv)


def _inside(root: Path, value: Path) -> Path:
    path = (value if value.is_absolute() else root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Path must stay inside repository root: {value}")
    return path


def _portable(root: Path, path: Path) -> str:
    return safe_relative_path(path.resolve().relative_to(root.resolve()).as_posix())


def _replace_root(value: str, root: Path | None, replacement: str) -> str:
    if root is None:
        return value
    resolved = str(root.resolve()).rstrip("\\/")
    spellings = {resolved, resolved.replace("\\", "/")}
    result = value
    for spelling in sorted(spellings, key=len, reverse=True):
        result = re.sub(re.escape(spelling), replacement, result, flags=re.IGNORECASE)
    return result


def _sanitize_text(
    value: str,
    *,
    repo_root: Path | None = None,
    temp_root: Path | None = None,
) -> str:
    """Replace runner paths with portable labels before evidence is persisted."""

    result = _replace_root(value, repo_root, ".")
    result = _replace_root(result, temp_root, "[temp]")
    return _ABSOLUTE_PATH.sub("[absolute-path]", result)


def _sanitize_command(
    command: list[str], *, repo_root: Path | None = None, temp_root: Path | None = None
) -> str:
    return _sanitize_text(
        subprocess.list2cmdline(command),
        repo_root=repo_root,
        temp_root=temp_root,
    )


def _sanitize_text_file(path: Path, *, repo_root: Path, temp_root: Path | None = None) -> None:
    """Rewrite a text evidence file without changing its structural format."""

    value = path.read_text(encoding="utf-8", errors="replace")
    sanitized = _sanitize_text(value, repo_root=repo_root, temp_root=temp_root)
    if sanitized != value:
        path.write_text(sanitized, encoding="utf-8")


def _absolute_path_findings(paths: list[Path]) -> dict[str, int]:
    findings: dict[str, int] = {}
    for path in paths:
        if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl", ".xml", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            findings[path.name] = 1
            continue
        matches = list(_ABSOLUTE_PATH.finditer(text))
        if matches:
            findings[path.name] = len(matches)
    return findings


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_step(
    *,
    checks: list[dict[str, Any]],
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    repo_root: Path | None = None,
    temp_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=STEP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        duration = round(time.perf_counter() - started, 3)
        checks.append(
            {
                "command": _sanitize_command(command, repo_root=repo_root, temp_root=temp_root),
                "details": {"timeout_seconds": STEP_TIMEOUT_SECONDS},
                "duration_seconds": duration,
                "name": name,
                "status": "failed",
            }
        )
        raise SmokeFailure(f"{name} exceeded {STEP_TIMEOUT_SECONDS} seconds") from exc
    duration = round(time.perf_counter() - started, 3)
    details: dict[str, Any] = {
        "exit_code": completed.returncode,
        "stdout_tail": _sanitize_text(
            completed.stdout[-4000:], repo_root=repo_root, temp_root=temp_root
        ),
        "stderr_tail": _sanitize_text(
            completed.stderr[-4000:], repo_root=repo_root, temp_root=temp_root
        ),
    }
    checks.append(
        {
            "command": _sanitize_command(command, repo_root=repo_root, temp_root=temp_root),
            "details": details,
            "duration_seconds": duration,
            "name": name,
            "status": "passed" if completed.returncode == 0 else "failed",
        }
    )
    if completed.returncode != 0:
        raise SmokeFailure(f"{name} exited with {completed.returncode}")
    return completed


def _artifact(root: Path, path: Path, kind: str, artifact_id: str) -> dict[str, Any]:
    size_bytes, digest, _ = file_metadata(path)
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "name": path.name,
        "path": _portable(root, path),
        "sha256": digest,
        "size_bytes": size_bytes,
    }


def _lineage_is_allowed(
    mode: str,
    *,
    ancestor_ok: bool,
    unexpected_paths: list[str],
) -> bool:
    if mode == "g0-seal":
        return ancestor_ok and not unexpected_paths
    if mode == "checkout-regression":
        return ancestor_ok
    raise ValueError(f"Unsupported clean-smoke mode: {mode}")


def run_clean_smoke(args: argparse.Namespace) -> tuple[Path, bool]:
    repo_root = args.repo_root.resolve()
    if not repo_root.is_dir():
        raise ValueError(f"Repository root does not exist: {repo_root}")
    output = _inside(repo_root, args.output)
    manifest_path = _inside(repo_root, args.manifest)
    bundle_dir = _inside(repo_root, args.bundle_dir)
    fixture_path = _inside(repo_root, args.fixture)
    manifest = read_json(manifest_path)
    manifest_sha256 = sha256_file(manifest_path)
    source_commit = manifest.get("source_commit")
    release_id = manifest.get("release_id")
    subject_id = manifest.get("subject_id")
    if not isinstance(source_commit, str) or len(source_commit) != 40:
        raise ValueError("Runtime manifest source_commit must be a 40-character hash.")
    if not isinstance(release_id, str) or not release_id:
        raise ValueError("Runtime manifest release_id is missing.")
    if not isinstance(subject_id, str) or not subject_id:
        raise ValueError("Runtime manifest subject_id is missing.")

    mode = getattr(args, "mode", "g0-seal")
    if mode not in {"g0-seal", "checkout-regression"}:
        raise ValueError(f"Unsupported clean-smoke mode: {mode}")
    checkout_commit = git_commit(repo_root)
    checks: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    error: str | None = None
    output_dir = output.parent
    junit_path = output_dir / "clean-junit.xml"
    coverage_path = output_dir / "clean-coverage.xml"
    replay_path = output_dir / "clean-replay-report.json"
    shadow_path = output_dir / "clean-shadow-report.json"
    frame_admission_path = output_dir / "clean-frame-admission-report.json"
    xml_errors: list[str] = []

    def sanitize_xml_evidence(temp_root: Path | None = None) -> None:
        for xml_path in (junit_path, coverage_path):
            if not xml_path.is_file():
                continue
            try:
                _sanitize_text_file(
                    xml_path,
                    repo_root=repo_root,
                    temp_root=temp_root,
                )
            except OSError:
                if xml_path.name not in xml_errors:
                    xml_errors.append(xml_path.name)

    # This check runs before the report directory is created, proving the checkout
    # itself is clean.  Generated evidence is written only after this point.
    clean = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    clean_details = {
        "exit_code": clean.returncode,
        "porcelain": clean.stdout,
    }
    clean_ok = clean.returncode == 0 and clean.stdout.strip() == ""
    checks.append(
        {
            "command": "git status --porcelain=v1 --untracked-files=all",
            "details": clean_details,
            "duration_seconds": 0.0,
            "name": "clean-checkout",
            "status": "passed" if clean_ok else "failed",
        }
    )
    forbidden_cache_paths = [
        repo_root / "build",
        repo_root / "dist",
        repo_root / ".venv",
        repo_root / "venv",
        *sorted((repo_root / "src").glob("*.egg-info")),
    ]
    existing_cache_paths = [
        _portable(repo_root, path) for path in forbidden_cache_paths if path.exists()
    ]
    cache_baseline_ok = not existing_cache_paths
    checks.append(
        {
            "command": "inspect build/dist/project-venv/egg-info baseline",
            "details": {"existing_paths": existing_cache_paths},
            "duration_seconds": 0.0,
            "name": "clean-build-cache-baseline",
            "status": "passed" if cache_baseline_ok else "failed",
        }
    )
    lineage = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_commit, checkout_commit],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    changed = subprocess.run(
        ["git", "diff", "--name-only", f"{source_commit}..{checkout_commit}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    changed_paths = [line.strip().replace("\\", "/") for line in changed.stdout.splitlines()]
    allowed_packaging_prefixes = ("bundles/", "evidence/", "docs/")
    unexpected_paths = [
        path for path in changed_paths if path and not path.startswith(allowed_packaging_prefixes)
    ]
    ancestor_ok = lineage.returncode == 0 and changed.returncode == 0
    packaging_only = not unexpected_paths
    lineage_ok = _lineage_is_allowed(
        mode,
        ancestor_ok=ancestor_ok,
        unexpected_paths=unexpected_paths,
    )
    checks.append(
        {
            "command": (
                "git merge-base --is-ancestor <manifest-source> HEAD; "
                "git diff --name-only <manifest-source>..HEAD"
            ),
            "details": {
                "allowed_packaging_prefixes": list(allowed_packaging_prefixes),
                "changed_paths": changed_paths,
                "checkout_commit": checkout_commit,
                "lineage_policy": mode,
                "manifest_source_commit": source_commit,
                "packaging_only": packaging_only,
                "unexpected_paths": unexpected_paths,
            },
            "duration_seconds": 0.0,
            "name": "candidate-source-lineage",
            "status": "passed" if lineage_ok else "failed",
        }
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    epoch_commit = source_commit if mode == "g0-seal" else checkout_commit
    source_epoch = subprocess.run(
        ["git", "show", "-s", "--format=%ct", epoch_commit],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    source_date_epoch = source_epoch.stdout.strip()
    if source_epoch.returncode != 0 or not source_date_epoch.isdigit():
        raise ValueError("Could not derive SOURCE_DATE_EPOCH from the tested commit.")

    env = os.environ.copy()
    for inherited in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(inherited, None)
    env.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "SOURCE_DATE_EPOCH": source_date_epoch,
        }
    )
    try:
        if not clean_ok:
            raise SmokeFailure("checkout contains uncommitted or untracked files")
        if not cache_baseline_ok:
            raise SmokeFailure("checkout contains reusable build or project-environment state")
        if not lineage_ok:
            if mode == "g0-seal":
                raise SmokeFailure(
                    "checkout is not a packaging/docs-only descendant of source_commit"
                )
            raise SmokeFailure("sealed G0 source is not an ancestor of the checkout")
        with tempfile.TemporaryDirectory(prefix=f"maple-core-{mode}-") as temp_name:
            temp_root = Path(temp_name).resolve()
            venv_dir = temp_root / "venv"
            artifact_dir = output_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)

            _run_step(
                checks=checks,
                name="create-isolated-venv",
                command=[sys.executable, "-m", "venv", str(venv_dir)],
                cwd=temp_root,
                env=env,
                repo_root=repo_root,
                temp_root=temp_root,
            )
            venv_python = (
                venv_dir / "Scripts" / "python.exe"
                if os.name == "nt"
                else venv_dir / "bin" / "python"
            )
            commands: list[tuple[str, list[str], Path]] = [
                (
                    "cacheless-lock-install",
                    [
                        str(venv_python),
                        "-m",
                        "pip",
                        "install",
                        "--no-cache-dir",
                        "--requirement",
                        str(repo_root / "configs" / "requirements.lock"),
                    ],
                    temp_root,
                ),
            ]
            # The sealed G0 reproduction intentionally remains on its historical
            # development lock.  G1 runtime dependencies are installed only by
            # the current-checkout regression, where the new lock is under test.
            if mode == "checkout-regression":
                commands.append(
                    (
                        "g1-runtime-lock-install",
                        [
                            str(venv_python),
                            "-m",
                            "pip",
                            "install",
                            "--no-cache-dir",
                            "--require-hashes",
                            "--requirement",
                            str(repo_root / "configs" / "g1-frame-requirements.lock"),
                        ],
                        temp_root,
                    )
                )
            commands.extend(
                [
                    (
                        "dependency-lock-audit",
                        [
                            str(venv_python),
                            str(repo_root / "tools" / "verify_dependency_lock.py"),
                            "--lock",
                            str(repo_root / "configs" / "requirements.lock"),
                            "--check-installed",
                        ],
                        repo_root,
                    ),
                    (
                        "ruff-lint",
                        [str(venv_python), "-m", "ruff", "check", "src", "tests", "tools"],
                        repo_root,
                    ),
                    (
                        "ruff-format",
                        [
                            str(venv_python),
                            "-m",
                            "ruff",
                            "format",
                            "--check",
                            "src",
                            "tests",
                            "tools",
                        ],
                        repo_root,
                    ),
                    ("mypy", [str(venv_python), "-m", "mypy"], repo_root),
                    (
                        "candidate-manifest-schema",
                        [
                            str(venv_python),
                            str(repo_root / "tools" / "validate_runtime_manifest.py"),
                            "--schema",
                            str(repo_root / "schemas" / "runtime-manifest.schema.json"),
                            "--manifest",
                            str(manifest_path),
                        ],
                        repo_root,
                    ),
                    (
                        "candidate-bundle-metadata",
                        [
                            str(venv_python),
                            str(repo_root / "tools" / "verify_bundle.py"),
                            "--bundle-dir",
                            str(bundle_dir),
                            "--metadata-only",
                        ],
                        repo_root,
                    ),
                    (
                        "pytest-coverage",
                        [
                            str(venv_python),
                            "-m",
                            "pytest",
                            f"--junitxml={junit_path}",
                            "--cov=maple_automation_core",
                            "--cov-report=term-missing",
                            f"--cov-report=xml:{coverage_path}",
                            "--cov-fail-under=90",
                        ],
                        repo_root,
                    ),
                    (
                        "wheel-sdist-build",
                        [
                            str(venv_python),
                            "-m",
                            "build",
                            "--wheel",
                            "--sdist",
                            "--no-isolation",
                            "--outdir",
                            str(artifact_dir),
                        ],
                        repo_root,
                    ),
                ]
            )
            for name, command, cwd in commands:
                _run_step(
                    checks=checks,
                    name=name,
                    command=command,
                    cwd=cwd,
                    env=env,
                    repo_root=repo_root,
                    temp_root=temp_root,
                )

            wheels = sorted(artifact_dir.glob("*.whl"))
            sdists = sorted(artifact_dir.glob("*.tar.gz"))
            if len(wheels) != 1:
                raise SmokeFailure(f"Expected one wheel, found {len(wheels)}")
            if len(sdists) != 1:
                raise SmokeFailure(f"Expected one sdist, found {len(sdists)}")
            _run_step(
                checks=checks,
                name="normalize-sdist",
                command=[
                    str(venv_python),
                    str(repo_root / "tools" / "normalize_sdist.py"),
                    "--source-date-epoch",
                    source_date_epoch,
                    str(sdists[0]),
                ],
                cwd=temp_root,
                env=env,
                repo_root=repo_root,
                temp_root=temp_root,
            )
            _run_step(
                checks=checks,
                name="wheel-install",
                command=[
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "--no-deps",
                    "--force-reinstall",
                    str(wheels[0]),
                ],
                cwd=temp_root,
                env=env,
                repo_root=repo_root,
                temp_root=temp_root,
            )
            if mode == "checkout-regression":
                _run_step(
                    checks=checks,
                    name="g1-installed-runtime-smoke",
                    command=[
                        str(venv_python),
                        str(repo_root / "tools" / "run_g1_installed_smoke.py"),
                        "--repo-root",
                        str(repo_root),
                        "--require-installed",
                        "--runtime-only",
                    ],
                    cwd=temp_root,
                    env=env,
                    repo_root=repo_root,
                    temp_root=temp_root,
                )
            _run_step(
                checks=checks,
                name="installed-wheel-import",
                command=[
                    str(venv_python),
                    "-c",
                    (
                        "from pathlib import Path; import maple_automation_core as package; "
                        f"root=Path({str(repo_root)!r}).resolve(); "
                        "loaded=Path(package.__file__).resolve(); "
                        "assert not loaded.is_relative_to(root), loaded; print(loaded)"
                    ),
                ],
                cwd=temp_root,
                env=env,
                repo_root=repo_root,
                temp_root=temp_root,
            )
            replay_command = [
                str(venv_python),
                str(repo_root / "tools" / "run_golden_replay.py"),
                "--require-installed",
                "--fixture",
                str(fixture_path),
                "--runs",
                "3",
            ]
            shadow_command = [
                str(venv_python),
                str(repo_root / "tools" / "run_shadow.py"),
                "--require-installed",
                "--fixture",
                str(fixture_path),
            ]
            if mode == "g0-seal":
                replay_command.extend(("--manifest", str(manifest_path)))
                shadow_command.extend(("--manifest", str(manifest_path)))
            replay_command.extend(("--report", str(replay_path)))
            shadow_command.extend(("--report", str(shadow_path)))
            _run_step(
                checks=checks,
                name="golden-replay-three-runs",
                command=replay_command,
                cwd=temp_root,
                env=env,
                repo_root=repo_root,
                temp_root=temp_root,
            )
            _run_step(
                checks=checks,
                name="shadow-zero-real-input",
                command=shadow_command,
                cwd=temp_root,
                env=env,
                repo_root=repo_root,
                temp_root=temp_root,
            )
            if mode == "checkout-regression":
                _run_step(
                    checks=checks,
                    name="installed-frame-admission-three-runs",
                    command=[
                        str(venv_python),
                        str(repo_root / "tools" / "run_frame_admission_replay.py"),
                        "--require-installed",
                        "--fixture",
                        str(repo_root / "fixtures" / "g1" / "frame_admission_v1.json"),
                        "--schema",
                        str(repo_root / "schemas" / "frame-admission-report.schema.json"),
                        "--runs",
                        "3",
                        "--repo-root",
                        str(repo_root),
                        "--report",
                        str(frame_admission_path),
                    ],
                    cwd=temp_root,
                    env=env,
                    repo_root=repo_root,
                    temp_root=temp_root,
                )
            shadow = read_json(shadow_path)
            input_audit = shadow.get("input_audit", {})
            rollback_ok = (
                isinstance(input_audit, dict)
                and input_audit.get("core_v2_real_input_call_count") == 0
                and input_audit.get("double_write_event_count") == 0
                and input_audit.get("connected") is False
                and manifest.get("input_owner") == "legacy"
            )
            checks.append(
                {
                    "command": "stop Shadow process; inspect dry-run receipts and input owner",
                    "details": {
                        "core_runner_state": "stopped",
                        "core_v2_real_input_call_count": input_audit.get(
                            "core_v2_real_input_call_count"
                        ),
                        "double_write_event_count": input_audit.get("double_write_event_count"),
                        "input_owner_after_stop": manifest.get("input_owner"),
                        "sink_connected_after_stop": input_audit.get("connected"),
                    },
                    "duration_seconds": 0.0,
                    "name": "rollback-stop-core-keep-legacy-owner",
                    "status": "passed" if rollback_ok else "failed",
                }
            )
            if not rollback_ok:
                raise SmokeFailure("rollback ownership audit failed")

            # Sanitize before taking artifact metadata.  The uploaded artifact
            # hash must describe the exact bytes that the collector receives.
            sanitize_xml_evidence(temp_root)
            if xml_errors:
                raise SmokeFailure("could not sanitize CI XML evidence")
            report_artifacts = [
                (junit_path, "junit", "clean-junit"),
                (coverage_path, "coverage", "clean-coverage"),
                (replay_path, "evidence-report", "clean-replay-result"),
                (shadow_path, "evidence-report", "clean-shadow-result"),
            ]
            if mode == "checkout-regression":
                report_artifacts.append(
                    (
                        frame_admission_path,
                        "evidence-report",
                        "clean-frame-admission-result",
                    )
                )
            for path, kind, artifact_id in report_artifacts:
                artifacts.append(_artifact(repo_root, path, kind, artifact_id))
            for path in sorted(artifact_dir.iterdir()):
                kind = "wheel" if path.suffix == ".whl" else "sdist"
                artifacts.append(
                    _artifact(repo_root, path, kind, f"clean-build-{path.stem.lower()}")
                )
    except (OSError, SmokeFailure, ValueError) as exc:
        error = str(exc)

    # Also sanitize partial reports after a failed step so always-run CI
    # collection never uploads runner paths from a failure artifact.
    sanitize_xml_evidence()
    privacy_paths = [junit_path, coverage_path, replay_path, shadow_path]
    if mode == "checkout-regression":
        privacy_paths.append(frame_admission_path)
    findings = _absolute_path_findings(privacy_paths)
    expected_paths_missing = error is None and any(not path.is_file() for path in privacy_paths)
    privacy_status = "passed"
    if error is not None and not any(path.is_file() for path in privacy_paths):
        privacy_status = "skipped"
    elif xml_errors or findings or expected_paths_missing:
        privacy_status = "failed"
    checks.append(
        {
            "command": "sanitize and scan CI evidence, coverage and JUnit paths",
            "details": {
                "absolute_path_files": sorted(findings),
                "files_checked": sum(path.is_file() for path in privacy_paths),
                "sanitization_errors": xml_errors,
            },
            "duration_seconds": 0.0,
            "name": "evidence-path-privacy",
            "status": privacy_status,
        }
    )

    completed_at = _timestamp()
    status = (
        "passed"
        if error is None and all(check["status"] == "passed" for check in checks)
        else "failed"
    )
    report_source_commit = source_commit if mode == "g0-seal" else checkout_commit
    report_release_id = (
        release_id if mode == "g0-seal" else f"checkout-regression-{checkout_commit[:12]}"
    )
    report_error = _sanitize_text(error, repo_root=repo_root) if error is not None else None
    payload: dict[str, Any] = {
        "artifacts": artifacts,
        "bundle_id": report_release_id,
        "checks": checks,
        "environment": {
            "python_version": platform.python_version(),
            "runner_os": platform.system(),
            "working_directory_policy": "repository-relative",
            "workflow_name": os.environ.get("GITHUB_WORKFLOW", "local-clean-smoke"),
            "run_id": os.environ.get("GITHUB_RUN_ID", "0")
            if os.environ.get("GITHUB_RUN_ID", "0").isdigit()
            else "0",
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "0")
            if os.environ.get("GITHUB_RUN_ATTEMPT", "0").isdigit()
            else "0",
        },
        "evidence_id": f"{mode}-{report_source_commit[:12]}",
        "execution_mode": "shadow" if mode == "g0-seal" else "offline",
        "generated_at": completed_at,
        "release_id": report_release_id,
        "report_kind": "clean-smoke" if mode == "g0-seal" else "test",
        "schema_version": SCHEMA_VERSION,
        "source_commit": report_source_commit,
        "status": status,
        "subject_id": subject_id,
        "summary": {
            "checkout_commit": checkout_commit,
            "tested_commit": checkout_commit,
            "lineage_policy": mode,
            "baseline_bundle_id": release_id,
            "baseline_manifest_source_commit": source_commit,
            "error": report_error,
            "os_build": platform.version(),
            "platform": platform.platform(),
            "pip_cache": "disabled",
            "project_venv_reused": False,
            "runtime_manifest_path": _portable(repo_root, manifest_path),
            "runtime_manifest_sha256": manifest_sha256,
            "source_date_epoch": source_date_epoch,
            "source_assets_policy": (
                "sealed candidate metadata plus content-addressed external attestations"
                if mode == "g0-seal"
                else "current checkout source with sealed G0 manifest as read-only baseline"
            ),
            "wheel_install": True,
        },
    }
    write_json(output, payload)
    return output, status == "passed"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        output, passed = run_clean_smoke(args)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Clean smoke report written: {output}")
    if not passed:
        payload = read_json(output)
        print(f"Clean smoke failed: {payload.get('summary', {}).get('error')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
