"""Recompute corpus, truth, CAS, and Event Tape provenance links."""

from __future__ import annotations

import json
import os
import stat
import sys
from argparse import ArgumentParser
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
try:
    import maple_automation_core as _runtime_package
except ModuleNotFoundError:
    sys.path.insert(0, str(SRC))
    import maple_automation_core as _runtime_package

from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402

from maple_automation_core.capture.pixel_store import canonical_json  # noqa: E402
from maple_automation_core.replay.event_tape import EventTape  # noqa: E402
from maple_automation_core.replay.frame_corpus import (  # noqa: E402
    FrameCorpusError,
    canonical_digest,
    load_strict_json,
    verify_corpus_file,
)

REPORT_SCHEMA_VERSION = "1.0.0"
_EVENT_PAYLOAD_KEYS = {
    "truth_scope",
    "truth_id",
    "truth_pixel_digest",
    "admission_status",
    "plan_suppressed",
    "fault_latched",
    "pixel_digest",
    "image_ref",
    "reason",
    "reason_code",
}
_SUPPRESSED_STATUSES = {"no_frame", "stale"}
_FATAL_STATUSES = {
    "duplicate",
    "out_of_order",
    "timestamp_regression",
    "frame_size_changed",
    "source_mismatch",
    "session_mismatch",
    "clock_domain_mismatch",
    "source_error",
}


def _payload_matches_truth(
    payload: Mapping[str, Any],
    sample: Mapping[str, Any],
    truth: Mapping[str, Any],
) -> bool:
    if set(payload) != _EVENT_PAYLOAD_KEYS:
        return False
    coarse = truth["expected_admission"]
    status = payload["admission_status"]
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason:
        return False
    common = (
        payload["truth_scope"] == "frame_ingestion_only"
        and payload["truth_id"] == sample["truth_id"]
        and payload["truth_pixel_digest"] == sample["pixel_digest"]
        and type(payload["plan_suppressed"]) is bool
        and type(payload["fault_latched"]) is bool
        and payload["admission_status"] == truth["expected_status"]
        and payload["reason_code"] == truth["expected_reason_code"]
    )
    if not common:
        return False
    if coarse == "accepted":
        return (
            status == "accepted"
            and payload["plan_suppressed"] is False
            and payload["fault_latched"] is False
            and payload["pixel_digest"] == sample["pixel_digest"]
            and payload["image_ref"] == sample["cas_ref"]
        )
    if coarse == "suppressed":
        return (
            status in _SUPPRESSED_STATUSES
            and payload["plan_suppressed"] is True
            and payload["fault_latched"] is False
            and payload["pixel_digest"] is None
            and payload["image_ref"] is None
        )
    return (
        coarse == "fatal"
        and status in _FATAL_STATUSES
        and payload["plan_suppressed"] is True
        and payload["fault_latched"] is True
        and payload["pixel_digest"] is None
        and payload["image_ref"] is None
    )


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file_without_links(path: Path, field_name: str) -> Path:
    """Reject lexical symlink/reparse components before resolving a file."""

    lexical = Path(os.path.abspath(path.expanduser()))
    for component in (*reversed(lexical.parents), lexical):
        if not component.exists() and not component.is_symlink():
            continue
        attributes = getattr(component.lstat(), "st_file_attributes", 0)
        if component.is_symlink() or attributes & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise FrameCorpusError(f"{field_name} contains a symlink/reparse point")
    resolved = lexical.resolve(strict=True)
    if not resolved.is_file():
        raise FrameCorpusError(f"{field_name} must be a regular file")
    return resolved


def _artifact(artifact_id: str, path: Path) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "privacy_class": "hash_only",
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
    }


def _schema_validate(payload: Mapping[str, Any], schema_path: Path) -> None:
    schema = load_strict_json(schema_path)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    if errors:
        detail = "; ".join(error.message for error in errors[:5])
        raise FrameCorpusError(f"provenance report schema validation failed: {detail}")


def build_report(
    manifest_path: Path,
    *,
    truth_root: Path,
    event_tapes: Sequence[Path],
    cas_root: Path | None,
    generated_at: str,
    schema_path: Path,
    profile: str = "b1_fixture",
) -> dict[str, Any]:
    """Build a report whose PASS status is derived from recomputed artifacts."""

    if not event_tapes:
        raise FrameCorpusError("at least one Event Tape is required")
    manifest_path = _regular_file_without_links(manifest_path, "manifest_path")
    summary = verify_corpus_file(
        manifest_path,
        truth_root=truth_root,
        cas_root=cas_root,
        profile=profile,
    )
    manifest = load_strict_json(manifest_path)
    samples = cast(list[dict[str, Any]], manifest["samples"])
    expected: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    observed: set[str] = set()
    orphan_count = 0
    mismatch_count = 0
    event_count = 0
    artifacts = [_artifact("frame-corpus-manifest", manifest_path)]
    for index, sample in enumerate(samples):
        truth_path = truth_root / cast(str, sample["truth_path"])
        truth = load_strict_json(truth_path)
        expected[cast(str, sample["truth_id"])] = (sample, truth)
        artifacts.append(_artifact(f"frame-truth-{index + 1}", truth_path))
    for index, tape_path in enumerate(event_tapes):
        tape_path = _regular_file_without_links(tape_path, f"event_tapes[{index}]")
        records = EventTape(tape_path).read_all()
        artifacts.append(_artifact(f"event-tape-{index + 1}", tape_path))
        event_count += len(records)
        for record in records:
            payload = record.payload
            truth_id = payload.get("truth_id")
            if not isinstance(truth_id, str) or truth_id not in expected:
                orphan_count += 1
                continue
            sample, truth = expected[truth_id]
            if truth_id in observed:
                mismatch_count += 1
            observed.add(truth_id)
            expected_type = {
                "accepted": "frame.accepted",
                "suppressed": "frame.suppressed",
                "fatal": "frame.fatal",
            }
            if (
                record.session_id != sample["session_id"]
                or record.frame_id != sample["sequence"]
                or record.event_type != expected_type[cast(str, truth["expected_admission"])]
                or not _payload_matches_truth(payload, sample, truth)
            ):
                mismatch_count += 1
    missing_count = len(set(expected) - observed)
    failures: list[str] = []
    if orphan_count:
        failures.append(f"Event Tape contains {orphan_count} orphan record(s)")
    if mismatch_count:
        failures.append(f"Event Tape contains {mismatch_count} binding mismatch(es)")
    if missing_count:
        failures.append(f"Event Tape is missing {missing_count} truth record(s)")
    limitations = [
        "This audit proves frame-ingestion provenance only.",
        "Container PTS values, when present, are extraction locators "
        "rather than capture timing truth.",
        "This component audit does not attest VC-003 hardware identity or a capture-source "
        "provenance artifact; the G1 Candidate packet must bind those separately.",
        "Event Tapes are verified as packet-shaped hash chains here; a hardware profile must "
        "also bind the producer ledger and exact wheel that emitted them.",
    ]
    if profile == "b1_fixture":
        limitations.append(
            "The b1_fixture profile is synthetic/component evidence and does not satisfy the "
            "300-unique-frame, three-independent-session B2 gate."
        )
    if cas_root is None:
        limitations.append(
            "Metadata-only mode did not open raw CAS objects or immutable occurrence records."
        )
    corpus_report: dict[str, Any] = {
        "corpus_digest": manifest["corpus_digest"],
        "source_count": summary["source_count"],
        "session_count": summary["session_count"],
        "independent_session_count": summary["independent_session_count"],
        "sample_count": summary["sample_count"],
        "unique_pixel_count": summary["unique_pixel_count"],
        "independent_fraction_ppm": summary["independent_fraction_ppm"],
        "category_count": summary["category_count"],
        "wrong_size_negative_count": summary["wrong_size_negative_count"],
    }
    # B2 summaries carry explicit evidence that the corpus contains real
    # capture sessions, rather than only satisfying cardinality thresholds.
    # Keep the component/B1 report shape compatible with existing fixtures.
    for field in ("live_session_count", "live_session_sample_count"):
        if field in summary:
            corpus_report[field] = summary[field]

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_id": f"frame-provenance-{manifest['corpus_id']}",
        "report_type": "frame_provenance_audit",
        "verification_profile": profile,
        "generated_at": generated_at,
        "source_commit": manifest["source_commit"],
        "tool_artifact_sha256": _sha256_file(Path(__file__).resolve()),
        "config_sha256": _sha256_file(manifest_path),
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "limitations": limitations,
        "artifacts": artifacts,
        "corpus": corpus_report,
        "event_tape": {
            "tape_count": len(event_tapes),
            "event_count": event_count,
            "chain_valid": True,
            "orphan_count": orphan_count,
            "mismatch_count": mismatch_count,
            "missing_count": missing_count,
        },
        "cas_verification": {
            "mode": "metadata_only" if cas_root is None else "full_cas",
            "verified_object_count": 0 if cas_root is None else len(samples),
        },
        "input_audit": {
            "input_owner": "legacy",
            "connected": False,
            "real_input_call_count": 0,
            "receiver_connect_count": 0,
            "window_write_count": 0,
            "double_write_event_count": 0,
        },
    }
    report["canonical_report_sha256"] = canonical_digest(report)
    _schema_validate(report, schema_path)
    return report


def verify_report(
    report: Mapping[str, Any],
    *,
    manifest_path: Path,
    truth_root: Path,
    event_tapes: Sequence[Path],
    cas_root: Path | None,
    schema_path: Path,
) -> None:
    """Rebuild the report and reject even a correctly re-signed contradiction."""

    _schema_validate(report, schema_path)
    if report.get("canonical_report_sha256") != canonical_digest(
        report, omit=("canonical_report_sha256",)
    ):
        raise FrameCorpusError("canonical_report_sha256 mismatch")
    rebuilt = build_report(
        manifest_path,
        truth_root=truth_root,
        event_tapes=event_tapes,
        cas_root=cas_root,
        generated_at=cast(str, report["generated_at"]),
        schema_path=schema_path,
        profile=cast(str, report.get("verification_profile", "b1_fixture")),
    )
    if dict(report) != rebuilt:
        raise FrameCorpusError("provenance report differs from recomputed evidence")


def _parse_args(argv: list[str] | None = None) -> Any:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--truth-root", type=Path)
    parser.add_argument("--event-tape", action="append", type=Path, required=True)
    parser.add_argument("--cas-root", type=Path)
    parser.add_argument(
        "--require-full-cas",
        action="store_true",
        help="Reject metadata-only mode; required by the B2 candidate profile.",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--verify-report", type=Path)
    parser.add_argument(
        "--schema",
        type=Path,
        default=ROOT / "schemas" / "frame-provenance-audit-report.schema.json",
    )
    parser.add_argument("--generated-at")
    parser.add_argument(
        "--profile",
        choices=("b1_fixture", "b2_gate"),
        default="b1_fixture",
    )
    parser.add_argument("--require-installed", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    package_path = Path(_runtime_package.__file__).resolve()
    if args.require_installed and package_path.is_relative_to(ROOT):
        raise RuntimeError(f"runtime package resolved from checkout: {package_path}")
    manifest = _regular_file_without_links(args.manifest, "manifest")
    truth_root = (
        manifest.parent if args.truth_root is None else args.truth_root.resolve(strict=True)
    )
    tapes = list(args.event_tape)
    cas_root = None if args.cas_root is None else args.cas_root.resolve(strict=True)
    if args.require_full_cas and cas_root is None:
        raise FrameCorpusError("full CAS mode is required by this verification profile")
    schema = args.schema.resolve(strict=True)
    if args.verify_report is not None:
        report = load_strict_json(args.verify_report.resolve(strict=True))
        verify_report(
            report,
            manifest_path=manifest,
            truth_root=truth_root,
            event_tapes=tapes,
            cas_root=cas_root,
            schema_path=schema,
        )
        print("Frame provenance report verified.")
        return 0
    generated_at = args.generated_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    report = build_report(
        manifest,
        truth_root=truth_root,
        event_tapes=tapes,
        cas_root=cas_root,
        generated_at=generated_at,
        schema_path=schema,
        profile=args.profile,
    )
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_bytes(canonical_json(report) + b"\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FrameCorpusError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
