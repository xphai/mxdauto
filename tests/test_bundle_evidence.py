from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from maple_automation_core.replay import GoldenReplayRunner, ShadowRunner
from tools import collect_ci_evidence, verify_bundle
from tools.bundle_common import safe_relative_path, sha256_file
from tools.report_binding import canonical_report_digest
from tools.verify_dependency_lock import parse_lock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "candidate-core-v2-20260829-shadow"
BUNDLE_DIR = PROJECT_ROOT / "bundles" / BUNDLE_ID
EVIDENCE_DIR = PROJECT_ROOT / "evidence" / "g0-candidate-core-v2-20260829"
FIXTURE_SHA256 = "22dd58eeaee16cb72eea529f177aad86747e162e7d9e7458a284a0dad4e6eb34"
FIXTURE_REPLAY_DIGEST = "767cad8215a420e0f01c36d434cc96530a74acc552f97c8685740a81de6baaed"
FIXTURE_SHADOW_DIGEST = "ac6f55d4cf3b57d494c951a8affafef73aa99eee3b49b3d76cff7c7dd46ee07b"
FIXTURE_REPORT_ROLE_FILES = {
    "current-replay": "golden-replay-report.json",
    "current-shadow": "golden-shadow-report.json",
    "clean-replay": "clean-replay-report.json",
    "clean-shadow": "clean-shadow-report.json",
}


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_fixture_evidence_set(
    repo_root: Path,
) -> tuple[Path, dict[str, Path], dict[str, Any], dict[str, Any]]:
    schema_dir = repo_root / "schemas"
    schema_dir.mkdir()
    shutil.copy2(
        PROJECT_ROOT / "schemas" / "evidence-report.schema.json",
        schema_dir / "evidence-report.schema.json",
    )
    fixture = repo_root / "fixtures" / "golden" / "pilot_minimal_v1.json"
    fixture.parent.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "fixtures" / "golden" / fixture.name, fixture)
    fixture_sha256 = sha256_file(fixture)

    replay_payload = GoldenReplayRunner(fixture).run_repeated(3).to_dict()
    replay_payload["fixture_file_sha256"] = fixture_sha256
    replay_payload["report_digest"] = canonical_report_digest(replay_payload)
    shadow_payload = ShadowRunner(fixture).run().to_dict()
    shadow_payload["fixture_file_sha256"] = fixture_sha256
    shadow_payload["report_digest"] = canonical_report_digest(shadow_payload)

    report_dir = repo_root / "evidence" / "ci-run"
    report_dir.mkdir(parents=True)
    role_paths: dict[str, Path] = {}
    for role, name in FIXTURE_REPORT_ROLE_FILES.items():
        path = report_dir / name
        payload = replay_payload if role.endswith("-replay") else shadow_payload
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        role_paths[role] = path
    return fixture, role_paths, replay_payload, shadow_payload


def _fixture_evidence_cli(
    *,
    repo_root: Path,
    output: Path,
    fixture: Path,
    role_entries: list[tuple[str, Path]],
    fixture_pin: str | None = FIXTURE_SHA256,
    replay_pin: str | None = FIXTURE_REPLAY_DIGEST,
    shadow_pin: str | None = FIXTURE_SHADOW_DIGEST,
) -> list[str]:
    argv = [
        "--output",
        str(output),
        "--repo-root",
        str(repo_root),
        "--fixture",
        str(fixture),
        "--source-commit",
        "a" * 40,
        "--event",
        "local",
        "--timestamp",
        "2026-08-29T00:00:00Z",
        "--workflow-result",
        "success",
    ]
    for role, path in role_entries:
        argv.extend(("--fixture-evidence-report", f"{role}::{path}"))
    for option, value in (
        ("--expected-fixture-sha256", fixture_pin),
        ("--expected-fixture-replay-digest", replay_pin),
        ("--expected-fixture-shadow-digest", shadow_pin),
    ):
        if value is not None:
            argv.extend((option, value))
    return argv


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


def test_ci_checkout_regression_preserves_the_sealed_static_packet() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_report = "evidence/ci-run/checkout-smoke-report.json"
    static_report = "evidence/clean-smoke/clean-smoke-report.json"

    assert f"run_clean_smoke.py --mode checkout-regression --output {ci_report}" in workflow
    assert f'$artifactArgs += "{ci_report}"' in workflow
    assert f"--evidence-report {ci_report}" not in workflow
    assert f"run_clean_smoke.py --output {static_report}" not in workflow
    assert "$checkoutSource = (git rev-parse HEAD).Trim()" in workflow
    assert "$candidateSource =" not in workflow
    assert (
        "run_golden_replay.py --runs 3 --report evidence/ci-run/golden-replay-report.json"
    ) in workflow
    assert "run_shadow.py --report evidence/ci-run/golden-shadow-report.json" in workflow
    assert "run_golden_replay.py --runs 3 --manifest bundles/" not in workflow
    assert "run_shadow.py --manifest bundles/" not in workflow
    assert "--evidence-report evidence/ci-run/" not in workflow
    fixture_only_reports = {
        "evidence/ci-run/golden-replay-report.json": "current-replay",
        "evidence/ci-run/golden-shadow-report.json": "current-shadow",
        "evidence/ci-run/clean-replay-report.json": "clean-replay",
        "evidence/ci-run/clean-shadow-report.json": "clean-shadow",
    }
    assert "--fixture fixtures/golden/pilot_minimal_v1.json" in workflow
    assert f"--expected-fixture-sha256 {FIXTURE_SHA256}" in workflow
    assert f"--expected-fixture-replay-digest {FIXTURE_REPLAY_DIGEST}" in workflow
    assert f"--expected-fixture-shadow-digest {FIXTURE_SHADOW_DIGEST}" in workflow
    for report, expected_role in fixture_only_reports.items():
        assert f"--fixture-evidence-report {expected_role}::{report}" in workflow
        assert f'$artifactArgs += "{report}"' not in workflow
    assert workflow.count("--fixture-evidence-report ") == len(fixture_only_reports)
    assert "--bundle-id candidate-core-v2-20260829-shadow" not in workflow
    assert "evidence/ci-run/clean-frame-admission-report.json" in workflow


def test_ci_collects_g1_frame_admission_as_checkout_evidence() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    report = "evidence/ci-run/frame-admission-report.json"

    assert "run_frame_admission_replay.py --runs 3" in workflow
    assert "--fixture fixtures/g1/frame_admission_v1.json" in workflow
    assert "--schema schemas/frame-admission-report.schema.json" in workflow
    assert f"--report {report}" in workflow
    assert f'$artifactArgs += "{report}"' in workflow
    assert f"--evidence-report {report}" not in workflow
    assert "name: g1-frame-admission-${{ github.run_id }}" in workflow


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
    without_checkout = dict(payload)
    without_checkout.pop("checkout_commit")
    required_errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(without_checkout)
    )
    assert any(error.validator == "required" for error in required_errors)


def test_ci_evidence_separates_manifest_source_and_checkout_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = "b" * 40
    observed_roots: list[Path] = []

    def fake_git_commit(repo_root: Path) -> str:
        observed_roots.append(repo_root)
        return checkout

    monkeypatch.setattr(collect_ci_evidence, "git_commit", fake_git_commit)
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
            "push",
            "--timestamp",
            "2026-08-29T00:00:00Z",
        ]
    )

    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    assert payload["source_commit"] == "a" * 40
    assert payload["checkout_commit"] == checkout
    assert observed_roots == [tmp_path.resolve()]


def test_ci_evidence_remote_without_git_head_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_git_commit(repo_root: Path) -> str:
        raise ValueError(f"no git repository: {repo_root}")

    monkeypatch.setattr(collect_ci_evidence, "git_commit", missing_git_commit)
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
            "push",
            "--timestamp",
            "2026-08-29T00:00:00Z",
        ]
    )

    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    assert payload["checkout_commit"] == "0" * 40
    assert payload["status"] == "failed"
    binding_checks = [
        check
        for check in payload["checks"]
        if check["name"] == "candidate-binding"  # type: ignore[index]
    ]
    assert binding_checks
    assert any(
        "checkout commit" in error
        for error in binding_checks[0]["details"]["errors"]  # type: ignore[index]
    )


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
        expected_checkout_commit=str(clean_summary["checkout_commit"]),
    )

    assert check["status"] == "passed"
    assert check["details"]["report_digest_valid"] is None  # type: ignore[index]


def test_ci_evidence_rejects_clean_smoke_checkout_tampering() -> None:
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
        expected_checkout_commit="b" * 40,
    )

    assert check["status"] == "failed"
    assert any(
        "checkout_commit does not match CI checkout_commit" in error
        for error in check["details"]["errors"]  # type: ignore[index]
    )


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


def test_ci_collector_sanitizes_junit_paths_before_hashing(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    report_dir = repo_root / "evidence" / "ci-run"
    report_dir.mkdir(parents=True)
    junit = report_dir / "junit.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        f'<testcase file="{repo_root / "tests" / "test_ok.py"}" /></testsuite>',
        encoding="utf-8",
    )
    output = report_dir / "ci-evidence.json"
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(repo_root),
            "--junit",
            str(junit),
            "--sanitize-paths",
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--workflow-result",
            "success",
        ]
    )

    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    checks = {check["name"]: check for check in payload["checks"]}  # type: ignore[index]
    sanitized = junit.read_text(encoding="utf-8")
    assert str(repo_root) not in sanitized
    assert checks["evidence-path-privacy"]["status"] == "passed"
    assert checks["evidence-path-privacy"]["details"]["rewritten_files"] == [  # type: ignore[index]
        "evidence/ci-run/junit.xml"
    ]
    assert checks["junit"]["details"]["path"] == "evidence/ci-run/junit.xml"  # type: ignore[index]
    junit_artifact = next(
        artifact
        for artifact in payload["artifacts"]
        if artifact["kind"] == "junit"  # type: ignore[index]
    )
    assert junit_artifact["sha256"] == sha256_file(junit)


def test_ci_collector_sanitizes_decoded_xml_entities_and_cdata(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    report_dir = repo_root / "evidence" / "ci-run"
    report_dir.mkdir(parents=True)
    junit = report_dir / "junit.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase file="C:&#92;Users&#92;Runner&#92;secret.py" />'
        "<system-out><![CDATA[C:\\runner\\secret.log]]></system-out>"
        "<!-- C:\\runner\\comment.log -->"
        "</testsuite>",
        encoding="utf-8",
    )
    output = report_dir / "ci-evidence.json"
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(repo_root),
            "--junit",
            str(junit),
            "--sanitize-paths",
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--workflow-result",
            "success",
        ]
    )

    collect_ci_evidence.collect_evidence(args)

    root = ElementTree.parse(junit).getroot()
    payload = _load_json(output)
    checks = {check["name"]: check for check in payload["checks"]}  # type: ignore[index]
    assert root.find("testcase").attrib["file"] == "[absolute-path]"  # type: ignore[union-attr]
    assert root.findtext("system-out") == "[absolute-path]"
    assert "C:\\runner" not in junit.read_text(encoding="utf-8")
    assert checks["evidence-path-privacy"]["status"] == "passed"


def test_ci_collector_scrubs_generic_json_and_marks_privacy_failure(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    report_dir = repo_root / "evidence" / "ci-run"
    report_dir.mkdir(parents=True)
    leaked = report_dir / "extra.json"
    leaked.write_text(json.dumps({"runner": r"C:\Users\Runner\secret"}), encoding="utf-8")
    output = report_dir / "ci-evidence.json"
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(repo_root),
            "--sanitize-paths",
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--workflow-result",
            "success",
        ]
    )

    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    checks = {check["name"]: check for check in payload["checks"]}  # type: ignore[index]
    sanitized = leaked.read_text(encoding="utf-8")
    assert "C:\\Users\\Runner\\secret" not in sanitized
    assert "[absolute-path]" in sanitized
    assert checks["evidence-path-privacy"]["status"] == "failed"
    assert checks["evidence-path-privacy"]["details"]["rewritten_files"] == [  # type: ignore[index]
        "evidence/ci-run/extra.json"
    ]
    assert "C:\\Users\\Runner\\secret" not in output.read_text(encoding="utf-8")


def test_ci_collector_sanitizes_checkout_json_without_breaking_escaped_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    report_dir = repo_root / "evidence" / "ci-run"
    report_dir.mkdir(parents=True)
    checkout_report = report_dir / "checkout-smoke-report.json"
    runner_temp = Path(r"C:\runner-temp")
    command = (
        r'C:\runner-temp\venv\Scripts\python.exe -c "from pathlib import Path; '
        f'root=Path({str(repo_root)!r}).resolve(); print(root)"'
    )
    checkout_report.write_text(
        json.dumps({"checks": [{"command": command}]}, indent=2),
        encoding="utf-8",
    )
    output = report_dir / "ci-evidence.json"
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(repo_root),
            "--sanitize-paths",
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--workflow-result",
            "success",
        ]
    )

    collect_ci_evidence.collect_evidence(args)

    sanitized_report = _load_json(checkout_report)
    sanitized_command = sanitized_report["checks"][0]["command"]  # type: ignore[index]
    payload = _load_json(output)
    checks = {check["name"]: check for check in payload["checks"]}  # type: ignore[index]
    assert isinstance(sanitized_command, str)
    assert "[temp]" in sanitized_command
    assert "from pathlib import Path" in sanitized_command
    assert str(repo_root) not in sanitized_command
    assert checks["evidence-path-privacy"]["status"] == "passed"
    assert github_output.read_text(encoding="utf-8").splitlines()[-1] == "upload_ready=true"


def test_ci_collector_quarantines_damaged_json_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    report_dir = repo_root / "evidence" / "ci-run"
    report_dir.mkdir(parents=True)
    damaged = report_dir / "checkout-smoke-report.json"
    damaged.write_text(
        '{"command": "[temp][absolute-path]"from pathlib import Path"}',
        encoding="utf-8",
    )
    output = report_dir / "ci-evidence.json"
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(repo_root),
            "--sanitize-paths",
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--workflow-result",
            "success",
        ]
    )

    collect_ci_evidence.collect_evidence(args)

    payload = _load_json(output)
    checks = {check["name"]: check for check in payload["checks"]}  # type: ignore[index]
    privacy = checks["evidence-path-privacy"]
    assert not damaged.exists()
    assert privacy["status"] == "failed"
    assert privacy["details"]["sanitizer_errors"] == [  # type: ignore[index]
        "evidence/ci-run/checkout-smoke-report.json"
    ]
    assert privacy["details"]["quarantined_files"] == [  # type: ignore[index]
        "evidence/ci-run/checkout-smoke-report.json"
    ]
    assert payload["status"] == "failed"
    assert github_output.read_text(encoding="utf-8").splitlines()[-1] == "upload_ready=true"


def test_ci_collector_quarantines_scrub_failure_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    report_dir = repo_root / "evidence" / "ci-run"
    report_dir.mkdir(parents=True)
    sentinel = report_dir / "sentinel.json"
    sentinel.write_text('{"sentinel": true}', encoding="utf-8")
    output = report_dir / "ci-evidence.json"
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    original_sanitizer = collect_ci_evidence._sanitize_text_file

    def fail_sentinel(path: Path, *, repo_root: Path, temp_root: Path | None = None) -> bool:
        if path == sentinel:
            raise OSError("sentinel scrub failure")
        return original_sanitizer(path, repo_root=repo_root, temp_root=temp_root)

    monkeypatch.setattr(collect_ci_evidence, "_sanitize_text_file", fail_sentinel)
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(repo_root),
            "--sanitize-paths",
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--workflow-result",
            "success",
        ]
    )

    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    checks = {check["name"]: check for check in payload["checks"]}  # type: ignore[index]
    privacy = checks["evidence-path-privacy"]
    assert not sentinel.exists()
    assert not any(path.name == sentinel.name for path in report_dir.rglob("*"))
    assert privacy["status"] == "failed"
    assert privacy["details"]["sanitizer_errors"] == ["evidence/ci-run/sentinel.json"]  # type: ignore[index]
    assert privacy["details"]["quarantined_files"] == [  # type: ignore[index]
        "evidence/ci-run/sentinel.json"
    ]
    assert privacy["details"]["quarantine_failures"] == []  # type: ignore[index]
    assert github_output.read_text(encoding="utf-8").splitlines()[-1] == "upload_ready=true"


def test_ci_collector_blocks_upload_when_scrub_failure_cannot_be_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    report_dir = repo_root / "evidence" / "ci-run"
    report_dir.mkdir(parents=True)
    sentinel = report_dir / "sentinel.json"
    sentinel.write_text('{"sentinel": true}', encoding="utf-8")
    output = report_dir / "ci-evidence.json"
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    def fail_sentinel(path: Path, *, repo_root: Path, temp_root: Path | None = None) -> bool:
        if path == sentinel:
            raise OSError("sentinel scrub failure")
        return False

    monkeypatch.setattr(collect_ci_evidence, "_sanitize_text_file", fail_sentinel)
    monkeypatch.setattr(collect_ci_evidence, "_remove_or_quarantine", lambda *args, **kwargs: False)
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(repo_root),
            "--sanitize-paths",
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--workflow-result",
            "success",
        ]
    )

    collect_ci_evidence.collect_evidence(args)
    payload = _load_json(output)
    checks = {check["name"]: check for check in payload["checks"]}  # type: ignore[index]
    privacy = checks["evidence-path-privacy"]
    assert sentinel.exists()
    assert privacy["details"]["quarantine_failures"] == [  # type: ignore[index]
        "evidence/ci-run/sentinel.json"
    ]
    assert "upload_ready=false" in github_output.read_text(encoding="utf-8")


def test_ci_collector_keeps_upload_closed_when_final_output_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    output = repo_root / "ci-evidence.json"
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    def write_invalid_json(path: Path, payload: dict[str, object]) -> None:
        del payload
        path.write_text('{"status": "passed"', encoding="utf-8")

    monkeypatch.setattr(collect_ci_evidence, "write_json", write_invalid_json)
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(repo_root),
            "--sanitize-paths",
            "--source-commit",
            "a" * 40,
            "--event",
            "local",
            "--timestamp",
            "2026-08-29T00:00:00Z",
            "--workflow-result",
            "success",
        ]
    )

    with pytest.raises(ValueError, match="failed final validation"):
        collect_ci_evidence.collect_evidence(args)

    assert not output.exists()
    assert github_output.read_text(encoding="utf-8").splitlines()[-1] == "upload_ready=false"


def test_ci_collector_uses_relative_missing_artifact_path(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    output = repo_root / "ci-evidence.json"
    args = collect_ci_evidence._parse_args(
        [
            "--output",
            str(output),
            "--repo-root",
            str(repo_root),
            "--artifact",
            "dist/missing.whl",
            "--sanitize-paths",
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
    payload_text = output.read_text(encoding="utf-8")
    payload = _load_json(output)
    build_check = next(check for check in payload["checks"] if check["name"] == "build-artifacts")  # type: ignore[index]
    assert build_check["details"]["missing"] == ["dist/missing.whl"]  # type: ignore[index]
    assert str(repo_root) not in payload_text
    assert _load_json(output)["status"] == "failed"


def test_ci_always_collector_guards_missing_dist() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    collector_start = workflow.index("- name: Collect CI evidence metadata")
    collector_end = workflow.index("- name: Upload JUnit and coverage", collector_start)
    collector = workflow[collector_start:collector_end]
    guard = "if (Test-Path -LiteralPath dist -PathType Container)"
    enumeration = "Get-ChildItem -LiteralPath dist -File"
    assert guard in collector
    assert enumeration in collector
    assert collector.index(guard) < collector.index(enumeration)
    assert '"upload_ready=false"' in collector
    upload_step_count = workflow.count("uses: actions/upload-artifact@v7")
    upload_guard_count = workflow.count(
        "if: ${{ always() && steps.collect_ci_evidence.outputs.upload_ready == 'true' }}"
    )
    assert upload_step_count == 6
    assert upload_guard_count == upload_step_count


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


def test_ci_fixture_evidence_report_rejects_stale_digest_without_candidate_claims(
    tmp_path: Path,
) -> None:
    fixture, role_paths, replay_payload, _ = _write_fixture_evidence_set(tmp_path)
    report = role_paths["current-replay"]
    stale_payload = dict(replay_payload)
    stale_payload["report_digest"] = "0" * 64
    report.write_text(json.dumps(stale_payload, sort_keys=True), encoding="utf-8")

    def collect(output_name: str) -> dict[str, object]:
        output = tmp_path / output_name
        args = collect_ci_evidence._parse_args(
            _fixture_evidence_cli(
                repo_root=tmp_path,
                output=output,
                fixture=fixture,
                role_entries=list(role_paths.items()),
            )
        )
        collect_ci_evidence.collect_evidence(args)
        return _load_json(output)

    failed_payload = collect("failed-ci-evidence.json")
    report_relative = report.relative_to(tmp_path).as_posix()
    failed_check = next(
        check
        for check in failed_payload["checks"]  # type: ignore[union-attr]
        if str(check["name"]).startswith("fixture-evidence-report")  # type: ignore[index]
        and check["details"]["path"] == report_relative  # type: ignore[index]
    )
    candidate_check = next(
        check
        for check in failed_payload["checks"]  # type: ignore[union-attr]
        if check["name"] == "candidate-binding"  # type: ignore[index]
    )
    assert failed_payload["status"] == "failed"
    assert failed_check["status"] == "failed"  # type: ignore[index]
    assert failed_check["details"]["binding_scope"] == "fixture-only"  # type: ignore[index]
    assert any(
        "report_digest does not match canonical report content" in error
        for error in failed_check["details"]["errors"]  # type: ignore[index]
    )
    assert any(
        "report_digest does not match pinned replay digest" in error
        for error in failed_check["details"]["errors"]  # type: ignore[index]
    )
    assert candidate_check["status"] == "skipped"  # type: ignore[index]

    report.write_text(json.dumps(replay_payload, sort_keys=True), encoding="utf-8")
    passed_payload = collect("passed-ci-evidence.json")
    passed_check = next(
        check
        for check in passed_payload["checks"]  # type: ignore[union-attr]
        if str(check["name"]).startswith("fixture-evidence-report")  # type: ignore[index]
        and check["details"]["path"] == report_relative  # type: ignore[index]
    )
    assert passed_payload["status"] == "passed"
    assert passed_check["status"] == "passed"  # type: ignore[index]
    assert passed_check["details"]["expected_report_digest"] == FIXTURE_REPLAY_DIGEST  # type: ignore[index]
    ci_schema = _load_json(PROJECT_ROOT / "schemas" / "ci-evidence.schema.json")
    assert list(Draft202012Validator(ci_schema).iter_errors(passed_payload)) == []


def test_ci_fixture_evidence_report_rejects_role_substitution_and_fabricated_semantics(
    tmp_path: Path,
) -> None:
    fixture, role_paths, replay_payload, shadow_payload = _write_fixture_evidence_set(tmp_path)

    def collect(
        output_name: str,
        fixture_pin: str = FIXTURE_SHA256,
        replay_pin: str = FIXTURE_REPLAY_DIGEST,
    ) -> dict[str, object]:
        args = collect_ci_evidence._parse_args(
            _fixture_evidence_cli(
                repo_root=tmp_path,
                output=tmp_path / output_name,
                fixture=fixture,
                role_entries=list(role_paths.items()),
                fixture_pin=fixture_pin,
                replay_pin=replay_pin,
            )
        )
        return _load_json(collect_ci_evidence.collect_evidence(args))

    current_shadow = role_paths["current-shadow"]
    current_shadow.write_text(json.dumps(replay_payload, sort_keys=True), encoding="utf-8")
    role_payload = collect("role-ci-evidence.json")
    role_check = next(
        check
        for check in role_payload["checks"]  # type: ignore[union-attr]
        if str(check["name"]).startswith("fixture-evidence-report")  # type: ignore[index]
        and check["details"]["path"]  # type: ignore[index]
        == current_shadow.relative_to(tmp_path).as_posix()
    )
    assert role_payload["status"] == "failed"
    assert any(
        "role mismatch: expected shadow, got replay" in error
        for error in role_check["details"]["errors"]  # type: ignore[index]
    )

    current_shadow.write_text(json.dumps(shadow_payload, sort_keys=True), encoding="utf-8")
    fabricated = json.loads(json.dumps(replay_payload))
    fabricated["fixture_digest"] = "f" * 64
    fabricated["bundle_id"] = "candidate-core-v2-20260829-shadow"
    fabricated["bundle_digest"] = "e" * 64
    fabricated["output_digest"] = "fabricated-output"
    for run in fabricated["runs"]:
        run["event_digest"] = "fabricated-event"
        run["event_sequence_digest"] = "fabricated-sequence"
        run["output_digest"] = "fabricated-run"
        run["bundle_digest"] = "fabricated-bundle"
    fabricated["report_digest"] = canonical_report_digest(fabricated)
    for role in ("current-replay", "clean-replay"):
        role_paths[role].write_text(json.dumps(fabricated, sort_keys=True), encoding="utf-8")

    semantic_payload = collect(
        "semantic-ci-evidence.json",
        replay_pin=fabricated["report_digest"],
    )
    semantic_checks = [
        check
        for check in semantic_payload["checks"]  # type: ignore[union-attr]
        if str(check["name"]).startswith("fixture-evidence-report")  # type: ignore[index]
        and check["details"]["expected_report_kind"] == "replay"  # type: ignore[index]
    ]
    assert semantic_payload["status"] == "failed"
    assert len(semantic_checks) == 2
    for semantic_check in semantic_checks:
        assert semantic_check["status"] == "failed"  # type: ignore[index]
        assert semantic_check["details"]["semantic_mismatch_fields"] == [  # type: ignore[index]
            "bundle_digest",
            "bundle_id",
            "fixture_digest",
            "output_digest",
            "runs",
        ]

    fixture_pin_payload = collect(
        "fixture-pin-ci-evidence.json",
        fixture_pin="0" * 64,
        replay_pin=fabricated["report_digest"],
    )
    fixture_check = next(
        check
        for check in fixture_pin_payload["checks"]  # type: ignore[union-attr]
        if check["name"] == "evidence-fixture"  # type: ignore[index]
    )
    assert fixture_pin_payload["status"] == "failed"
    assert fixture_check["status"] == "failed"  # type: ignore[index]
    assert fixture_check["details"]["reason"] == "fixture SHA-256 does not match pin"  # type: ignore[index]


@pytest.mark.parametrize(
    ("missing_pin", "expected_error"),
    [
        ("fixture", "--expected-fixture-sha256"),
        ("replay", "pinned replay report digest is required"),
        ("shadow", "pinned shadow report digest is required"),
    ],
)
def test_ci_fixture_evidence_requires_fixture_and_kind_kat_pins(
    tmp_path: Path,
    missing_pin: str,
    expected_error: str,
) -> None:
    fixture, role_paths, _, _ = _write_fixture_evidence_set(tmp_path)
    pins: dict[str, str | None] = {
        "fixture": FIXTURE_SHA256,
        "replay": FIXTURE_REPLAY_DIGEST,
        "shadow": FIXTURE_SHADOW_DIGEST,
    }
    pins[missing_pin] = None
    output = tmp_path / f"missing-{missing_pin}-pin.json"
    args = collect_ci_evidence._parse_args(
        _fixture_evidence_cli(
            repo_root=tmp_path,
            output=output,
            fixture=fixture,
            role_entries=list(role_paths.items()),
            fixture_pin=pins["fixture"],
            replay_pin=pins["replay"],
            shadow_pin=pins["shadow"],
        )
    )

    collect_ci_evidence.collect_evidence(args)

    payload = _load_json(output)
    assert payload["status"] == "failed"
    assert expected_error in json.dumps(payload["checks"], sort_keys=True)


@pytest.mark.parametrize("zero_value", [False, 0.0], ids=("false", "float-zero"))
def test_ci_fixture_shadow_semantics_compare_json_types_strictly(
    tmp_path: Path,
    zero_value: bool | float,
) -> None:
    fixture, role_paths, _, shadow_payload = _write_fixture_evidence_set(tmp_path)
    forged = json.loads(json.dumps(shadow_payload))
    forged["input_audit"]["core_v2_real_input_call_count"] = zero_value
    forged["report_digest"] = canonical_report_digest(forged)
    for role in ("current-shadow", "clean-shadow"):
        role_paths[role].write_text(json.dumps(forged, sort_keys=True), encoding="utf-8")
    output = tmp_path / f"typed-shadow-{type(zero_value).__name__}.json"
    args = collect_ci_evidence._parse_args(
        _fixture_evidence_cli(
            repo_root=tmp_path,
            output=output,
            fixture=fixture,
            role_entries=list(role_paths.items()),
            shadow_pin=forged["report_digest"],
        )
    )

    collect_ci_evidence.collect_evidence(args)

    payload = _load_json(output)
    shadow_checks = [
        check
        for check in payload["checks"]  # type: ignore[union-attr]
        if str(check["name"]).startswith("fixture-evidence-report")  # type: ignore[index]
        and check["details"]["expected_report_kind"] == "shadow"  # type: ignore[index]
    ]
    assert payload["status"] == "failed"
    assert len(shadow_checks) == 2
    for check in shadow_checks:
        assert check["status"] == "failed"  # type: ignore[index]
        assert check["details"]["report_digest_valid"] is True  # type: ignore[index]
        assert check["details"]["expected_report_digest"] == forged["report_digest"]  # type: ignore[index]
        assert "input_audit" in check["details"]["semantic_mismatch_fields"]  # type: ignore[index]


def test_ci_fixture_semantics_reject_added_generated_at_even_when_resigned(
    tmp_path: Path,
) -> None:
    fixture, role_paths, replay_payload, _ = _write_fixture_evidence_set(tmp_path)
    forged = json.loads(json.dumps(replay_payload))
    forged["generated_at"] = "2026-08-29T00:00:00Z"
    forged["report_digest"] = canonical_report_digest(forged)
    for role in ("current-replay", "clean-replay"):
        role_paths[role].write_text(json.dumps(forged, sort_keys=True), encoding="utf-8")
    output = tmp_path / "timestamped-replay.json"
    args = collect_ci_evidence._parse_args(
        _fixture_evidence_cli(
            repo_root=tmp_path,
            output=output,
            fixture=fixture,
            role_entries=list(role_paths.items()),
            replay_pin=forged["report_digest"],
        )
    )

    collect_ci_evidence.collect_evidence(args)

    payload = _load_json(output)
    replay_checks = [
        check
        for check in payload["checks"]  # type: ignore[union-attr]
        if str(check["name"]).startswith("fixture-evidence-report")  # type: ignore[index]
        and check["details"]["expected_report_kind"] == "replay"  # type: ignore[index]
    ]
    assert payload["status"] == "failed"
    assert len(replay_checks) == 2
    for check in replay_checks:
        assert check["status"] == "failed"  # type: ignore[index]
        assert check["details"]["report_digest_valid"] is True  # type: ignore[index]
        assert check["details"]["expected_report_digest"] == forged["report_digest"]  # type: ignore[index]
        assert "generated_at" in check["details"]["semantic_mismatch_fields"]  # type: ignore[index]


def test_ci_fixture_evidence_requires_four_unique_roles_and_paths(tmp_path: Path) -> None:
    fixture, role_paths, _, _ = _write_fixture_evidence_set(tmp_path)
    valid_entries = list(role_paths.items())

    invalid_role_sets: list[list[tuple[str, Path]]] = [
        valid_entries[:-1],
        [*valid_entries, ("current-replay", role_paths["current-replay"].with_name("copy.json"))],
        [
            (role, role_paths["current-replay"] if role == "clean-replay" else path)
            for role, path in valid_entries
        ],
    ]
    shutil.copy2(role_paths["current-replay"], role_paths["current-replay"].with_name("copy.json"))

    for index, role_entries in enumerate(invalid_role_sets):
        args = collect_ci_evidence._parse_args(
            _fixture_evidence_cli(
                repo_root=tmp_path,
                output=tmp_path / f"invalid-role-set-{index}.json",
                fixture=fixture,
                role_entries=role_entries,
            )
        )
        with pytest.raises(ValueError):
            collect_ci_evidence.collect_evidence(args)


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
