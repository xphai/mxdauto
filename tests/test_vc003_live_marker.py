"""Focused tests for the VC-003 live marker/candidate boundary."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from maple_automation_core.capture.pixel_store import PixelSpec, PixelStore, pixel_digest
from maple_automation_core.capture.vc003_source import VC003RawFrame, VC003SourceConfig
from maple_automation_core.domain.frame import FrameSize, SourceRect
from maple_automation_core.replay.vc003_live_marker import (
    BUCKET_DURATION_NS,
    DEFAULT_LIVE_MARKER_CONFIG,
    FULL_FRAME_CALIBRATION_SHA256,
    FULL_FRAME_GEOMETRY,
    FULL_FRAME_GEOMETRY_SHA256,
    FULL_FRAME_PIXEL_SPEC,
    CapacityOneMemoryCAS,
    FixedBucketSelector,
    MemoryCASArtifact,
    ReadOnlyPixelStore,
    VC003BucketSelection,
    VC003FailClosedSummary,
    VC003LiveMarkerRunner,
    VC003LiveMarkerValidationError,
    VC003RestrictedPrivateRow,
    VC003SelectedRow,
    build_frame_source_config,
    validate_live_marker_lineage,
)


def _packet(
    frame_id: int,
    received_at_ns: int,
    *,
    pixel: str | None = None,
    geometry: Any = FULL_FRAME_GEOMETRY,
) -> SimpleNamespace:
    return SimpleNamespace(
        frame_id=frame_id,
        captured_at_ns=received_at_ns,
        received_at_ns=received_at_ns,
        session_id="session-a",
        source_id="capture-card-primary",
        content_hash="a" * 64 if pixel is None else pixel,
        source_geometry=geometry,
        source_size=FULL_FRAME_GEOMETRY.source_size,
    )


def _selection(bucket: int = 0, *, frame_id: int = 7) -> VC003BucketSelection:
    at = bucket * BUCKET_DURATION_NS + 17
    packet = _packet(frame_id, at, pixel=f"{frame_id + 1:064x}")
    return VC003BucketSelection(
        bucket_index=bucket,
        bucket_start_ns=bucket * BUCKET_DURATION_NS,
        bucket_end_ns=(bucket + 1) * BUCKET_DURATION_NS,
        admitted_at_ns=at,
        frame_id=frame_id,
        captured_at_ns=at,
        received_at_ns=at,
        session_id=packet.session_id,
        source_id=packet.source_id,
        pixel_digest=packet.content_hash,
        packet=packet,
    )


def _marker_result(
    *,
    status: str = "candidate",
    frame_id: int = 7,
    result_digest: str = "c" * 64,
) -> SimpleNamespace:
    anchor = SimpleNamespace(x=12.5, y=9.5)
    candidate = (
        SimpleNamespace(digest="a" * 64, anchor_working=anchor) if status == "candidate" else None
    )
    marker = SimpleNamespace(
        source_bbox=SourceRect(x=10, y=20, width=3, height=4),
        source_centroid=(11.0, 21.0),
        area=12,
        bright_core_pixels=8,
    )
    evidence = SimpleNamespace(
        digest="b" * 64,
        config_digest=DEFAULT_LIVE_MARKER_CONFIG.digest,
        calibration_sha256=FULL_FRAME_CALIBRATION_SHA256,
        checked_at_ns=100,
        frame_id=frame_id,
        session_id="session-a",
        source_id="capture-card-primary",
        pixel_digest=f"{frame_id + 1:064x}",
        marker=marker,
    )
    fault = SimpleNamespace(code="marker_fault") if status == "fault" else None
    return SimpleNamespace(
        status=status,
        candidate=candidate,
        evidence=evidence,
        fault=fault,
        digest=result_digest,
        result_digest=result_digest,
    )


def test_full_frame_contract_keeps_none_source_geometry_as_full_frame() -> None:
    source = VC003SourceConfig(source_geometry=None)
    assert source.source_geometry is None
    assert source.geometry == FULL_FRAME_GEOMETRY
    adapter_config = build_frame_source_config(source)
    assert adapter_config.source_geometry == FULL_FRAME_GEOMETRY
    assert adapter_config.geometry == FULL_FRAME_GEOMETRY
    assert DEFAULT_LIVE_MARKER_CONFIG.geometry == FULL_FRAME_GEOMETRY
    assert DEFAULT_LIVE_MARKER_CONFIG.pixel_spec == FULL_FRAME_PIXEL_SPEC
    assert DEFAULT_LIVE_MARKER_CONFIG.calibration_sha256 == FULL_FRAME_CALIBRATION_SHA256
    assert adapter_config.geometry_hash == FULL_FRAME_GEOMETRY_SHA256


def test_selector_uses_packet_received_clock_and_first_accepted_per_bucket() -> None:
    selector = FixedBucketSelector(start_at_ns=1_000)
    first = _packet(1, 1_000)
    second = _packet(2, 1_000 + 100)
    boundary = _packet(3, 1_000 + BUCKET_DURATION_NS)

    assert selector.consider(first)
    assert not selector.consider(second)
    assert selector.consider(boundary)
    assert [item.frame_id for item in selector.selected] == [1, 3]
    assert len(selector.accepted_ledger) == 3
    assert selector.bucket_index(1_000 + BUCKET_DURATION_NS) == 1
    with pytest.raises(VC003LiveMarkerValidationError):
        selector.consider(_packet(4, 1_000 + 2 * BUCKET_DURATION_NS), admitted_at_ns=1_000)


def test_capacity_one_cas_read_only_view_and_selected_retention(tmp_path: Any) -> None:
    spec = PixelSpec(width=2, height=1)
    first_bytes = b"\x00\x01\x02\x03\x04\x05"
    second_bytes = b"\x05\x04\x03\x02\x01\x00"
    first_digest = pixel_digest(spec, first_bytes)
    second_digest = pixel_digest(spec, second_bytes)
    cas = CapacityOneMemoryCAS()
    assert (
        cas.put(
            spec,
            first_bytes,
            source_provenance_id="vc003-live",
            session_id="session-a",
            source_sequence=1,
        )
        == first_digest
    )
    assert cas.read(first_digest, spec) == first_bytes
    assert (
        cas.put(
            spec,
            second_bytes,
            source_provenance_id="vc003-live",
            session_id="session-a",
            source_sequence=2,
        )
        == second_digest
    )
    assert not cas.exists(first_digest)
    assert cas.snapshot()["capacity"] == 1

    readonly = ReadOnlyPixelStore(cas)
    assert readonly.read(second_digest, spec) == second_bytes
    assert not hasattr(readonly, "put")
    destination = PixelStore(tmp_path / "retained")
    artifact = cas.retain_selected(
        second_digest,
        destination,
        source_provenance_id="vc003-live",
        session_id="session-a",
        source_sequence=2,
    )
    assert artifact.pixel_digest == second_digest
    occurrence = destination.occurrence(
        second_digest,
        source_provenance_id="vc003-live",
        session_id="session-a",
        source_sequence=2,
    )
    assert occurrence.privacy_class == "restricted"
    assert occurrence.retention_class == "candidate"


def test_public_row_is_hash_only_and_private_row_keeps_candidate_facts() -> None:
    selection = _selection()
    artifact = MemoryCASArtifact(
        pixel_digest=selection.pixel_digest,
        spec=FULL_FRAME_PIXEL_SPEC,
        byte_length=FULL_FRAME_PIXEL_SPEC.length,
        source_provenance_id="vc003-live",
        session_id=selection.session_id,
        source_sequence=selection.frame_id,
    )
    result = _marker_result()
    row = VC003SelectedRow.from_result(
        selection,
        result,
        artifact,
        checked_at_ns=100,
        completed_at_ns=101,
        config_digest=DEFAULT_LIVE_MARKER_CONFIG.digest,
        calibration_sha256=FULL_FRAME_CALIBRATION_SHA256,
    )
    public = row.to_dict()
    assert public["row_kind"] == "public_hash_only"
    assert public["status"] == "candidate"
    assert public["pixel_digest"] == selection.pixel_digest
    assert "anchor_working" not in public
    assert "source_bbox" not in public
    assert "pixels" not in public
    private = VC003RestrictedPrivateRow.from_result(row, result, artifact)
    private_body = private.to_dict()
    assert private_body["row_kind"] == "restricted_verifier"
    assert private_body["working_candidate"] == {"x": 12.5, "y": 9.5}
    assert private_body["source_bbox"] == {"x": 10, "y": 20, "width": 3, "height": 4}
    assert private_body["pixel_ref"].startswith("external://")


def test_lineage_validator_rejects_duplicate_or_orphan_occurrences() -> None:
    selection = _selection()
    artifact = MemoryCASArtifact(
        pixel_digest=selection.pixel_digest,
        spec=FULL_FRAME_PIXEL_SPEC,
        byte_length=FULL_FRAME_PIXEL_SPEC.length,
        source_provenance_id="vc003-live",
        session_id=selection.session_id,
        source_sequence=selection.frame_id,
    )
    result = _marker_result()
    row = VC003SelectedRow.from_result(
        selection,
        result,
        artifact,
        checked_at_ns=100,
        completed_at_ns=101,
        config_digest=DEFAULT_LIVE_MARKER_CONFIG.digest,
        calibration_sha256=FULL_FRAME_CALIBRATION_SHA256,
    )
    selector = FixedBucketSelector(start_at_ns=0)
    assert selector.consider(selection.packet)
    valid = validate_live_marker_lineage(
        selector,
        [row],
        [result],
        expected_config_digest=DEFAULT_LIVE_MARKER_CONFIG.digest,
        require_complete=False,
    )
    assert valid

    duplicate = validate_live_marker_lineage(
        selector,
        [row, row],
        [result, result],
        expected_config_digest=DEFAULT_LIVE_MARKER_CONFIG.digest,
        require_complete=False,
    )
    assert not duplicate
    assert any("duplicate_bucket" in failure for failure in duplicate.failures)

    forged = replace(
        row,
        bucket_index=1,
        sample_id="bucket-001",
        bucket_start_ns=BUCKET_DURATION_NS,
        bucket_end_ns=2 * BUCKET_DURATION_NS,
        admitted_at_ns=BUCKET_DURATION_NS + 17,
        captured_at_ns=BUCKET_DURATION_NS + 17,
        checked_at_ns=BUCKET_DURATION_NS + 100,
        completed_at_ns=BUCKET_DURATION_NS + 101,
    )
    orphan = validate_live_marker_lineage(
        selector,
        [forged],
        [result],
        expected_config_digest=DEFAULT_LIVE_MARKER_CONFIG.digest,
        require_complete=False,
    )
    assert not orphan
    assert any("orphan_bucket" in failure for failure in orphan.failures)


def test_wrong_geometry_is_rejected_before_extractor() -> None:
    wrong_geometry = FULL_FRAME_GEOMETRY.__class__(
        source_size=FrameSize(width=1920, height=1080),
        content_rect=SourceRect(x=1, y=0, width=1919, height=1080),
        working_size=FrameSize(width=1920, height=1080),
    )
    source = SimpleNamespace(read=lambda: None)
    runner = VC003LiveMarkerRunner(
        source,
        source_config=VC003SourceConfig(source_geometry=None),
        pixel_store=CapacityOneMemoryCAS(),
        clock=lambda: 1_000,
    )
    pixels = bytes(FULL_FRAME_PIXEL_SPEC.length)
    digest = pixel_digest(FULL_FRAME_PIXEL_SPEC, pixels)
    raw = VC003RawFrame(
        source_id="capture-card-primary",
        session_id="vc003-session",
        frame_id=1,
        captured_at_ns=1_000,
        clock_domain="monotonic",
        transform_version="capture-v1",
        source_geometry=wrong_geometry,
        source_size=FULL_FRAME_GEOMETRY.source_size,
        content_hash=digest,
        image_ref="cas://sha256/" + digest,
        raw_bytes=pixels,
        spec=FULL_FRAME_PIXEL_SPEC,
    )
    runner.extractor = cast(
        Any,
        SimpleNamespace(extract=lambda *_args, **_kwargs: pytest.fail("extractor called")),
    )
    admission, outcome = runner.ingest(raw, received_at_ns=1_000)
    assert admission.accepted is False
    assert outcome is not None
    assert isinstance(outcome, VC003FailClosedSummary)
    assert outcome.code == "admission_source_mismatch"
    assert runner.failures[-1] is outcome


def test_fail_closed_summary_contains_no_sensitive_payload() -> None:
    summary = VC003FailClosedSummary(code="geometry_mismatch", frame_id=4)
    body = summary.to_dict()
    assert body["plan_suppressed"] is True
    assert body["extractor_invoked"] is False
    assert body["raw_pixels_public"] is False
    assert body["coordinates_public"] is False
    assert body["absolute_paths_public"] is False
    assert "pixels" not in body
    assert "path" not in body
