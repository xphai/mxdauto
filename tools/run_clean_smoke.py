"""Run the G0 cacheless Windows clean smoke and emit one evidence report."""

from __future__ import annotations

import argparse
import os
import platform
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
    return parser.parse_args(argv)


def _inside(root: Path, value: Path) -> Path:
    path = (value if value.is_absolute() else root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Path must stay inside repository root: {value}")
    return path


def _portable(root: Path, path: Path) -> str:
    return safe_relative_path(path.resolve().relative_to(root.resolve()).as_posix())


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _command_text(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def _run_step(
    *,
    checks: list[dict[str, Any]],
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
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
                "command": _command_text(command),
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
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
    checks.append(
        {
            "command": _command_text(command),
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

    checkout_commit = git_commit(repo_root)
    checks: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    error: str | None = None
    output_dir = output.parent

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
    lineage_ok = lineage.returncode == 0 and changed.returncode == 0 and not unexpected_paths
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
                "manifest_source_commit": source_commit,
                "unexpected_paths": unexpected_paths,
            },
            "duration_seconds": 0.0,
            "name": "candidate-source-lineage",
            "status": "passed" if lineage_ok else "failed",
        }
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    source_epoch = subprocess.run(
        ["git", "show", "-s", "--format=%ct", source_commit],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    source_date_epoch = source_epoch.stdout.strip()
    if source_epoch.returncode != 0 or not source_date_epoch.isdigit():
        raise ValueError("Could not derive SOURCE_DATE_EPOCH from source_commit.")

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
            raise SmokeFailure("checkout is not a packaging/docs-only descendant of source_commit")
        with tempfile.TemporaryDirectory(prefix="maple-core-g0-clean-") as temp_name:
            temp_root = Path(temp_name).resolve()
            venv_dir = temp_root / "venv"
            artifact_dir = output_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            junit_path = output_dir / "clean-junit.xml"
            coverage_path = output_dir / "clean-coverage.xml"
            replay_path = output_dir / "clean-replay-report.json"
            shadow_path = output_dir / "clean-shadow-report.json"

            _run_step(
                checks=checks,
                name="create-isolated-venv",
                command=[sys.executable, "-m", "venv", str(venv_dir)],
                cwd=temp_root,
                env=env,
            )
            venv_python = (
                venv_dir / "Scripts" / "python.exe"
                if os.name == "nt"
                else venv_dir / "bin" / "python"
            )
            commands: tuple[tuple[str, list[str], Path], ...] = (
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
            )
            for name, command, cwd in commands:
                _run_step(checks=checks, name=name, command=command, cwd=cwd, env=env)

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
            )
            _run_step(
                checks=checks,
                name="golden-replay-three-runs",
                command=[
                    str(venv_python),
                    str(repo_root / "tools" / "run_golden_replay.py"),
                    "--require-installed",
                    "--fixture",
                    str(fixture_path),
                    "--runs",
                    "3",
                    "--manifest",
                    str(manifest_path),
                    "--report",
                    str(replay_path),
                ],
                cwd=temp_root,
                env=env,
            )
            _run_step(
                checks=checks,
                name="shadow-zero-real-input",
                command=[
                    str(venv_python),
                    str(repo_root / "tools" / "run_shadow.py"),
                    "--require-installed",
                    "--fixture",
                    str(fixture_path),
                    "--manifest",
                    str(manifest_path),
                    "--report",
                    str(shadow_path),
                ],
                cwd=temp_root,
                env=env,
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

            for path, kind, artifact_id in (
                (junit_path, "junit", "clean-junit"),
                (coverage_path, "coverage", "clean-coverage"),
                (replay_path, "evidence-report", "clean-replay-result"),
                (shadow_path, "evidence-report", "clean-shadow-result"),
            ):
                artifacts.append(_artifact(repo_root, path, kind, artifact_id))
            for path in sorted(artifact_dir.iterdir()):
                kind = "wheel" if path.suffix == ".whl" else "sdist"
                artifacts.append(
                    _artifact(repo_root, path, kind, f"clean-build-{path.stem.lower()}")
                )
    except (OSError, SmokeFailure, ValueError) as exc:
        error = str(exc)

    completed_at = _timestamp()
    status = (
        "passed"
        if error is None and all(check["status"] == "passed" for check in checks)
        else "failed"
    )
    payload: dict[str, Any] = {
        "artifacts": artifacts,
        "bundle_id": release_id,
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
        "evidence_id": f"clean-smoke-{source_commit[:12]}",
        "execution_mode": "shadow",
        "generated_at": completed_at,
        "release_id": release_id,
        "report_kind": "clean-smoke",
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "status": status,
        "subject_id": subject_id,
        "summary": {
            "checkout_commit": checkout_commit,
            "error": error,
            "os_build": platform.version(),
            "platform": platform.platform(),
            "pip_cache": "disabled",
            "project_venv_reused": False,
            "runtime_manifest_path": _portable(repo_root, manifest_path),
            "runtime_manifest_sha256": manifest_sha256,
            "source_date_epoch": source_date_epoch,
            "source_assets_policy": (
                "checkout metadata plus content-addressed external attestations"
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
