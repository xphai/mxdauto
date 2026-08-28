from __future__ import annotations

import json
import shutil
from pathlib import Path

from tools import build_candidate_bundle
from tools.bundle_common import sha256_file
from tools.report_binding import canonical_report_digest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_empty_junit_and_low_coverage_are_failed(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text("<testsuites />", encoding="utf-8")
    coverage = tmp_path / "coverage.xml"
    coverage.write_text(
        '<coverage line-rate="0.50" lines-covered="5" lines-valid="10" />',
        encoding="utf-8",
    )

    junit_status, junit_details, _ = build_candidate_bundle._read_junit(junit)
    coverage_status, coverage_details, _ = build_candidate_bundle._read_coverage(coverage)

    assert junit_status == "failed"
    assert junit_details["tests"] == 0
    assert coverage_status == "failed"
    assert coverage_details["minimum_line_rate"] == 0.9


def _bound_replay_payload(core_root: Path, manifest_sha256: str) -> dict[str, object]:
    fixture_path = core_root / "fixtures" / "golden" / "pilot_minimal_v1.json"
    runs = [
        {
            "run_index": index,
            "event_count": 13,
            "planned_action_count": 2,
            "output_digest": "a" * 64,
            "event_digest": "b" * 64,
            "event_sequence_digest": "c" * 64,
        }
        for index in range(3)
    ]
    payload: dict[str, object] = {
        "bundle_id": build_candidate_bundle.BUNDLE_ID,
        "candidate_binding": {
            "bundle_id": build_candidate_bundle.BUNDLE_ID,
            "release_id": build_candidate_bundle.RELEASE_ID,
            "runtime_manifest_path": "bundles/candidate/runtime-manifest.json",
            "runtime_manifest_sha256": manifest_sha256,
            "source_commit": "a" * 40,
        },
        "deterministic": True,
        "fixture_digest": "d" * 64,
        "fixture_file_sha256": sha256_file(fixture_path),
        "fixture_id": "golden-pilot-minimal-v1",
        "release_id": build_candidate_bundle.RELEASE_ID,
        "repeat_count": 3,
        "report_id": (f"replay-golden-pilot-minimal-v1-{build_candidate_bundle.RELEASE_ID}"),
        "report_type": "golden_replay",
        "runs": runs,
        "runtime_manifest_path": "bundles/candidate/runtime-manifest.json",
        "runtime_manifest_sha256": manifest_sha256,
        "session_id": "golden-session-001",
        "source_commit": "a" * 40,
        "status": "PASS",
    }
    payload["report_digest"] = canonical_report_digest(payload)
    return payload


def test_bound_replay_digest_and_fixture_are_recomputed(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures" / "golden" / "pilot_minimal_v1.json"
    fixture.parent.mkdir(parents=True)
    shutil.copyfile(PROJECT_ROOT / build_candidate_bundle.FIXTURE_RELATIVE_PATH, fixture)
    manifest_sha256 = "e" * 64
    payload = _bound_replay_payload(tmp_path, manifest_sha256)
    report_path = tmp_path / "replay.json"
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    status, _, _ = build_candidate_bundle._read_bound_result(
        core_root=tmp_path,
        path=report_path,
        report_kind="replay",
        source_commit="a" * 40,
        manifest_repo_path="bundles/candidate/runtime-manifest.json",
        manifest_sha256=manifest_sha256,
    )
    assert status == "passed"

    payload["fixture_digest"] = "f" * 64
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    status, details, _ = build_candidate_bundle._read_bound_result(
        core_root=tmp_path,
        path=report_path,
        report_kind="replay",
        source_commit="a" * 40,
        manifest_repo_path="bundles/candidate/runtime-manifest.json",
        manifest_sha256=manifest_sha256,
    )
    assert status == "failed"
    assert "report_digest does not match canonical report content" in details["errors"]
