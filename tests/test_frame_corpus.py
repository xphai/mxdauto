from __future__ import annotations

import copy
from hashlib import sha256
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import maple_automation_core.replay.frame_corpus as corpus
from maple_automation_core.capture.frame_source import (
    FrameAdmissionStatus,
    FrameSourceAdapter,
    FrameSourceConfig,
    RawFrame,
)
from maple_automation_core.capture.pixel_store import PixelStore, canonical_json
from maple_automation_core.domain.frame import FrameSize, SourceGeometry, SourceRect
from maple_automation_core.replay.event_tape import EventTape
from maple_automation_core.replay.frame_corpus import (
    FrameCorpusError,
    _safe_relative,
    append_admission_to_event_tape,
    canonical_digest,
    load_strict_json,
    public_privacy_summary,
    verify_corpus_file,
)
from tools.audit_frame_provenance import build_report, verify_report
from tools.import_frame_corpus import build_corpus

ROOT = Path(__file__).resolve().parents[1]
TRUTH_SCHEMA = ROOT / "schemas" / "frame-truth.schema.json"
MANIFEST_SCHEMA = ROOT / "schemas" / "frame-corpus-manifest.schema.json"
AUDIT_SCHEMA = ROOT / "schemas" / "frame-provenance-audit-report.schema.json"
ZERO_SHA = "0" * 64


class _NullSource:
    def read(self) -> RawFrame | None:
        return None


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(payload) + b"\n")


def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _make_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    private = tmp_path / "private"
    private.mkdir(parents=True)
    source_paths: list[Path] = []
    raw_paths: list[Path] = []
    for index in range(3):
        source = private / f"source-{index}.bin"
        source.write_bytes(f"synthetic-source-{index}".encode())
        source_paths.append(source)
        raw = private / f"sample-{index}.bgr"
        raw.write_bytes(bytes([index + 1, 2, 3, 4, 5, 6]))
        raw_paths.append(raw)

    plan = {
        "schema_version": "1.0.0",
        "corpus_id": "synthetic-ingestion-v1",
        "created_at": "2026-08-29T00:00:00Z",
        "source_commit": "a" * 40,
        "sources": [
            {
                "source_id": f"source-{index}",
                "source_path": str(source_paths[index]),
                "expected_sha256": _sha(source_paths[index]),
                "locator_kind": "raw_fixture",
                "license_id": "synthetic-fixture",
                "privacy_class": "restricted",
                "timing_truth": False,
            }
            for index in range(3)
        ],
        "sessions": [
            {
                "session_id": f"session-{index}",
                "source_id": f"source-{index}",
                "split": ("train", "validation", "test")[index],
                "independent": True,
            }
            for index in range(3)
        ],
        "samples": [
            {
                "sample_id": f"sample-{index}",
                "truth_id": f"truth-{index}",
                "session_id": f"session-{index}",
                "sequence": index,
                "raw_path": str(raw_paths[index]),
                "pixel_spec": {
                    "channels": 3,
                    "dtype": "uint8",
                    "height": 1,
                    "length": 6,
                    "pixel_format": "BGR8",
                    "stride": 6,
                    "width": 2,
                },
                "expected_admission": "fatal" if index == 2 else "accepted",
                "expected_status": "frame_size_changed" if index == 2 else "accepted",
                "expected_reason_code": "frame_size_changed" if index == 2 else "accepted",
                "category": ("static", "motion", "wrong_size")[index],
                "wrong_size_negative": index == 2,
                "source_locator": {
                    "kind": "frame_index",
                    "value": index,
                    "timing_truth": False,
                },
                "privacy_class": "restricted",
                "retention_class": "candidate",
                "transform_version": "synthetic-v1",
                "calibration_sha256": ZERO_SHA,
                "primary_reviewer_id": "reviewer-primary",
                "independent_reviewer_id": "reviewer-independent" if index == 0 else None,
                "independent_decision": "confirmed" if index == 0 else None,
                "adjudication_id": None,
            }
            for index in range(3)
        ],
        "limitations": ["Synthetic and de-identified ingestion fixture only."],
    }
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, plan)
    output_root = tmp_path / "public"
    cas_root = tmp_path / "private-cas"
    manifest = build_corpus(
        plan_path,
        output_root,
        cas_root,
        truth_schema=TRUTH_SCHEMA,
        manifest_schema=MANIFEST_SCHEMA,
    )
    return manifest, output_root, cas_root


def _set_parent_digests(
    manifest_path: Path,
    output_root: Path,
    parent_by_sample: dict[int, int | str | None],
) -> None:
    """Re-sign selected truth rows and their manifest for graph regressions."""

    payload = load_strict_json(manifest_path)
    for child_index, parent in parent_by_sample.items():
        sample = payload["samples"][child_index]
        truth_path = output_root / sample["truth_path"]
        truth = load_strict_json(truth_path)
        if type(parent) is int:
            parent_digest = payload["samples"][parent]["pixel_digest"]
        else:
            parent_digest = parent
        truth["derivation"]["parent_pixel_digest"] = parent_digest
        truth["record_digest"] = canonical_digest(truth, omit=("record_digest",))
        _write_json(truth_path, truth)
        sample["truth_sha256"] = _sha(truth_path)

    truths = [load_strict_json(output_root / sample["truth_path"]) for sample in payload["samples"]]
    payload["privacy_summary"] = public_privacy_summary(payload, truths)
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest_path, payload)


def _make_tapes(manifest_path: Path, output_root: Path) -> list[Path]:
    manifest = load_strict_json(manifest_path)
    tape_paths: list[Path] = []
    for index, sample in enumerate(manifest["samples"]):
        truth = load_strict_json(output_root / sample["truth_path"])
        path = output_root / f"session-{index}.jsonl"
        tape = EventTape(path)
        event_type = {
            "accepted": "frame.accepted",
            "suppressed": "frame.suppressed",
            "fatal": "frame.fatal",
        }[truth["expected_admission"]]
        tape.append(
            event_type=event_type,
            payload={
                "truth_scope": "frame_ingestion_only",
                "truth_id": sample["truth_id"],
                "truth_pixel_digest": sample["pixel_digest"],
                "admission_status": (
                    "accepted"
                    if truth["expected_admission"] == "accepted"
                    else "frame_size_changed"
                ),
                "plan_suppressed": truth["expected_admission"] != "accepted",
                "fault_latched": truth["expected_admission"] == "fatal",
                "pixel_digest": (
                    sample["pixel_digest"] if truth["expected_admission"] == "accepted" else None
                ),
                "image_ref": (
                    sample["cas_ref"] if truth["expected_admission"] == "accepted" else None
                ),
                "reason": (
                    "frame admitted"
                    if truth["expected_admission"] == "accepted"
                    else "source frame size changed"
                ),
                "reason_code": (
                    "accepted"
                    if truth["expected_admission"] == "accepted"
                    else "frame_size_changed"
                ),
            },
            session_id=sample["session_id"],
            frame_id=sample["sequence"],
            world_state_version=0,
            recorded_at_ns=100 + index,
        )
        tape_paths.append(path)
    return tape_paths


def test_import_full_cas_truth_manifest_and_split_verification(tmp_path: Path) -> None:
    manifest, output_root, cas_root = _make_fixture(tmp_path)
    summary = verify_corpus_file(
        manifest,
        truth_root=output_root,
        cas_root=cas_root,
        minimum_samples=3,
        minimum_sessions=3,
        required_independent_fraction_ppm=200_000,
    )
    assert summary == {
        "status": "PASS",
        "source_count": 3,
        "session_count": 3,
        "independent_session_count": 3,
        "sample_count": 3,
        "unique_pixel_count": 3,
        "primary_reviewed": 3,
        "independent_reviewed": 1,
        "independent_fraction_ppm": 333_333,
        "category_count": 3,
        "wrong_size_negative_count": 1,
    }
    payload = load_strict_json(manifest)
    assert all("source_path" not in source for source in payload["sources"])
    assert payload["privacy_summary"]["raw_artifacts_public"] is False
    assert (
        list(
            Draft202012Validator(
                load_strict_json(MANIFEST_SCHEMA),
                format_checker=FormatChecker(),
            ).iter_errors(payload)
        )
        == []
    )


def test_manifest_rejects_orphan_parent_pixel_digest(tmp_path: Path) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    _set_parent_digests(manifest, output_root, {0: "f" * 64})

    with pytest.raises(
        FrameCorpusError, match="orphan.*parent_pixel_digest|parent_pixel_digest.*manifest"
    ):
        verify_corpus_file(manifest, truth_root=output_root)


def test_manifest_rejects_two_node_pixel_derivation_cycle(tmp_path: Path) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    _set_parent_digests(manifest, output_root, {0: 1, 1: 0})

    with pytest.raises(FrameCorpusError, match="cycle"):
        verify_corpus_file(manifest, truth_root=output_root)


def test_manifest_rejects_three_node_pixel_derivation_cycle(tmp_path: Path) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    _set_parent_digests(manifest, output_root, {0: 1, 1: 2, 2: 0})

    with pytest.raises(FrameCorpusError, match="cycle"):
        verify_corpus_file(manifest, truth_root=output_root)


def test_truth_schema_and_semantics_reject_extra_keys_and_pts_timing(tmp_path: Path) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    payload = load_strict_json(manifest)
    truth_path = output_root / payload["samples"][0]["truth_path"]
    truth = load_strict_json(truth_path)
    truth["unexpected"] = True
    truth["record_digest"] = canonical_digest(truth, omit=("record_digest",))
    _write_json(truth_path, truth)
    payload["samples"][0]["truth_sha256"] = _sha(truth_path)
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)
    with pytest.raises(FrameCorpusError, match="unexpected key"):
        verify_corpus_file(manifest, truth_root=output_root)

    manifest, output_root, _ = _make_fixture(tmp_path / "pts")
    payload = load_strict_json(manifest)
    truth_path = output_root / payload["samples"][0]["truth_path"]
    truth = load_strict_json(truth_path)
    truth["source_locator"] = {"kind": "pts_locator", "value": "pts:1", "timing_truth": True}
    truth["record_digest"] = canonical_digest(truth, omit=("record_digest",))
    _write_json(truth_path, truth)
    payload["samples"][0]["truth_sha256"] = _sha(truth_path)
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)
    with pytest.raises(FrameCorpusError, match="not capture timing truth"):
        verify_corpus_file(manifest, truth_root=output_root)


def test_manifest_rejects_resigned_cross_split_duplicate_and_truth_tamper(
    tmp_path: Path,
) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    payload = load_strict_json(manifest)
    payload["samples"][1]["pixel_digest"] = payload["samples"][0]["pixel_digest"]
    payload["samples"][1]["cas_ref"] = payload["samples"][0]["cas_ref"]
    truth_path = output_root / payload["samples"][1]["truth_path"]
    truth = load_strict_json(truth_path)
    truth["pixel_digest"] = payload["samples"][0]["pixel_digest"]
    truth["cas_ref"] = payload["samples"][0]["cas_ref"]
    truth["record_digest"] = canonical_digest(truth, omit=("record_digest",))
    _write_json(truth_path, truth)
    payload["samples"][1]["truth_sha256"] = _sha(truth_path)
    truths = [load_strict_json(output_root / sample["truth_path"]) for sample in payload["samples"]]
    payload["privacy_summary"] = public_privacy_summary(payload, truths)
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)
    with pytest.raises(FrameCorpusError, match="overlap across splits"):
        verify_corpus_file(manifest, truth_root=output_root)


def test_full_verifier_detects_corrupt_private_cas(tmp_path: Path) -> None:
    manifest, output_root, cas_root = _make_fixture(tmp_path)
    payload = load_strict_json(manifest)
    digest = payload["samples"][0]["pixel_digest"]
    PixelStore(cas_root).path_for(digest).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="length"):
        verify_corpus_file(manifest, truth_root=output_root, cas_root=cas_root)


def test_b2_gate_cannot_be_satisfied_by_synthetic_three_sample_fixture(
    tmp_path: Path,
) -> None:
    manifest, output_root, cas_root = _make_fixture(tmp_path)
    with pytest.raises(FrameCorpusError, match="fewer sessions|fewer samples"):
        verify_corpus_file(
            manifest,
            truth_root=output_root,
            cas_root=cas_root,
            profile="b2_gate",
        )
    tapes = _make_tapes(manifest, output_root)
    with pytest.raises(FrameCorpusError, match="fewer sessions|fewer samples"):
        build_report(
            manifest,
            truth_root=output_root,
            event_tapes=tapes,
            cas_root=cas_root,
            generated_at="2026-08-29T00:00:00Z",
            schema_path=AUDIT_SCHEMA,
            profile="b2_gate",
        )


def test_provenance_audit_recomputes_event_tape_and_rejects_resigned_tamper(
    tmp_path: Path,
) -> None:
    manifest, output_root, cas_root = _make_fixture(tmp_path)
    tapes = _make_tapes(manifest, output_root)
    report = build_report(
        manifest,
        truth_root=output_root,
        event_tapes=tapes,
        cas_root=cas_root,
        generated_at="2026-08-29T00:00:00Z",
        schema_path=AUDIT_SCHEMA,
    )
    assert report["status"] == "PASS"
    assert report["event_tape"] == {
        "tape_count": 3,
        "event_count": 3,
        "chain_valid": True,
        "orphan_count": 0,
        "mismatch_count": 0,
        "missing_count": 0,
    }
    verify_report(
        report,
        manifest_path=manifest,
        truth_root=output_root,
        event_tapes=tapes,
        cas_root=cas_root,
        schema_path=AUDIT_SCHEMA,
    )

    tampered = copy.deepcopy(report)
    tampered["corpus"]["sample_count"] = 999
    tampered["canonical_report_sha256"] = canonical_digest(
        tampered, omit=("canonical_report_sha256",)
    )
    with pytest.raises(FrameCorpusError, match="differs from recomputed"):
        verify_report(
            tampered,
            manifest_path=manifest,
            truth_root=output_root,
            event_tapes=tapes,
            cas_root=cas_root,
            schema_path=AUDIT_SCHEMA,
        )


def test_strict_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    target = tmp_path / "duplicate.json"
    target.write_text('{"schema_version":"1.0.0","schema_version":"1.0.0"}', encoding="utf-8")
    with pytest.raises(FrameCorpusError, match="duplicate JSON key"):
        load_strict_json(target)


def test_truth_path_rejects_lexical_symlink_before_resolution(tmp_path: Path) -> None:
    root = tmp_path / "truth-root"
    root.mkdir()
    target = root / "truth.json"
    target.write_text("{}", encoding="utf-8")
    link = root / "truth-link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this host")
    with pytest.raises(FrameCorpusError, match="symlink/reparse"):
        _safe_relative(root, link.name, "truth_path")


def test_audit_rejects_event_tape_symlink_before_resolution(tmp_path: Path) -> None:
    manifest, output_root, cas_root = _make_fixture(tmp_path)
    tapes = _make_tapes(manifest, output_root)
    link = output_root / "event-link.jsonl"
    try:
        link.symlink_to(tapes[0])
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this host")
    with pytest.raises(FrameCorpusError, match="symlink/reparse"):
        build_report(
            manifest,
            truth_root=output_root,
            event_tapes=[link, *tapes[1:]],
            cas_root=cas_root,
            generated_at="2026-08-29T00:00:00Z",
            schema_path=AUDIT_SCHEMA,
        )


def test_audit_fails_closed_on_orphan_event(tmp_path: Path) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    tapes = _make_tapes(manifest, output_root)
    orphan = output_root / "orphan.jsonl"
    EventTape(orphan).append(
        event_type="frame.accepted",
        payload={
            "truth_scope": "frame_ingestion_only",
            "truth_id": "truth-orphan",
            "truth_pixel_digest": ZERO_SHA,
        },
        session_id="session-orphan",
        frame_id=0,
        world_state_version=0,
        recorded_at_ns=1,
    )
    report = build_report(
        manifest,
        truth_root=output_root,
        event_tapes=[*tapes, orphan],
        cas_root=None,
        generated_at="2026-08-29T00:00:00Z",
        schema_path=AUDIT_SCHEMA,
    )
    assert report["status"] == "FAIL"
    assert report["event_tape"]["orphan_count"] == 1


def test_official_admission_mapper_binds_truth_pixel_and_event_chain(tmp_path: Path) -> None:
    geometry = SourceGeometry(
        source_size=FrameSize(width=2, height=1),
        content_rect=SourceRect(x=0, y=0, width=2, height=1),
        working_size=FrameSize(width=2, height=1),
    )
    config = FrameSourceConfig(
        session_id="session-map",
        source_id="source-map",
        clock_domain="monotonic",
        transform_version="capture-v1",
        source_geometry=geometry,
        max_age_ns=100,
    )
    digest = "a" * 64
    adapter = FrameSourceAdapter(_NullSource(), config)
    result = adapter.ingest(
        RawFrame(
            source_id="source-map",
            session_id="session-map",
            frame_id=1,
            captured_at_ns=10,
            clock_domain="monotonic",
            transform_version="capture-v1",
            source_geometry=geometry,
            content_hash=digest,
            image_ref=f"cas://sha256/{digest}",
        ),
        received_at_ns=11,
    )
    tape = EventTape(tmp_path / "mapped.jsonl")
    record = append_admission_to_event_tape(
        tape,
        result,
        truth_id="truth-map",
        truth_pixel_digest=digest,
    )
    assert record.event_type == "frame.accepted"
    assert record.payload["truth_pixel_digest"] == digest
    assert EventTape(tape.path).read_all() == (record,)

    with pytest.raises(FrameCorpusError, match="does not match ingestion truth"):
        append_admission_to_event_tape(
            tape,
            result,
            truth_id="truth-map-2",
            truth_pixel_digest="b" * 64,
        )


def test_resigned_locator_host_path_and_invalid_timestamp_are_rejected(tmp_path: Path) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    payload = load_strict_json(manifest)
    truth_path = output_root / payload["samples"][0]["truth_path"]
    truth = load_strict_json(truth_path)
    truth["source_locator"]["value"] = r"F:\Users\Alice\private\frame.bgr"
    truth["record_digest"] = canonical_digest(truth, omit=("record_digest",))
    _write_json(truth_path, truth)
    payload["samples"][0]["truth_sha256"] = _sha(truth_path)
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)
    with pytest.raises(FrameCorpusError, match="source_locator.value"):
        verify_corpus_file(manifest, truth_root=output_root)

    manifest, output_root, _ = _make_fixture(tmp_path / "date")
    payload = load_strict_json(manifest)
    payload["created_at"] = "not-a-date"
    truths = [load_strict_json(output_root / sample["truth_path"]) for sample in payload["samples"]]
    payload["privacy_summary"] = public_privacy_summary(payload, truths)
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)
    with pytest.raises(FrameCorpusError, match="ISO-8601"):
        verify_corpus_file(manifest, truth_root=output_root)


def test_public_private_tree_overlap_and_privacy_scan_fail_before_publish(tmp_path: Path) -> None:
    fixture = ROOT / "fixtures" / "g1" / "frame_corpus_synthetic_v1"
    with pytest.raises(FrameCorpusError, match="must not overlap"):
        build_corpus(
            fixture / "import-plan.json",
            tmp_path / "public",
            tmp_path / "public" / "private-cas",
            truth_schema=TRUTH_SCHEMA,
            manifest_schema=MANIFEST_SCHEMA,
            private_root=fixture,
        )

    plan = load_strict_json(fixture / "import-plan.json")
    plan["limitations"].append(r"Captured under F:\Users\Alice\private\account-123")
    plan_path = tmp_path / "private-plan.json"
    _write_json(plan_path, plan)
    public = tmp_path / "scanned-public"
    with pytest.raises(FrameCorpusError, match="privacy scan"):
        build_corpus(
            plan_path,
            public,
            tmp_path / "private-cas",
            truth_schema=TRUTH_SCHEMA,
            manifest_schema=MANIFEST_SCHEMA,
            private_root=fixture,
        )
    assert not public.exists()


def test_independent_session_threshold_and_session_sequence_uniqueness(tmp_path: Path) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    payload = load_strict_json(manifest)
    for session in payload["sessions"]:
        session["independent"] = False
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)
    with pytest.raises(FrameCorpusError, match="independent source sessions"):
        verify_corpus_file(
            manifest,
            truth_root=output_root,
            minimum_independent_sessions=3,
        )

    manifest, output_root, _ = _make_fixture(tmp_path / "duplicate-sequence")
    payload = load_strict_json(manifest)
    payload["samples"][1]["session_id"] = payload["samples"][0]["session_id"]
    payload["samples"][1]["sequence"] = payload["samples"][0]["sequence"]
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)
    with pytest.raises(FrameCorpusError, match="session_id, sequence"):
        verify_corpus_file(manifest, truth_root=output_root)


def test_audit_rejects_resigned_semantically_contradictory_packet_payload(
    tmp_path: Path,
) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    tapes = _make_tapes(manifest, output_root)
    payload = load_strict_json(manifest)
    sample = payload["samples"][0]
    bad_tape = output_root / "bad-accepted.jsonl"
    EventTape(bad_tape).append(
        event_type="frame.accepted",
        payload={
            "truth_scope": "frame_ingestion_only",
            "truth_id": sample["truth_id"],
            "truth_pixel_digest": sample["pixel_digest"],
            "admission_status": "accepted",
            "plan_suppressed": False,
            "fault_latched": True,
            "pixel_digest": None,
            "image_ref": None,
            "reason": "contradictory but re-chained",
            "reason_code": "accepted",
        },
        session_id=sample["session_id"],
        frame_id=sample["sequence"],
        world_state_version=0,
        recorded_at_ns=100,
    )
    report = build_report(
        manifest,
        truth_root=output_root,
        event_tapes=[bad_tape, *tapes[1:]],
        cas_root=None,
        generated_at="2026-08-29T00:00:00Z",
        schema_path=AUDIT_SCHEMA,
    )
    assert report["status"] == "FAIL"
    assert report["event_tape"]["mismatch_count"] == 1


def test_strict_json_and_scalar_validators_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(FrameCorpusError, match="missing|symlink|file"):
        load_strict_json(missing)
    directory = tmp_path / "directory.json"
    directory.mkdir()
    with pytest.raises(FrameCorpusError, match="missing|symlink|file"):
        load_strict_json(directory)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(FrameCorpusError, match="invalid strict JSON"):
        load_strict_json(invalid)
    scalar = tmp_path / "scalar.json"
    scalar.write_text("[]", encoding="utf-8")
    with pytest.raises(FrameCorpusError, match="must be an object"):
        load_strict_json(scalar)
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(FrameCorpusError, match="non-standard JSON constant"):
        load_strict_json(nonfinite)

    with pytest.raises(FrameCorpusError, match="missing key"):
        corpus._require_exact_keys({}, {"required"}, "object")
    with pytest.raises(FrameCorpusError, match="unexpected key"):
        corpus._require_exact_keys({"extra": 1}, set(), "object")
    with pytest.raises(FrameCorpusError, match="must be an object"):
        corpus._mapping([], "mapping")
    with pytest.raises(FrameCorpusError, match="must be an array"):
        corpus._array({}, "array")
    with pytest.raises(FrameCorpusError, match="non-empty"):
        corpus._text("", "text")
    with pytest.raises(FrameCorpusError, match="integer"):
        corpus._integer(True, "integer")
    with pytest.raises(FrameCorpusError, match="boolean"):
        corpus._boolean(1, "boolean")
    with pytest.raises(FrameCorpusError, match="lowercase hexadecimal"):
        corpus._hex("A" * 64, "digest")
    with pytest.raises(FrameCorpusError, match="hexadecimal"):
        corpus._hex("g" * 64, "digest")
    with pytest.raises(FrameCorpusError, match="portable identifier"):
        corpus._identifier("contains space", "identifier")

    with pytest.raises(FrameCorpusError, match="ending in Z"):
        corpus._utc_timestamp("2026-08-29T00:00:00+00:00", "created_at")
    with pytest.raises(FrameCorpusError, match="valid ISO-8601"):
        corpus._utc_timestamp("not-a-dateZ", "created_at")


def test_privacy_scan_recurses_nested_values_without_disclosing_matches() -> None:
    manifest = {"corpus_id": "synthetic", "nested": {"values": ["/home/private", "ok"]}}
    truth = {"truth_id": "truth-1", "note": r"C:\Users\Alice\frame.bgr"}
    summary = corpus.public_privacy_summary(manifest, [truth])
    assert summary["pii_findings"] == 2
    assert summary["scanned_json_count"] == 2
    assert summary["public_mode"] == "hash_only"
    assert "home" not in str(summary)


def test_safe_relative_rejects_missing_directory_symlink_and_non_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(FrameCorpusError, match="does not exist"):
        corpus._safe_relative(root, "missing.json", "truth_path")

    nested = root / "nested"
    nested.mkdir()
    with pytest.raises(FrameCorpusError, match="regular file"):
        corpus._safe_relative(root, "nested", "truth_path")

    target = root / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = root / "nested-link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this host")
    with pytest.raises(FrameCorpusError, match="symlink/reparse"):
        corpus._safe_relative(root, "nested-link", "truth_path")


def test_truth_record_semantics_cover_admission_derivation_privacy_and_review(
    tmp_path: Path,
) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    payload = load_strict_json(manifest)
    truth_path = output_root / payload["samples"][0]["truth_path"]
    truth = load_strict_json(truth_path)
    assert corpus.verify_truth_record(truth).length == 6

    def candidate(mutator: object) -> dict[str, object]:
        value = copy.deepcopy(truth)
        mutator(value)  # type: ignore[operator]
        return value

    cases: list[tuple[str, object]] = [
        ("schema", lambda p: p.update({"schema_version": "0.0.0"})),
        ("scope", lambda p: p.update({"truth_scope": "world_state"})),
        ("locator kind", lambda p: p["source_locator"].update({"kind": "unknown"})),  # type: ignore[index]
        (
            "PTS token",
            lambda p: p["source_locator"].update(  # type: ignore[index]
                {"kind": "pts_locator", "value": "not-pts", "timing_truth": False}
            ),
        ),
        ("pixel spec", lambda p: p.update({"pixel_spec": {"bad": True}})),
        ("CAS binding", lambda p: p.update({"cas_ref": "cas://sha256/" + "0" * 64})),
        ("admission", lambda p: p.update({"expected_admission": "unknown"})),
        ("reason code", lambda p: p.update({"expected_reason_code": "other"})),
        (
            "status contradiction",
            lambda p: p.update(
                {
                    "expected_status": "frame_size_changed",
                    "expected_reason_code": "frame_size_changed",
                }
            ),
        ),
        ("category", lambda p: p.update({"category": "unknown"})),
        ("wrong-size category", lambda p: p.update({"wrong_size_negative": True})),
        (
            "wrong-size admission",
            lambda p: p.update(
                {
                    "wrong_size_negative": True,
                    "category": "wrong_size",
                    "expected_admission": "accepted",
                }
            ),
        ),
        (
            "wrong-size status",
            lambda p: p.update(
                {
                    "wrong_size_negative": True,
                    "category": "wrong_size",
                    "expected_admission": "fatal",
                    "expected_status": "source_error",
                    "expected_reason_code": "source_error",
                }
            ),
        ),
        (
            "hash-only derivative",
            lambda p: p["derivation"].update({"redaction_artifact_sha256": "0" * 64}),  # type: ignore[index]
        ),
        (
            "redaction mode",
            lambda p: p["derivation"].update({"redaction_mode": "unknown"}),  # type: ignore[index]
        ),
        (
            "privacy class",
            lambda p: p["privacy"].update({"class": "unknown"}),  # type: ignore[index]
        ),
        (
            "privacy retention",
            lambda p: p["privacy"].update({"retention": "unknown"}),  # type: ignore[index]
        ),
        (
            "primary review",
            lambda p: p["review"].update({"primary_decision": "rejected"}),  # type: ignore[index]
        ),
        (
            "independent pair",
            lambda p: p["review"].update({"independent_reviewer_id": None}),  # type: ignore[index]
        ),
        (
            "independent id",
            lambda p: p["review"].update({"independent_reviewer_id": "bad id"}),  # type: ignore[index]
        ),
        (
            "same reviewer masquerading as independent",
            lambda p: p["review"].update(  # type: ignore[index]
                {
                    "independent_reviewer_id": "reviewer-primary",
                    "independent_decision": "confirmed",
                }
            ),
        ),
        (
            "independent decision",
            lambda p: p["review"].update({"independent_decision": "unknown"}),  # type: ignore[index]
        ),
        (
            "adjudication",
            lambda p: p["review"].update({"adjudication_id": "unused"}),  # type: ignore[index]
        ),
        ("record digest", lambda p: p.update({"record_digest": "0" * 64})),
    ]
    for _name, mutator in cases:
        with pytest.raises(FrameCorpusError, match=".*"):
            corpus.verify_truth_record(candidate(mutator))  # type: ignore[arg-type]

    applied = copy.deepcopy(truth)
    applied["derivation"].update(  # type: ignore[index]
        {
            "redaction_mode": "applied_deidentified_derivative",
            "redaction_artifact_sha256": "a" * 64,
            "deidentified_derivative_sha256": "b" * 64,
        }
    )
    applied["review"].update(  # type: ignore[index]
        {
            "independent_decision": "disputed_then_adjudicated",
            "adjudication_id": "adjudication-1",
        }
    )
    applied["record_digest"] = canonical_digest(applied, omit=("record_digest",))
    assert corpus.verify_truth_record(applied).length == 6


def test_manifest_shape_graph_and_file_profile_guards(tmp_path: Path) -> None:
    manifest, output_root, cas_root = _make_fixture(tmp_path)
    payload = load_strict_json(manifest)
    corpus._verify_manifest_shape(payload)

    for candidate in (
        {**payload, "schema_version": "0.0.0"},
        {**payload, "truth_scope": "world_state"},
        {**payload, "corpus_digest": ZERO_SHA},
    ):
        with pytest.raises(FrameCorpusError, match=".*"):
            corpus._verify_manifest_shape(candidate)

    # The iterative DFS marks completed parents and skips an already explored
    # branch; this graph is acyclic but shares the parent pixel object.
    corpus._verify_pixel_derivation_graph(
        {"pixel-a", "pixel-b", "pixel-c"},
        {"pixel-a": {"pixel-b"}, "pixel-c": {"pixel-b"}},
    )

    with pytest.raises(FrameCorpusError, match="profile"):
        corpus.verify_corpus_file(manifest, truth_root=output_root, profile="unknown")
    with pytest.raises(FrameCorpusError, match="full CAS"):
        corpus.verify_corpus_file(manifest, truth_root=output_root, profile="b2_gate")

    manifest.write_text(manifest.read_text(encoding="utf-8").rstrip() + " \n", encoding="utf-8")
    with pytest.raises(FrameCorpusError, match="canonical JSON"):
        corpus.verify_corpus_file(manifest, truth_root=output_root, cas_root=cas_root)


def test_manifest_shape_and_session_source_guards_are_semantic(tmp_path: Path) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    original = load_strict_json(manifest)

    def resign(value: dict[str, object]) -> dict[str, object]:
        candidate = copy.deepcopy(value)
        candidate["corpus_digest"] = canonical_digest(candidate, omit=("corpus_digest",))
        return candidate

    cases: list[tuple[str, dict[str, object]]] = []
    duplicate_source = copy.deepcopy(original)
    duplicate_source["sources"][1]["source_id"] = duplicate_source["sources"][0]["source_id"]  # type: ignore[index]
    cases.append(("duplicate source", resign(duplicate_source)))

    unsupported_source = copy.deepcopy(original)
    unsupported_source["sources"][0]["locator_kind"] = "other"  # type: ignore[index]
    cases.append(("source locator", resign(unsupported_source)))

    privacy_source = copy.deepcopy(original)
    privacy_source["sources"][0]["privacy_class"] = "other"  # type: ignore[index]
    cases.append(("source privacy", resign(privacy_source)))

    video_timing = copy.deepcopy(original)
    video_timing["sources"][0]["locator_kind"] = "video_container"  # type: ignore[index]
    video_timing["sources"][0]["timing_truth"] = True  # type: ignore[index]
    cases.append(("video timing", resign(video_timing)))

    duplicate_session = copy.deepcopy(original)
    duplicate_session["sessions"][1]["session_id"] = duplicate_session["sessions"][0]["session_id"]  # type: ignore[index]
    cases.append(("duplicate session", resign(duplicate_session)))

    unknown_source = copy.deepcopy(original)
    unknown_source["sessions"][0]["source_id"] = "source-unknown"  # type: ignore[index]
    cases.append(("unknown source", resign(unknown_source)))

    unsupported_split = copy.deepcopy(original)
    unsupported_split["sessions"][0]["split"] = "other"  # type: ignore[index]
    cases.append(("session split", resign(unsupported_split)))

    for _name, candidate in cases:
        with pytest.raises(FrameCorpusError, match=".*"):
            corpus.verify_corpus_manifest(candidate, truth_root=output_root)

    with pytest.raises(FrameCorpusError, match="<= 1000000"):
        corpus.verify_corpus_manifest(
            original,
            truth_root=output_root,
            required_independent_fraction_ppm=1_000_001,
        )
    with pytest.raises(FrameCorpusError, match="fewer sessions"):
        corpus.verify_corpus_manifest(original, truth_root=output_root, minimum_sessions=4)
    with pytest.raises(FrameCorpusError, match="independent source sessions"):
        corpus.verify_corpus_manifest(
            original,
            truth_root=output_root,
            minimum_independent_sessions=4,
        )

    duplicate_split = copy.deepcopy(original)
    duplicate_split["splits"]["validation"].append("session-0")  # type: ignore[index]
    with pytest.raises(FrameCorpusError, match="more than one split"):
        corpus.verify_corpus_manifest(resign(duplicate_split), truth_root=output_root)

    disagree_split = copy.deepcopy(original)
    disagree_split["splits"]["train"] = ["session-1"]  # type: ignore[index]
    disagree_split["splits"]["validation"] = []  # type: ignore[index]
    with pytest.raises(FrameCorpusError, match="split listing disagrees"):
        corpus.verify_corpus_manifest(resign(disagree_split), truth_root=output_root)

    missing_split = copy.deepcopy(original)
    missing_split["splits"]["test"] = []  # type: ignore[index]
    with pytest.raises(FrameCorpusError, match="every session"):
        corpus.verify_corpus_manifest(resign(missing_split), truth_root=output_root)

    with pytest.raises(FrameCorpusError, match="truth_root"):
        corpus.verify_corpus_manifest(original, truth_root=tmp_path / "does-not-exist")


def test_manifest_sample_binding_and_review_threshold_guards(tmp_path: Path) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    original = load_strict_json(manifest)

    def resign(value: dict[str, object]) -> dict[str, object]:
        candidate = copy.deepcopy(value)
        candidate["corpus_digest"] = canonical_digest(candidate, omit=("corpus_digest",))
        return candidate

    duplicate = copy.deepcopy(original)
    duplicate["samples"][1]["sample_id"] = duplicate["samples"][0]["sample_id"]  # type: ignore[index]
    unknown_session = copy.deepcopy(original)
    unknown_session["samples"][0]["session_id"] = "session-unknown"  # type: ignore[index]
    bad_ref = copy.deepcopy(original)
    bad_ref["samples"][0]["cas_ref"] = "cas://sha256/" + ZERO_SHA  # type: ignore[index]
    bad_truth_hash = copy.deepcopy(original)
    bad_truth_hash["samples"][0]["truth_sha256"] = ZERO_SHA  # type: ignore[index]
    bad_binding = copy.deepcopy(original)
    bad_binding["samples"][0]["category"] = "motion"  # type: ignore[index]
    for candidate, _message in (
        (duplicate, "duplicate sample"),
        (unknown_session, "unknown session"),
        (bad_ref, "cas_ref"),
        (bad_truth_hash, "truth artifact SHA"),
        (bad_binding, "binding mismatch"),
    ):
        with pytest.raises(FrameCorpusError, match=".*"):
            corpus.verify_corpus_manifest(resign(candidate), truth_root=output_root)

    with pytest.raises(FrameCorpusError, match="fewer samples"):
        corpus.verify_corpus_manifest(original, truth_root=output_root, minimum_samples=4)
    with pytest.raises(FrameCorpusError, match="unique pixel"):
        corpus.verify_corpus_manifest(original, truth_root=output_root, minimum_unique_pixels=4)
    with pytest.raises(FrameCorpusError, match="independent review fraction"):
        corpus.verify_corpus_manifest(
            original,
            truth_root=output_root,
            required_independent_fraction_ppm=400_000,
        )

    review = copy.deepcopy(original)
    review["review_summary"]["primary_reviewed"] = 99  # type: ignore[index]
    with pytest.raises(FrameCorpusError, match="review_summary"):
        corpus.verify_corpus_manifest(resign(review), truth_root=output_root)


def test_truth_manifest_cross_links_are_rechecked_after_resigning(tmp_path: Path) -> None:
    def prepare(name: str, truth_mutator: object, sample_mutator: object | None = None) -> None:
        manifest, output_root, _ = _make_fixture(tmp_path / name)
        payload = load_strict_json(manifest)
        sample = payload["samples"][0]
        truth_path = output_root / sample["truth_path"]
        truth = load_strict_json(truth_path)
        truth_mutator(truth)  # type: ignore[operator]
        truth["record_digest"] = canonical_digest(truth, omit=("record_digest",))
        _write_json(truth_path, truth)
        sample["truth_sha256"] = _sha(truth_path)
        if sample_mutator is not None:
            sample_mutator(sample)
        truths = [load_strict_json(output_root / item["truth_path"]) for item in payload["samples"]]
        payload["privacy_summary"] = public_privacy_summary(payload, truths)
        payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
        _write_json(manifest, payload)
        with pytest.raises(FrameCorpusError, match=".*"):
            verify_corpus_file(manifest, truth_root=output_root)

    prepare("source-id", lambda truth: truth.update({"source_id": "source-1"}))
    prepare(
        "locator-kind",
        lambda truth: truth["source_locator"].update(  # type: ignore[index]
            {"kind": "live_sequence", "value": 0}
        ),
    )
    prepare(
        "timing-truth",
        lambda truth: truth["source_locator"].update({"timing_truth": True}),  # type: ignore[index]
    )
    prepare(
        "provenance",
        lambda truth: truth.update({"source_provenance_id": "f" * 64}),
        lambda sample: sample.update({"source_provenance_id": "f" * 64}),
    )
    prepare(
        "source-hash",
        lambda truth: truth["derivation"].update({"source_artifact_sha256": ZERO_SHA}),  # type: ignore[index]
    )


def test_event_tape_mapper_emits_suppressed_and_fatal_records(tmp_path: Path) -> None:
    geometry = SourceGeometry(
        source_size=FrameSize(width=2, height=1),
        content_rect=SourceRect(x=0, y=0, width=2, height=1),
        working_size=FrameSize(width=2, height=1),
    )
    config = FrameSourceConfig(
        session_id="session-events",
        source_id="source-events",
        clock_domain="monotonic",
        transform_version="capture-v1",
        source_geometry=geometry,
        max_age_ns=100,
    )
    adapter = FrameSourceAdapter(_NullSource(), config)
    suppressed = adapter.poll(10)
    assert suppressed.status is FrameAdmissionStatus.NO_FRAME
    fatal = adapter.ingest(
        RawFrame(
            source_id="wrong-source",
            session_id="session-events",
            frame_id=1,
            captured_at_ns=10,
            clock_domain="monotonic",
            transform_version="capture-v1",
            source_geometry=geometry,
            content_hash=ZERO_SHA,
            image_ref=f"cas://sha256/{ZERO_SHA}",
        ),
        received_at_ns=11,
    )
    assert fatal.status is FrameAdmissionStatus.SOURCE_MISMATCH

    tape = EventTape(tmp_path / "events.jsonl")
    suppressed_record = append_admission_to_event_tape(
        tape,
        suppressed,
        truth_id="truth-suppressed",
        truth_pixel_digest=ZERO_SHA,
    )
    fatal_record = append_admission_to_event_tape(
        tape,
        fatal,
        truth_id="truth-fatal",
        truth_pixel_digest=ZERO_SHA,
        recorded_at_ns=42,
    )
    assert suppressed_record.event_type == "frame.suppressed"
    assert fatal_record.event_type == "frame.fatal"
    assert suppressed_record.payload["pixel_digest"] is None
    assert fatal_record.payload["fault_latched"] is True

    with pytest.raises(TypeError, match="tape"):
        append_admission_to_event_tape(
            object(),  # type: ignore[arg-type]
            suppressed,
            truth_id="truth-suppressed-2",
            truth_pixel_digest=ZERO_SHA,
        )
    with pytest.raises(TypeError, match="result"):
        append_admission_to_event_tape(
            tape,
            object(),  # type: ignore[arg-type]
            truth_id="truth-suppressed-3",
            truth_pixel_digest=ZERO_SHA,
        )


def test_live_session_b2_gate_requires_real_sample_volume_and_reports_counters(
    tmp_path: Path,
) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    payload = load_strict_json(manifest)
    source = payload["sources"][0]
    source["locator_kind"] = "live_session"
    truth_path = output_root / payload["samples"][0]["truth_path"]
    truth = load_strict_json(truth_path)
    truth["source_locator"] = {"kind": "live_sequence", "value": 0, "timing_truth": False}
    truth["record_digest"] = canonical_digest(truth, omit=("record_digest",))
    _write_json(truth_path, truth)
    payload["samples"][0]["truth_sha256"] = _sha(truth_path)
    truths = [load_strict_json(output_root / item["truth_path"]) for item in payload["samples"]]
    payload["privacy_summary"] = public_privacy_summary(payload, truths)
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)

    with pytest.raises(FrameCorpusError, match="at least 100 samples"):
        corpus.verify_corpus_manifest(
            payload,
            truth_root=output_root,
            require_live_session=True,
        )

    # Expand the live session to the frozen B2 minimum with distinct sample
    # and truth identities; no gate constant is relaxed for the positive case.
    first_sample = copy.deepcopy(payload["samples"][0])
    first_truth = load_strict_json(truth_path)
    payload["sessions"][0]["sample_count"] = 100
    live_samples = [first_sample]
    for sequence in range(1, 100):
        truth = copy.deepcopy(first_truth)
        truth["sample_id"] = f"sample-live-{sequence}"
        truth["truth_id"] = f"truth-live-{sequence}"
        truth["sequence"] = sequence
        truth["source_locator"]["value"] = sequence
        truth["review"] = {
            "primary_reviewer_id": "reviewer-primary",
            "primary_decision": "confirmed",
            "independent_reviewer_id": None,
            "independent_decision": None,
            "adjudication_id": None,
        }
        truth["record_digest"] = canonical_digest(truth, omit=("record_digest",))
        clone_path = output_root / "truths" / f"truth-live-{sequence}.json"
        _write_json(clone_path, truth)
        sample = copy.deepcopy(first_sample)
        sample["sample_id"] = truth["sample_id"]
        sample["truth_id"] = truth["truth_id"]
        sample["truth_path"] = str(clone_path.relative_to(output_root)).replace("\\", "/")
        sample["truth_sha256"] = _sha(clone_path)
        sample["sequence"] = sequence
        live_samples.append(sample)
    payload["samples"] = live_samples + payload["samples"][1:]
    payload["review_summary"] = {
        "primary_reviewed": len(payload["samples"]),
        "independent_reviewed": 1,
        "independent_fraction_ppm": 1_000_000 // len(payload["samples"]),
    }
    truths = [load_strict_json(output_root / item["truth_path"]) for item in payload["samples"]]
    payload["privacy_summary"] = public_privacy_summary(payload, truths)
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)
    summary = corpus.verify_corpus_manifest(
        payload,
        truth_root=output_root,
        require_live_session=True,
    )
    assert summary["live_session_count"] == 1
    assert summary["live_session_sample_count"] == 100


def test_b2_gate_rejects_disputed_review_without_recomputable_adjudication(
    tmp_path: Path,
) -> None:
    manifest, output_root, _ = _make_fixture(tmp_path)
    payload = load_strict_json(manifest)
    payload["sources"][0]["locator_kind"] = "live_session"
    truth_path = output_root / payload["samples"][0]["truth_path"]
    truth = load_strict_json(truth_path)
    truth["source_locator"]["kind"] = "live_sequence"
    truth["review"].update(
        {
            "independent_decision": "disputed_then_adjudicated",
            "adjudication_id": "adjudication-1",
        }
    )
    truth["record_digest"] = canonical_digest(truth, omit=("record_digest",))
    _write_json(truth_path, truth)
    payload["samples"][0]["truth_sha256"] = _sha(truth_path)
    truths = [load_strict_json(output_root / item["truth_path"]) for item in payload["samples"]]
    payload["privacy_summary"] = public_privacy_summary(payload, truths)
    payload["corpus_digest"] = canonical_digest(payload, omit=("corpus_digest",))
    _write_json(manifest, payload)

    with pytest.raises(FrameCorpusError, match="adjudication artifact"):
        corpus.verify_corpus_manifest(
            payload,
            truth_root=output_root,
            require_category_coverage=True,
        )
