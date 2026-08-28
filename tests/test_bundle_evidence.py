from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tools import collect_ci_evidence, verify_bundle
from tools.bundle_common import safe_relative_path, sha256_file
from tools.verify_dependency_lock import parse_lock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "candidate-core-v2-20260829-shadow"
BUNDLE_DIR = PROJECT_ROOT / "bundles" / BUNDLE_ID
EVIDENCE_DIR = PROJECT_ROOT / "evidence" / "g0-candidate-core-v2-20260829"


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_release_schemas_are_valid() -> None:
    for schema_path in sorted((PROJECT_ROOT / "schemas").glob("*.schema.json")):
        schema = _load_json(schema_path)
        Draft202012Validator.check_schema(schema)


def test_candidate_bundle_metadata_verifies() -> None:
    errors = verify_bundle.verify_bundle(
        bundle_dir=BUNDLE_DIR,
        repo_root=PROJECT_ROOT,
        metadata_only=True,
    )
    assert errors == []


def test_ci_clean_smoke_preserves_the_sealed_static_packet() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_report = "evidence/ci-run/clean-smoke-report.json"
    static_report = "evidence/clean-smoke/clean-smoke-report.json"

    assert f"run_clean_smoke.py --output {ci_report}" in workflow
    assert f"--evidence-report {ci_report}" in workflow
    assert f"run_clean_smoke.py --output {static_report}" not in workflow


def test_candidate_manifest_matches_dec_001_and_has_passed_offline_reports() -> None:
    manifest = _load_json(BUNDLE_DIR / "runtime-manifest.json")
    assert manifest["map_id"] == "100040004"
    assert manifest["profile_id"] == "pilot-subject-01"
    assert manifest["subject_id"] == "subject-01"
    assert manifest["model_id"] == "best_forest_v3-candidate"
    assert manifest["classes"] == ["mob"]
    assert manifest["input_size"] == {"height": 640, "width": 640}
    assert manifest["thresholds"]["detection_confidence"] == 0.25  # type: ignore[index]
    assert manifest["key_bindings"]["attack"] == "a"  # type: ignore[index]
    assert manifest["input_owner"] == "legacy"
    assert manifest["real_input_enabled"] is False

    replay = _load_json(EVIDENCE_DIR / "replay-report.json")
    shadow = _load_json(EVIDENCE_DIR / "shadow-report.json")
    assert replay["status"] == "passed"
    assert shadow["status"] == "passed"
    assert replay["source_commit"] == manifest["source_commit"]
    assert shadow["source_commit"] == manifest["source_commit"]
    assert shadow["checks"][0]["details"]["real_input_call_count"] == 0  # type: ignore[index]
    assert shadow["checks"][0]["details"]["unclassified_diff_count"] == 0  # type: ignore[index]


def test_candidate_bundle_tampering_is_reported(tmp_path: Path) -> None:
    for relative in (
        Path("bundles") / BUNDLE_ID,
        Path("evidence") / "g0-candidate-core-v2-20260829",
        Path("evidence") / "ci",
        Path("dist"),
    ):
        source = PROJECT_ROOT / relative
        if source.is_dir():
            shutil.copytree(source, tmp_path / relative)
    manifest = tmp_path / "bundles" / BUNDLE_ID / "runtime-manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    errors = verify_bundle.verify_bundle(
        bundle_dir=tmp_path / "bundles" / BUNDLE_ID,
        repo_root=tmp_path,
        metadata_only=True,
    )

    assert any("manifest_sha256" in error for error in errors)


def test_ci_evidence_collection_matches_schema(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0" />', encoding="utf-8"
    )
    coverage = tmp_path / "coverage.xml"
    coverage.write_text(
        '<coverage line-rate="0.95" lines-covered="19" lines-valid="20" />',
        encoding="utf-8",
    )
    lock = tmp_path / "requirements.lock"
    lock.write_text("sample-package==1.2.3\n", encoding="utf-8")
    wheel = tmp_path / "sample_package-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel-fixture")
    output = tmp_path / "ci-evidence.json"
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(tmp_path),
            "--junit",
            str(junit),
            "--coverage",
            str(coverage),
            "--lock",
            str(lock),
            "--artifact",
            str(wheel),
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
        ]
    )
    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    schema = _load_json(PROJECT_ROOT / "schemas" / "ci-evidence.schema.json")
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload))
    assert errors == []
    assert payload["status"] == "passed"


def test_ci_evidence_records_github_step_outcomes_and_job_failure(tmp_path: Path) -> None:
    output = tmp_path / "ci-evidence.json"
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(tmp_path),
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--check-result",
            "ruff-lint::failure::python -m ruff check src tests tools",
            "--check-result",
            "ruff-format::success::python -m ruff format --check src tests tools",
            "--workflow-result",
            "failure",
        ]
    )

    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    checks = {check["name"]: check for check in payload["checks"]}  # type: ignore[index]
    assert payload["status"] == "failed"
    assert checks["ruff-lint"]["status"] == "failed"
    assert checks["ruff-lint"]["details"]["outcome"] == "failure"  # type: ignore[index]
    assert checks["ruff-format"]["status"] == "passed"
    assert checks["ruff-format"]["details"]["github_step_outcome"] == "success"  # type: ignore[index]


def test_ci_evidence_dependency_success_is_not_a_parse_error(tmp_path: Path) -> None:
    output = tmp_path / "ci-evidence.json"
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(tmp_path),
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--check-result",
            "install-dependencies::success::python -m pip install -r requirements.lock",
            "--workflow-result",
            "success",
        ]
    )

    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    checks = {check["name"]: check for check in payload["checks"]}  # type: ignore[index]
    assert payload["status"] == "passed"
    assert "ci-step-outcomes" not in checks
    assert checks["install-dependencies"]["status"] == "passed"
    assert payload["dependency_install_result"] == "passed"


def test_ci_evidence_accepts_clean_smoke_without_replay_digest_fields() -> None:
    clean_path = PROJECT_ROOT / "evidence" / "clean-smoke" / "clean-smoke-report.json"
    clean = _load_json(clean_path)
    clean_summary = clean["summary"]
    assert isinstance(clean_summary, dict)
    fixture_path = PROJECT_ROOT / "fixtures" / "golden" / "pilot_minimal_v1.json"
    schema = _load_json(PROJECT_ROOT / "schemas" / "evidence-report.schema.json")

    check, _ = collect_ci_evidence._validate_evidence_report(
        repo_root=PROJECT_ROOT,
        path=clean_path,
        index=2,
        schema=schema,
        expected_bundle_id=BUNDLE_ID,
        expected_release_id=BUNDLE_ID,
        expected_source_commit=str(clean["source_commit"]),
        manifest_repo_path=str(clean_summary["runtime_manifest_path"]),
        manifest_sha256=str(clean_summary["runtime_manifest_sha256"]),
        fixture_candidates=[(fixture_path, sha256_file(fixture_path))],
    )

    assert check["status"] == "passed"
    assert check["details"]["report_digest_valid"] is None  # type: ignore[index]


def test_ci_evidence_main_returns_failure_for_failed_packet(tmp_path: Path) -> None:
    output = tmp_path / "ci-evidence.json"
    result = collect_ci_evidence.main(
        [
            "--output",
            str(output),
            "--repo-root",
            str(tmp_path),
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--check-result",
            "pytest::failure::python -m pytest",
            "--workflow-result",
            "failure",
        ]
    )

    assert result == 1
    assert _load_json(output)["status"] == "failed"


def test_ci_evidence_missing_report_is_recorded_without_crashing(tmp_path: Path) -> None:
    output = tmp_path / "ci-evidence.json"
    missing_report = tmp_path / "evidence" / "replay-report.json"
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(tmp_path),
            "--evidence-report",
            str(missing_report),
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--workflow-result",
            "failure",
        ]
    )

    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    evidence_checks = [
        check
        for check in payload["checks"]
        if check["name"] == "evidence-report"  # type: ignore[index]
    ]
    assert payload["status"] == "failed"
    assert evidence_checks
    assert evidence_checks[0]["status"] == "failed"
    assert evidence_checks[0]["details"]["reason"] == "file does not exist"  # type: ignore[index]


def test_ci_evidence_reports_stale_internal_digest(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"fixture": true}\n', encoding="utf-8")
    report = tmp_path / "replay-report.json"
    report.write_text(
        json.dumps(
            {
                "report_type": "golden_replay",
                "status": "PASS",
                "fixture_file_sha256": "0" * 64,
                "report_digest": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "ci-evidence.json"
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(tmp_path),
            "--evidence-report",
            str(report),
            "--fixture",
            str(fixture),
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
        ]
    )

    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    evidence_checks = [
        check
        for check in payload["checks"]
        if check["name"] == "evidence-report"  # type: ignore[index]
    ]
    assert evidence_checks[0]["status"] == "failed"
    assert any(
        "report_digest does not match canonical report content" in error
        for error in evidence_checks[0]["details"]["errors"]  # type: ignore[index]
    )


def test_dependency_lock_rejects_unpinned_requirements(tmp_path: Path) -> None:
    lock = tmp_path / "requirements.lock"
    lock.write_text("pytest>=8\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exact"):
        parse_lock(lock)


def test_dependency_lock_normalizes_package_names(tmp_path: Path) -> None:
    lock = tmp_path / "requirements.lock"
    lock.write_text("typing_extensions==4.16.0\n", encoding="utf-8")
    assert parse_lock(lock) == {"typing-extensions": "4.16.0"}


@pytest.mark.parametrize("value", ["C:/asset", "folder/file:stream", "../asset", "/asset"])
def test_portable_paths_reject_drive_ads_and_traversal(value: str) -> None:
    with pytest.raises(ValueError):
        safe_relative_path(value)
