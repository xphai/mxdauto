"""Fast contract coverage for the VC-003 live-marker integration boundary.

These tests intentionally use immutable synthetic packets and tiny structural
fakes at the extractor/source seams.  The production module owns the capture,
CAS, selection, row, accounting, lineage, and fail-closed contracts; the
tests exercise those contracts without opening a device or running OpenCV.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from maple_automation_core.capture.frame_source import (
    FrameAdmissionEvent,
    FrameAdmissionResult,
    FrameAdmissionStatus,
)
from maple_automation_core.capture.pixel_store import (
    PixelArtifact,
    PixelSpec,
    PixelStore,
    pixel_digest,
)
from maple_automation_core.capture.vc003_source import VC003SourceConfig
from maple_automation_core.domain.frame import CaptureHealth, FramePacket, FrameSize, SourceRect
from maple_automation_core.replay import vc003_live_marker as live


def _packet(
    frame_id: int = 1,
    received_at_ns: int = 1_000,
    *,
    digest: str | None = None,
    session_id: str = "vc003-session",
    source_id: str = "capture-card-primary",
    geometry: Any = live.FULL_FRAME_GEOMETRY,
    metadata: dict[str, Any] | None = None,
) -> FramePacket:
    value = "a" * 64 if digest is None else digest
    health = CaptureHealth(
        session_id=session_id,
        frame_id=frame_id,
        source_id=source_id,
        content_hash=value,
        clock_domain="monotonic",
        captured_at_ns=received_at_ns,
        received_at_ns=received_at_ns,
        transform_version="capture-v1",
        max_age_ns=live.MAX_AGE_NS,
    )
    return FramePacket(
        source_id=source_id,
        session_id=session_id,
        frame_id=frame_id,
        captured_at_ns=received_at_ns,
        received_at_ns=received_at_ns,
        transform_version="capture-v1",
        clock_domain="monotonic",
        content_hash=value,
        source_geometry=geometry,
        image_ref=f"cas://sha256/{value}",
        capture_health=health,
        image_metadata={} if metadata is None else metadata,
    )


def _event_result(packet: FramePacket) -> FrameAdmissionResult:
    event = FrameAdmissionEvent(
        status=FrameAdmissionStatus.ACCEPTED,
        observed_at_ns=packet.received_at_ns,
        reason="accepted",
        session_id=packet.session_id,
        source_id=packet.source_id,
        frame_id=packet.frame_id,
        plan_suppressed=False,
        fault_latched=False,
    )
    return FrameAdmissionResult(status=FrameAdmissionStatus.ACCEPTED, event=event, packet=packet)


def _result(
    packet: FramePacket,
    *,
    status: str = "candidate",
    result_digest: str = "c" * 64,
    with_marker: bool = True,
) -> SimpleNamespace:
    candidate = None
    if status == "candidate":
        candidate = SimpleNamespace(
            digest="a" * 64,
            anchor_working=SimpleNamespace(x=12.5, y=9.5),
            source_frame_id=packet.frame_id,
            frame_id=packet.frame_id,
            session_id=packet.session_id,
            source_id=packet.source_id,
            pixel_digest=packet.content_hash,
            generation=0,
        )
    marker = None
    if with_marker:
        marker = SimpleNamespace(
            source_bbox=SourceRect(x=10, y=20, width=3, height=4),
            source_centroid=(11.0, 21.0),
            area=12,
            bright_core_pixels=8,
        )
    evidence = SimpleNamespace(
        digest="b" * 64,
        config_digest=live.DEFAULT_LIVE_MARKER_CONFIG.digest,
        calibration_sha256=live.FULL_FRAME_CALIBRATION_SHA256,
        checked_at_ns=packet.received_at_ns + 1,
        frame_id=packet.frame_id,
        session_id=packet.session_id,
        source_id=packet.source_id,
        pixel_digest=packet.content_hash,
        received_at_ns=packet.received_at_ns,
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


def _memory_artifact(selection: live.VC003BucketSelection) -> live.MemoryCASArtifact:
    return live.MemoryCASArtifact(
        pixel_digest=selection.pixel_digest,
        spec=live.FULL_FRAME_PIXEL_SPEC,
        byte_length=live.FULL_FRAME_PIXEL_SPEC.length,
        source_provenance_id="vc003-live",
        session_id=selection.session_id,
        source_sequence=selection.frame_id,
    )


def _selection(bucket: int = 0, frame_id: int = 1) -> live.VC003BucketSelection:
    at = bucket * live.BUCKET_DURATION_NS + 17
    packet = _packet(frame_id, at, digest=f"{frame_id + 1:064x}")
    return live.VC003BucketSelection(
        bucket_index=bucket,
        bucket_start_ns=bucket * live.BUCKET_DURATION_NS,
        bucket_end_ns=(bucket + 1) * live.BUCKET_DURATION_NS,
        admitted_at_ns=at,
        frame_id=frame_id,
        captured_at_ns=at,
        received_at_ns=at,
        session_id=packet.session_id,
        source_id=packet.source_id,
        pixel_digest=packet.content_hash,
        packet=packet,
    )


def _row(
    *,
    bucket: int = 0,
    frame_id: int = 1,
    status: str = "candidate",
    result_digest: str = "c" * 64,
) -> tuple[live.VC003SelectedRow, live.VC003RestrictedPrivateRow, SimpleNamespace]:
    selection = _selection(bucket, frame_id)
    result = _result(selection.packet, status=status, result_digest=result_digest)
    artifact = _memory_artifact(selection)
    row = live.VC003SelectedRow.from_result(
        selection,
        result,
        artifact,
        checked_at_ns=selection.received_at_ns + 1,
        completed_at_ns=selection.received_at_ns + 2,
        config_digest=live.DEFAULT_LIVE_MARKER_CONFIG.digest,
        calibration_sha256=live.FULL_FRAME_CALIBRATION_SHA256,
    )
    private = live.VC003RestrictedPrivateRow.from_result(row, result, artifact)
    return row, private, result


def test_config_thresholds_and_small_helpers_cover_fixed_contract() -> None:
    assert live.full_frame_geometry() == live.FULL_FRAME_GEOMETRY
    config = live.default_minimap_marker_config(
        session_id="s", source_id="src", clock_domain="mono", max_age_ns=4
    )
    assert config.session_id == "s"
    assert config.source_id == "src"
    assert config.clock_domain == "mono"
    assert config.max_age_ns == 4
    assert live.build_frame_source_config(VC003SourceConfig(), max_age_ns=4).max_age_ns == 4
    thresholds = live.VC003LiveMarkerThresholds(max_age_ns=4, min_candidate_count=2)
    assert thresholds.bucket_seconds == 3
    assert thresholds.duration_ns == live.BUCKET_DURATION_NS
    assert thresholds.digest == live._digest(thresholds.to_dict())
    assert thresholds.to_hash_only() == thresholds.to_dict()
    assert live.LiveMarkerThresholds is live.VC003LiveMarkerThresholds
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003LiveMarkerThresholds(bucket_count=99)
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003LiveMarkerThresholds(bucket_seconds=2)
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.build_frame_source_config(
            replace(VC003SourceConfig(), transform_version="wrong")
        )
    with pytest.raises(TypeError):
        live.build_frame_source_config(object())  # type: ignore[arg-type]
    with pytest.raises(live.VC003LiveMarkerError):
        live._sha256("short", "digest")
    with pytest.raises(live.VC003LiveMarkerError):
        live._token("a\nb", "token")
    with pytest.raises(live.VC003LiveMarkerError):
        live._non_negative(-1, "count")
    assert live._status("FOUND") == "candidate"
    assert live._status("none") == "no_candidate"
    assert live._status("error") == "fault"
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live._status("other")


def test_read_only_store_and_capacity_one_cas_argument_and_error_paths() -> None:
    spec = PixelSpec(width=2, height=1)
    first = b"\x00\x01\x02\x03\x04\x05"
    second = b"\x05\x04\x03\x02\x01\x00"
    first_digest = pixel_digest(spec, first)
    second_digest = pixel_digest(spec, second)
    cas = live.CapacityOneMemoryCAS()
    assert cas.put(first, spec, source_provenance_id="p", session_id="s", source_sequence=1)
    assert cas.latest_digest == first_digest
    assert cas.put(spec, second, source_provenance_id="p", session_id="s", source_sequence=2)
    assert cas.latest is not None
    assert cas.latest.digest == second_digest
    assert cas.put_count == 2 and cas.superseded_count == 1
    assert cas.get(second_digest, spec) == second
    assert cas.load(second_digest, spec) == second
    assert cas.read_pixels(second_digest, spec) == second
    assert cas.get_artifact(second_digest).pixel_digest == second_digest
    assert cas.read_artifact(second_digest).ref.endswith(second_digest)
    assert cas.get_occurrence(
        second_digest, source_provenance_id="p", session_id="s", source_sequence=2
    )
    assert cas.snapshot() == {
        "capacity": 1,
        "occupied": True,
        "latest_pixel_digest": second_digest,
        "put_count": 2,
        "superseded_count": 1,
    }
    readonly = live.ReadOnlyPixelStore(cas)
    assert readonly.get(second_digest, spec) == second
    assert readonly.load(second_digest, spec) == second
    assert readonly.exists(second_digest, spec)
    assert readonly.has(second_digest, spec)
    assert not readonly.exists("0" * 64)
    with pytest.raises(TypeError):
        live.ReadOnlyPixelStore(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cas.put(spec)  # type: ignore[call-overload]
    with pytest.raises(TypeError):
        cas.put(first, object())  # type: ignore[arg-type]
    with pytest.raises(live.VC003LiveMarkerValidationError):
        cas.read(first_digest)
    with pytest.raises(live.VC003LiveMarkerValidationError):
        cas.read(second_digest, PixelSpec(width=1, height=2))
    with pytest.raises(live.VC003LiveMarkerValidationError):
        cas.occurrence(
            second_digest,
            source_provenance_id="other",
            session_id="s",
            source_sequence=2,
        )


def test_capacity_one_cas_encoded_metadata_and_external_retention() -> None:
    spec = PixelSpec(width=2, height=1)
    data = b"\x10\x11\x12\x13\x14\x15"
    encoded = b"encoded"
    cas = live.CapacityOneMemoryCAS()
    digest = pixel_digest(spec, data)
    encoded_digest = live.encoded_payload_sha256(encoded)
    artifact = cas.put_artifact(
        data,
        spec,
        encoded_bytes=encoded,
        encoded_sha256=encoded_digest,
        encoded_size=len(encoded),
        privacy_class="restricted",
        retention_class="candidate",
        source_provenance_id="p",
        session_id="s",
        source_sequence=3,
        expected_pixel_digest=digest,
    )
    assert isinstance(artifact, PixelArtifact)
    assert artifact.source_encoded_sha256 == encoded_digest
    assert artifact.source_encoded_size == len(encoded)
    destination = PixelStore.__new__(PixelStore)
    # A real PixelStore gives the retention path a durable, read-back target.
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        destination = PixelStore(root)
        retained = cas.retain_selected(
            digest,
            destination,
            source_provenance_id="p",
            session_id="s",
            source_sequence=3,
        )
        assert retained.privacy_class == "restricted"
        assert destination.read(digest, spec) == data
    with pytest.raises(ValueError):
        cas.put_artifact(
            data,
            spec,
            encoded_bytes=encoded,
            encoded_size=1,
            source_provenance_id="p",
            session_id="s",
            source_sequence=4,
        )
    with pytest.raises(live.VC003LiveMarkerValidationError):
        cas.retain_selected(
            digest,
            destination,
            source_provenance_id="p",
            session_id="s",
            source_sequence=3,
            privacy_class="private",
        )
    with pytest.raises(TypeError):
        cas.retain_selected(
            digest,
            object(),  # type: ignore[arg-type]
            source_provenance_id="p",
            session_id="s",
            source_sequence=3,
        )


def test_low_level_contract_guards_and_eviction_fail_closed() -> None:
    with pytest.raises(live.VC003LiveMarkerError):
        live._canonical({"not-json": object()})
    with pytest.raises(live.VC003LiveMarkerError):
        live._sha256("g" * 64, "digest")
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.build_frame_source_config(
            VC003SourceConfig(
                source_geometry=live.FULL_FRAME_GEOMETRY.__class__(
                    source_size=FrameSize(width=1920, height=1080),
                    content_rect=SourceRect(x=1, y=0, width=1919, height=1080),
                    working_size=FrameSize(width=1920, height=1080),
                )
            )
        )
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003LiveMarkerThresholds(bucket_duration_ns=1)
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003LiveMarkerThresholds(generation=1)

    spec = PixelSpec(width=2, height=1)
    data = b"\x01\x02\x03\x04\x05\x06"
    digest = pixel_digest(spec, data)
    with pytest.raises(TypeError):
        live.MemoryCASArtifact(
            pixel_digest=digest,
            spec=object(),  # type: ignore[arg-type]
            byte_length=6,
            source_provenance_id="p",
            session_id="s",
            source_sequence=1,
        )
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.MemoryCASArtifact(
            pixel_digest=digest,
            spec=spec,
            byte_length=5,
            source_provenance_id="p",
            session_id="s",
            source_sequence=1,
        )
    artifact = live.MemoryCASArtifact(
        pixel_digest=digest,
        spec=spec,
        byte_length=spec.length,
        source_provenance_id="p",
        session_id="s",
        source_sequence=1,
    )
    assert artifact.digest == digest
    assert artifact.ref == f"cas://sha256/{digest}"
    assert artifact.artifact_sha256 == live._digest(
        {
            "pixel_digest": digest,
            "spec": spec.to_dict(),
            "byte_length": spec.length,
            "source_provenance_id": "p",
            "session_id": "s",
            "source_sequence": 1,
        }
    )
    cas = live.CapacityOneMemoryCAS()
    with pytest.raises(live.VC003LiveMarkerValidationError):
        cas.artifact(digest)
    with pytest.raises(live.VC003LiveMarkerValidationError):
        cas.retain_selected(
            digest,
            PixelStore.__new__(PixelStore),  # type: ignore[arg-type]
            source_provenance_id="p",
            session_id="s",
            source_sequence=1,
        )
    encoded = b"encoded"
    with pytest.raises(live.VC003LiveMarkerValidationError):
        cas.put(
            spec,
            data,
            expected_pixel_digest="f" * 64,
            source_provenance_id="p",
            session_id="s",
            source_sequence=1,
        )
    with pytest.raises(live.VC003LiveMarkerValidationError):
        cas.put(
            spec,
            data,
            encoded_bytes=encoded,
            encoded_sha256="f" * 64,
            source_provenance_id="p",
            session_id="s",
            source_sequence=1,
        )
    with pytest.raises(live.VC003LiveMarkerValidationError):
        cas.put(
            spec,
            data,
            encoded_bytes=encoded,
            encoded_hash="f" * 64,
            source_provenance_id="p",
            session_id="s",
            source_sequence=1,
        )
    with pytest.raises(ValueError):
        cas.put(
            spec,
            data,
            encoded_sha256=live.encoded_payload_sha256(encoded),
            source_provenance_id="p",
            session_id="s",
            source_sequence=1,
        )
    with pytest.raises(ValueError):
        cas.put(
            spec,
            data,
            encoded_size=4,
            source_provenance_id="p",
            session_id="s",
            source_sequence=1,
        )


def test_selector_accepts_protocol_variants_and_enforces_first_occurrence() -> None:
    packet0 = _packet(1, 1_000, digest="1" * 64)
    packet1 = _packet(2, 1_000 + live.BUCKET_DURATION_NS, digest="2" * 64)
    selector = live.FixedBucketSelector(measurement_start_ns=1_000)
    assert selector.bucket_for(1_000) == 0
    assert selector.bucket_index(selector.end_at_ns) is None
    assert selector.consider(FrameAdmissionResult(
        status=FrameAdmissionStatus.ACCEPTED,
        event=FrameAdmissionEvent(
            status=FrameAdmissionStatus.ACCEPTED,
            observed_at_ns=1_000,
            reason="ok",
            plan_suppressed=False,
            fault_latched=False,
        ),
        packet=packet0,
    ))
    assert not selector.consider(_packet(3, 1_100, digest="3" * 64))
    assert selector.consider(packet1)
    assert selector.consider(_packet(4, 1_000 + 2 * live.BUCKET_DURATION_NS, digest="4" * 64))
    assert selector.coverage == 3
    assert len(selector.accepted_ledger) == 4
    assert selector.selected == selector.selections
    assert selector.missing_buckets[0] == 3
    assert not selector.complete
    with pytest.raises(live.VC003LiveMarkerValidationError):
        selector.validate_complete()
    selector.validate_first_accepted()
    with pytest.raises(live.VC003LiveMarkerValidationError):
        selector.consider(packet1, admitted_at_ns=1_001 + live.BUCKET_DURATION_NS)
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.FixedBucketSelector(start_at_ns=1, origin_ns=2)
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.FixedBucketSelector(bucket_count=1)
    assert live.FixedBucketSelector.select([packet0]).coverage == 1
    assert selector.digest == live._digest(selector.to_dict())
    assert selector.to_hash_only() == selector.to_dict()


def test_selector_rejects_statuses_and_handles_generic_admission_shapes() -> None:
    selector = live.FixedBucketSelector(start_at_ns=0)
    rejected = SimpleNamespace(
        accepted=False,
        status=FrameAdmissionStatus.STALE,
        packet=None,
    )
    assert not selector.consider(rejected)
    assert not selector.consider(SimpleNamespace(status=FrameAdmissionStatus.NO_FRAME))
    packet = SimpleNamespace(
        frame_id=8,
        captured_at_ns=100,
        received_at_ns=100,
        session_id="s",
        source_id="src",
        content_hash="8" * 64,
    )
    assert selector.consider({"packet": packet, "admitted_at_ns": 100})
    with pytest.raises(TypeError):
        selector.consider(SimpleNamespace(accepted=True, packet=object()))
    with pytest.raises(live.VC003LiveMarkerValidationError):
        selector.consider(packet, admitted_at_ns=101)
    with pytest.raises(live.VC003LiveMarkerValidationError):
        selector.consider(SimpleNamespace(
            frame_id=8,
            captured_at_ns=100,
            received_at_ns=100,
            session_id="s",
            source_id="src",
            content_hash="9" * 64,
        ))


def test_selector_reaches_full_fixed_window_and_serializes_every_bucket() -> None:
    selector = live.FixedBucketSelector(start_at_ns=0)
    for bucket in range(live.BUCKET_COUNT):
        at = bucket * live.BUCKET_DURATION_NS + 1
        packet = SimpleNamespace(
            frame_id=bucket,
            captured_at_ns=at,
            received_at_ns=at,
            session_id="s",
            source_id="src",
            content_hash=f"{bucket + 1:064x}",
        )
        assert selector.consider(packet)
    assert selector.complete
    assert selector.coverage == live.BUCKET_COUNT
    assert selector.missing_buckets == ()
    selector.validate_complete()
    body = selector.to_dict()
    assert len(body["buckets"]) == live.BUCKET_COUNT
    assert body["accepted_counts"] == [1] * live.BUCKET_COUNT
    assert body["coverage"] == live.BUCKET_COUNT


def test_public_row_roundtrip_detached_parse_and_privacy_rejection() -> None:
    row, private, result = _row()
    body = row.to_dict()
    assert body["row_kind"] == "public_hash_only"
    assert row.frame_digest == row.frame_digest
    assert row.marker_result_digest == row.result_digest
    assert row.selected_digest == row.digest
    assert "source_bbox" not in body
    assert live.VC003SelectedRow.from_dict(body).to_dict() == body
    with pytest.raises(live.VC003LiveMarkerPrivacyError):
        live.VC003SelectedRow.from_dict(private.to_dict())
    offset_body = dict(body, bucket_offset_ns=5)
    offset_body["row_digest"] = live._digest(
        {key: value for key, value in offset_body.items() if key != "row_digest"}
    )
    assert live.VC003SelectedRow.from_dict(offset_body).admitted_at_ns == 5
    with pytest.raises(TypeError):
        live.VC003SelectedRow.from_dict([])  # type: ignore[arg-type]
    with pytest.raises(live.VC003LiveMarkerPrivacyError):
        live.VC003SelectedRow.from_dict(dict(body, raw_bytes="LEAK"))
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003SelectedRow.from_dict({"row_kind": "public_hash_only"})
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003SelectedRow.from_dict(dict(body, row_digest="0" * 64))
    with pytest.raises(live.VC003LiveMarkerPrivacyError):
        live._assert_public_hash_only({"nested": {"absolute_path": "C:/secret"}})
    with pytest.raises(live.VC003LiveMarkerPrivacyError):
        live._assert_public_hash_only({"nested": [b"raw"]})
    with pytest.raises(live.VC003LiveMarkerPrivacyError):
        live._assert_public_hash_only({"nested": "\\\\server\\share"})
    assert result.status == "candidate"


def test_selected_row_full_identity_roundtrip_and_contract_mutations() -> None:
    row, _, _ = _row()
    full_body = {
        field.name: getattr(row, field.name)
        for field in row.__dataclass_fields__.values()
        if field.name != "_public_frame_digest"
    }
    restored = live.VC003SelectedRow.from_dict(full_body)
    assert restored == row
    with pytest.raises(live.VC003LiveMarkerPrivacyError):
        live.VC003SelectedRow.from_dict(dict(full_body, extra="private"))
    missing = dict(full_body)
    missing.pop("state_digest")
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003SelectedRow.from_dict(missing)

    mutations: list[dict[str, Any]] = [
        {"schema_version": "0"},
        {"scope": "other"},
        {"sample_id": "bucket-099"},
        {"bucket_index": live.BUCKET_COUNT},
        {"bucket_end_ns": live.BUCKET_DURATION_NS + 1},
        {"admitted_at_ns": live.BUCKET_DURATION_NS},
        {"checked_at_ns": 0},
        {"captured_at_ns": live.BUCKET_DURATION_NS},
        {"generation": 1},
        {"calibration_sha256": "f" * 64},
        {"marker_status": "fault", "fault_code": None},
        {"marker_status": "candidate", "candidate_digest": None},
        {"marker_status": "no_candidate", "candidate_digest": "a" * 64},
        {"plan_suppressed": True},
        {"retained": 1},
    ]
    for mutation in mutations:
        with pytest.raises((live.VC003LiveMarkerValidationError, live.VC003LiveMarkerError)):
            live.VC003SelectedRow(**{**full_body, **mutation})


def test_result_helpers_and_detached_normalization_cover_mapping_protocols() -> None:
    payload = {"status": "candidate", "value": 1}
    assert live._result_payload(payload) == payload
    assert live._result_payload(SimpleNamespace(to_dict=lambda: payload)) == payload
    with pytest.raises(TypeError):
        live._result_payload(SimpleNamespace(to_dict=lambda: []))
    with pytest.raises(TypeError):
        live._result_payload(object())
    payload_result = SimpleNamespace(to_dict=lambda: payload)
    assert live._result_digest(payload_result) == live._digest(payload)
    candidate = SimpleNamespace(to_dict=lambda: {"x": 1})
    evidence = SimpleNamespace(to_dict=lambda: {"e": 1})
    fallback = SimpleNamespace(candidate=candidate, evidence=evidence)
    assert live._candidate_digest(fallback) == live._digest({"x": 1})
    assert live._evidence_digest(fallback) == live._digest({"e": 1})
    assert live._candidate_digest(SimpleNamespace(candidate=None)) is None
    assert live._evidence_digest(SimpleNamespace(evidence=None)) is None
    assert live._fault_code(SimpleNamespace(fault=SimpleNamespace(code="fault"))) == "fault"
    assert live._normalize_row(_row()[0].to_dict()).status == "candidate"


@pytest.mark.parametrize("status", ["no_candidate", "fault"])
def test_public_and_restricted_rows_cover_non_candidate_branches(status: str) -> None:
    row, private, result = _row(
        status=status,
        result_digest=("d" if status == "fault" else "e") * 64,
    )
    assert row.status == status
    assert row.plan_suppressed is True
    assert row.candidate_digest is None
    private_body = private.to_dict()
    assert private_body["status"] == status
    assert "working_candidate" not in private_body
    assert "source_bbox" in private_body
    detached = live.VC003SelectedRow.from_dict(row.to_dict())
    assert detached.status == status
    assert result.fault is not None if status == "fault" else result.fault is None


def test_restricted_row_coordinates_and_artifact_contract() -> None:
    row, _, result = _row()
    selection = _selection()
    artifact = _memory_artifact(selection)
    restricted = live.VC003RestrictedPrivateRow.from_result(
        row,
        result,
        artifact,
        marker_coordinates={"source": {"x": 1.5, "y": 2.5}},
        artifact_path="external://private",
    )
    body = restricted.to_dict()
    assert restricted.digest == live._digest(body)
    assert body["working_candidate"] == {"x": 12.5, "y": 9.5}
    assert body["component_area"] == 12
    with pytest.raises(TypeError):
        live.VC003RestrictedPrivateRow(row, result, artifact, marker_coordinates=b"raw")  # type: ignore[arg-type]
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003RestrictedPrivateRow(
            row,
            result,
            replace(artifact, source_sequence=99),
        )


def test_accounting_roundtrip_and_validation() -> None:
    candidate = SimpleNamespace(status="found")
    no_candidate = SimpleNamespace(status="no_marker")
    fault = SimpleNamespace(status="error")
    accounting = live.VC003MarkerAccounting.from_results([candidate, no_candidate, fault])
    assert accounting.selected == 3
    assert accounting.candidate == 1
    assert accounting.no_candidate == 1
    assert accounting.fault == 1
    assert accounting.marker_fault == 1
    assert accounting.total == 3
    assert accounting.valid
    assert accounting.digest == live._digest(accounting.to_dict())
    accounting.validate(3)
    rows = [_row(status="candidate")[0], _row(status="no_candidate")[0], _row(status="fault")[0]]
    assert live.validate_marker_accounting(rows, expected_selected=3).valid
    assert live.validate_accounting([candidate, no_candidate, fault]).valid
    with pytest.raises(live.VC003LiveMarkerValidationError):
        accounting.validate(2)
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003MarkerAccounting(selected=1, candidate=1, no_candidate=1).validate()
    with pytest.raises(live.VC003LiveMarkerError):
        live.VC003MarkerAccounting(selected=-1)


def test_fail_closed_summary_and_lineage_valid_and_invalid_paths() -> None:
    summary = live.VC003FailClosedSummary(
        code="geometry_mismatch",
        session_id="s",
        source_id="src",
        frame_id=2,
        actual_size=FrameSize(width=1, height=1),
        actual_geometry_sha256=live.FULL_FRAME_GEOMETRY_SHA256,
    )
    assert summary.calibration_sha256 == live.FULL_FRAME_CALIBRATION_SHA256
    assert summary.digest == live._digest(summary._body_dict())
    assert summary.to_dict()["digest"] == summary.digest
    assert (
        live.VC003FailClosedSummary(code="empty", expected_size=None).to_dict()["expected_size"]
        is None
    )
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003FailClosedSummary(code="bad", plan_suppressed=False)
    selection = _selection()
    selector = live.FixedBucketSelector(start_at_ns=0)
    selector.consider(selection.packet)
    row, private, result = _row()
    valid = live.validate_live_marker_lineage(
        selector,
        [row],
        [result],
        expected_config_digest=live.DEFAULT_LIVE_MARKER_CONFIG.digest,
        require_complete=False,
    )
    assert valid and valid.checked_rows == 1
    assert valid.digest == live._digest(valid.to_dict())
    assert live.validate_live_marker_lineage(selector, [private], [result], require_complete=False)
    restricted_wire = private.to_dict()
    restricted_wire["row_digest"] = live._digest(
        {
            "schema_version": live.SCHEMA_VERSION,
            "row_kind": "public_hash_only",
            "bucket_index": row.bucket_index,
            "status": row.status,
            "generation": live.GENERATION,
            "frame_digest": row.frame_digest,
            "pixel_digest": row.pixel_digest,
            "candidate_digest": row.candidate_digest,
            "evidence_digest": row.evidence_digest,
            "result_digest": row.result_digest,
        }
    )
    restricted_check = live.validate_live_marker_lineage(
        [selection], [restricted_wire], [result], require_complete=False
    )
    assert not restricted_check
    assert any("result_lineage_mismatch" in failure for failure in restricted_check.failures)
    invalid = live.validate_live_marker_lineage(
        selector,
        [replace(row, retained=False)],
        [_result(selection.packet, status="no_candidate", result_digest=result.result_digest)],
        expected_config_digest="f" * 64,
        expected_geometry=live.full_frame_geometry(),
        require_complete=True,
    )
    assert not invalid
    assert invalid.failures
    malformed = live.validate_live_marker_lineage([object()], [], require_complete=False)
    assert not malformed
    assert any(failure.startswith("selection_invalid") for failure in malformed.failures)


def test_lineage_reports_selection_row_packet_cas_and_result_mismatches() -> None:
    row, _, result = _row()
    selection = _selection()
    selector = live.FixedBucketSelector(start_at_ns=0)
    selector.consider(selection.packet)
    duplicate_selection = live.validate_live_marker_lineage(
        [selection, selection], [row], require_complete=False
    )
    assert not duplicate_selection
    assert any("duplicate_selection_bucket" in item for item in duplicate_selection.failures)
    duplicate_occurrence = live.validate_live_marker_lineage(
        [_selection(), _selection(bucket=1)], [row], require_complete=False
    )
    assert any("duplicate_occurrence" in item for item in duplicate_occurrence.failures)

    public = row.to_dict()
    public["frame_digest"] = "f" * 64
    public["row_digest"] = live._digest(
        {key: value for key, value in public.items() if key != "row_digest"}
    )
    frame_mismatch = live.validate_live_marker_lineage(
        [selection], [public], require_complete=False
    )
    assert any("frame_digest_mismatch" in item for item in frame_mismatch.failures)

    wrong_row = replace(row, source_sequence=9, retained=False)
    mismatched_packet = SimpleNamespace(
        frame_id=selection.frame_id,
        captured_at_ns=selection.captured_at_ns,
        received_at_ns=selection.received_at_ns,
        session_id=selection.session_id,
        source_id=selection.source_id,
        content_hash=selection.pixel_digest,
        source_geometry=SourceRect(x=0, y=0, width=1, height=1),
        source_size=FrameSize(width=1, height=1),
    )
    packet_selection = replace(selection, packet=mismatched_packet)
    packet_check = live.validate_live_marker_lineage(
        [packet_selection], [wrong_row], [result], require_complete=False
    )
    assert any("lineage_mismatch:source_sequence" in item for item in packet_check.failures)
    assert any("geometry_mismatch" in item for item in packet_check.failures)
    assert any("size_mismatch" in item for item in packet_check.failures)
    assert any("retention_not_attested" in item for item in packet_check.failures)

    class BadRetainedStore:
        def read(self, _digest: str, _spec: PixelSpec) -> bytes:
            return bytes(_spec.length)

        def occurrence(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                privacy_class="private",
                retention_class="persistent",
                artifact_sha256="0" * 64,
            )

    cas_check = live.validate_live_marker_lineage(
        selector,
        [row],
        retained_store=BadRetainedStore(),  # type: ignore[arg-type]
        require_complete=False,
    )
    assert any("cas_digest_mismatch" in item for item in cas_check.failures)
    assert any("cas_retention_mismatch" in item for item in cas_check.failures)
    assert any("cas_artifact_mismatch" in item for item in cas_check.failures)
    missing_cas = live.validate_live_marker_lineage(
        selector,
        [row],
        retained_store=SimpleNamespace(read=lambda *_args: (_ for _ in ()).throw(OSError("gone"))),  # type: ignore[arg-type]
        require_complete=False,
    )
    assert any(item.startswith("cas_missing") for item in missing_cas.failures)

    assert any(
        "result_count_mismatch" in item
        for item in live.validate_live_marker_lineage(
            selector, [row], [], require_complete=False
        ).failures
    )
    bad_result = SimpleNamespace(
        status="candidate",
        digest="f" * 64,
        result_digest="f" * 64,
        evidence=SimpleNamespace(
            digest="e" * 64,
            frame_id=99,
            session_id="wrong",
            source_id="wrong",
            pixel_digest="0" * 64,
        ),
        candidate=None,
        fault=None,
    )
    result_check = live.validate_live_marker_lineage(
        selector, [row], [bad_result], require_complete=False
    )
    assert any("result_digest_mismatch" in item for item in result_check.failures)
    assert any("evidence_digest_mismatch" in item for item in result_check.failures)
    assert any("candidate_digest_mismatch" in item for item in result_check.failures)
    assert any("result_lineage_mismatch" in item for item in result_check.failures)
    invalid_result = live.validate_live_marker_lineage(
        selector, [row], [SimpleNamespace(status=object())], require_complete=False
    )
    assert any(item.startswith("result_invalid") for item in invalid_result.failures)


def _runner_setup(
    *,
    source_store: Any | None = None,
    retained_store: PixelStore | None = None,
    memory_cas: live.CapacityOneMemoryCAS | None = None,
    clock: Any = None,
) -> tuple[live.VC003LiveMarkerRunner, FramePacket, bytes]:
    data = bytes(live.FULL_FRAME_PIXEL_SPEC.length)
    digest = pixel_digest(live.FULL_FRAME_PIXEL_SPEC, data)
    cas = live.CapacityOneMemoryCAS() if source_store is None else source_store
    if isinstance(cas, live.CapacityOneMemoryCAS):
        cas.put(
            live.FULL_FRAME_PIXEL_SPEC,
            data,
            source_provenance_id="vc003-live",
            session_id="vc003-session",
            source_sequence=1,
        )
    source = SimpleNamespace(
        read=lambda: None,
        pixel_store=cas,
    )
    runner = live.VC003LiveMarkerRunner(
        source,
        source_config=VC003SourceConfig(),
        retained_store=retained_store,
        memory_cas=memory_cas,
        clock=clock,
    )
    packet = _packet(frame_id=1, received_at_ns=1_000, digest=digest)
    return runner, packet, data


def test_runner_processes_real_admission_and_validates_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, packet, _ = _runner_setup(clock=lambda: 1_001)
    result = _result(packet)
    runner.extractor = SimpleNamespace(extract=lambda *_args, **_kwargs: result)
    admission = _event_result(packet)
    row = runner.process_admission(admission, checked_at_ns=1_001)
    assert isinstance(row, live.VC003SelectedRow)
    assert runner.rows == [row]
    assert len(runner.private_rows) == 1
    assert runner.results == [result]
    assert runner.accounting.candidate == 1
    assert runner.validate(require_complete=False)
    assert runner.selector is not None
    assert runner.selector.coverage == 1
    assert runner.memory_cas is runner.source_store
    assert runner.extractor.pixel_store if hasattr(runner.extractor, "pixel_store") else True
    assert runner.source_provenance_id == "vc003-live"


def test_runner_copies_from_source_cas_into_memory_and_retains_external_occurrence() -> None:
    import tempfile

    data = bytes(live.FULL_FRAME_PIXEL_SPEC.length)
    digest = pixel_digest(live.FULL_FRAME_PIXEL_SPEC, data)
    source_cas = live.CapacityOneMemoryCAS()
    source_cas.put(
        live.FULL_FRAME_PIXEL_SPEC,
        data,
        source_provenance_id="vc003-live",
        session_id="vc003-session",
        source_sequence=1,
    )
    memory_cas = live.CapacityOneMemoryCAS()
    source = SimpleNamespace(read=lambda: None, pixel_store=source_cas)
    with tempfile.TemporaryDirectory() as root:
        retained_store = PixelStore(root)
        runner = live.VC003LiveMarkerRunner(
            source,
            source_config=VC003SourceConfig(),
            memory_cas=memory_cas,
            retained_store=retained_store,
            clock=lambda: 1_001,
        )
        packet = _packet(frame_id=1, received_at_ns=1_000, digest=digest)
        result = _result(packet)
        runner.extractor = SimpleNamespace(extract=lambda *_args, **_kwargs: result)
        row = runner.process_admission(_event_result(packet), checked_at_ns=1_001)
        assert isinstance(row, live.VC003SelectedRow)
        assert runner.memory_cas is memory_cas
        assert runner.source_store is source_cas
        assert source_cas is not memory_cas
        assert retained_store.read(digest, live.FULL_FRAME_PIXEL_SPEC) == data
        assert runner.validate(require_complete=False)


def test_runner_timestamp_regression_fails_closed_after_cas_read() -> None:
    runner, packet, _ = _runner_setup(clock=lambda: 2_000)
    called = False

    def extract(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        return _result(packet)

    runner.extractor = SimpleNamespace(extract=extract)
    outcome = runner.process_admission(_event_result(packet), checked_at_ns=999)
    assert isinstance(outcome, live.VC003FailClosedSummary)
    assert outcome.code == "timestamp_regression"
    assert not called


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("geometry", "geometry_mismatch"),
        ("health", "freshness_contract_mismatch"),
        ("bad_spec", "pixel_spec_mismatch"),
        ("wrong_spec", "frame_size_changed"),
    ],
)
def test_runner_fail_closed_before_selector_or_extractor(mutation: str, code: str) -> None:
    runner, packet, _ = _runner_setup(clock=lambda: 1_001)
    called = False

    def extract(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        return _result(packet)

    runner.extractor = SimpleNamespace(extract=extract)
    if mutation == "geometry":
        wrong = live.FULL_FRAME_GEOMETRY.__class__(
            source_size=FrameSize(width=1920, height=1080),
            content_rect=SourceRect(x=1, y=0, width=1919, height=1080),
            working_size=FrameSize(width=1920, height=1080),
        )
        packet = replace(packet, source_geometry=wrong)
    elif mutation == "health":
        packet = replace(packet, capture_health=replace(packet.capture_health, max_age_ns=1))
    elif mutation == "bad_spec":
        packet = replace(packet, image_metadata={"pixel_spec": "bad"})
    else:
        packet = replace(
            packet,
            image_metadata={"pixel_spec": PixelSpec(width=1, height=1).to_dict()},
        )
    outcome = runner.process_admission(_event_result(packet))
    assert isinstance(outcome, live.VC003FailClosedSummary)
    assert outcome.code == code
    assert not called
    assert runner.selector is None


def test_runner_fail_closed_after_selection_for_cas_clock_and_extractor_errors() -> None:
    runner, packet, _ = _runner_setup(clock=lambda: 999)
    runner.source_store = SimpleNamespace(
        read=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing"))
    )
    runner.memory_cas = live.CapacityOneMemoryCAS()
    runner.extractor = SimpleNamespace(extract=lambda *_args, **_kwargs: _result(packet))
    outcome = runner.process_admission(_event_result(packet), checked_at_ns=1_001)
    assert isinstance(outcome, live.VC003FailClosedSummary)
    assert outcome.code.startswith("pixel_cas_")

    runner, packet, _ = _runner_setup(clock=lambda: 999)
    runner.extractor = SimpleNamespace(extract=lambda *_args, **_kwargs: _result(packet))
    assert isinstance(
        runner.process_admission(_event_result(packet), checked_at_ns=1_001),
        live.VC003SelectedRow,
    )
    runner.source_store.put(
        live.FULL_FRAME_PIXEL_SPEC,
        bytes(live.FULL_FRAME_PIXEL_SPEC.length),
        source_provenance_id="vc003-live",
        session_id="vc003-session",
        source_sequence=2,
    )
    later_at = live.BUCKET_DURATION_NS + 2_000
    later = _packet(frame_id=2, received_at_ns=later_at, digest=packet.content_hash)
    # The same fake extractor is replaced with a failing one for the next selected bucket.
    runner.extractor = SimpleNamespace(
        extract=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    outcome = runner.process_admission(_event_result(later), checked_at_ns=later_at + 1)
    assert isinstance(outcome, live.VC003FailClosedSummary)
    assert outcome.code == "marker_RuntimeError"


def test_runner_process_admission_rejected_and_poll_ingest_protocols() -> None:
    runner, packet, _ = _runner_setup(clock=lambda: 1_001)
    rejected_event = FrameAdmissionEvent(
        status=FrameAdmissionStatus.NO_FRAME,
        observed_at_ns=1_000,
        reason="empty",
    )
    rejected = FrameAdmissionResult(status=FrameAdmissionStatus.NO_FRAME, event=rejected_event)
    outcome = runner.process_admission(rejected)
    assert isinstance(outcome, live.VC003FailClosedSummary)
    assert outcome.code == "admission_no_frame"
    source = runner.source
    source.read = lambda: None
    admission, polled = runner.poll(now_ns=1_000, timeout_s=0)
    assert admission.packet is None and isinstance(polled, live.VC003FailClosedSummary)
    with pytest.raises(ValueError):
        runner.poll(timeout_s=-1)
    with pytest.raises(ValueError):
        runner.poll(timeout_s=True)
    assert packet.frame_id == 1


def test_runner_constructor_accepts_adapter_config_and_rejects_contract_mismatch() -> None:
    cas = live.CapacityOneMemoryCAS()
    adapter = live.build_frame_source_config(VC003SourceConfig())
    source = SimpleNamespace(read=lambda: None, pixel_store=cas)
    runner = live.VC003LiveMarkerRunner(source, source_config=adapter, pixel_store=cas)
    assert runner.adapter_config == adapter
    with pytest.raises(live.VC003LiveMarkerValidationError):
        live.VC003LiveMarkerRunner(
            source,
            source_config=replace(adapter, max_age_ns=1),
            pixel_store=cas,
        )
    with pytest.raises(TypeError):
        live.VC003LiveMarkerRunner(source, thresholds=object(), pixel_store=cas)  # type: ignore[arg-type]
