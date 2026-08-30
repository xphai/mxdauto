from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest

from maple_automation_core.capture.frame_source import canonical_calibration_sha256
from maple_automation_core.capture.pixel_store import PixelSpec, PixelStore
from maple_automation_core.domain.frame import (
    CaptureHealth,
    FramePacket,
    FrameSize,
    SourceGeometry,
    SourceRect,
)
from maple_automation_core.localization.minimap_marker import (
    MinimapMarkerConfig,
    MinimapMarkerExtractor,
    MinimapMarkerFaultCode,
    MinimapMarkerResult,
    MinimapMarkerStatus,
)


def _geometry() -> SourceGeometry:
    return SourceGeometry(
        source_size=FrameSize(width=32, height=24),
        content_rect=SourceRect(x=2, y=3, width=28, height=18),
        working_size=FrameSize(width=14, height=9),
    )


def _config() -> MinimapMarkerConfig:
    return MinimapMarkerConfig(
        geometry=_geometry(),
        minimap_roi=SourceRect(x=2, y=3, width=28, height=18),
        transform_version="capture-v1",
        source_id="source",
        session_id="session",
        clock_domain="monotonic",
    )


def _frame(store: PixelStore, pixels: np.ndarray, *, received_at_ns: int = 110) -> FramePacket:
    spec = PixelSpec(width=32, height=24)
    digest = store.put(spec, pixels)
    geometry = _geometry()
    return FramePacket(
        source_id="source",
        session_id="session",
        frame_id=7,
        captured_at_ns=100,
        received_at_ns=received_at_ns,
        transform_version="capture-v1",
        clock_domain="monotonic",
        content_hash=digest,
        source_geometry=geometry,
        image_ref=f"cas://sha256/{digest}",
        capture_health=CaptureHealth(
            session_id="session",
            frame_id=7,
            source_id="source",
            content_hash=digest,
            clock_domain="monotonic",
            captured_at_ns=100,
            received_at_ns=received_at_ns,
            transform_version="capture-v1",
            max_age_ns=1_000,
        ),
        image_metadata={
            "pixel_spec": spec.to_dict(),
            "geometry_sha256": _config().geometry_sha256,
            "calibration_sha256": canonical_calibration_sha256(geometry, "capture-v1"),
        },
    )


def _pixels(color: tuple[int, int, int] | None = None) -> np.ndarray:
    result = np.zeros((24, 32, 3), dtype=np.uint8)
    if color is not None:
        result[8:11, 10:13] = color
    return result


def test_unique_yellow_marker_emits_anonymous_candidate_and_lineage() -> None:
    # A temporary directory keeps the test on the same verified CAS path used
    # by the production adapter.
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        frame = _frame(store, _pixels((0, 255, 255)))
        result = MinimapMarkerExtractor(_config(), store).extract(frame, now_ns=200)

    assert result.status is MinimapMarkerStatus.CANDIDATE
    assert result.succeeded
    assert result.candidate is not None
    assert result.candidate.subject_id == "anonymous-player"
    assert result.candidate.pixel_digest == frame.content_hash
    assert result.candidate.evidence_digest == result.evidence.digest  # type: ignore[union-attr]
    assert result.candidate.evidence_source.value == "minimap_yellow_marker"
    assert result.marker is not None
    assert result.marker.area == 9
    assert result.marker.bright_core_pixels == 9
    assert result.candidate.anchor_working is not None
    assert result.candidate.anchor_working.x == pytest.approx(4.5)
    assert result.candidate.anchor_working.y == pytest.approx(3.0)


def test_orange_is_excluded() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        pixels = _pixels()
        pixels[8:11, 10:13] = (0, 205, 255)
        result = MinimapMarkerExtractor(_config(), store).extract(_frame(store, pixels), now_ns=200)

    assert result.status is MinimapMarkerStatus.NO_CANDIDATE
    assert result.candidate is None
    assert result.evidence is not None
    assert result.evidence.component_count == 0


def test_multiple_yellow_components_are_ambiguous() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        pixels = _pixels()
        pixels[8:11, 10:13] = (0, 255, 255)
        pixels[15:18, 20:23] = (0, 255, 255)
        result = MinimapMarkerExtractor(_config(), store).extract(_frame(store, pixels), now_ns=200)

    assert result.status is MinimapMarkerStatus.NO_CANDIDATE
    assert result.candidate is None
    assert result.evidence is not None and result.evidence.component_count == 2


def test_empty_frame_has_no_candidate_and_result_roundtrips_hash_only() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        result = MinimapMarkerExtractor(_config(), store).extract(
            _frame(store, _pixels()), now_ns=200
        )

    assert result.status is MinimapMarkerStatus.NO_CANDIDATE
    payload = result.to_dict()
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
    assert "raw_pixels" not in encoded
    assert "mask" not in encoded
    assert "pixel_bytes" not in encoded
    assert MinimapMarkerResult.from_dict(json.loads(encoded)) == result
    assert result.digest == MinimapMarkerResult.from_dict(payload).digest


def test_validation_is_fail_closed_for_stale_and_wrong_image_ref() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        frame = _frame(store, _pixels((0, 255, 255)))
        extractor = MinimapMarkerExtractor(_config(), store)
        stale = extractor.extract(frame, now_ns=2_000)
        wrong_ref = extractor.extract(replace(frame, image_ref="frame://raw"), now_ns=200)

    assert stale.status is MinimapMarkerStatus.FAULT
    assert stale.fault is not None and stale.fault.code is MinimapMarkerFaultCode.STALE
    assert stale.candidate is None
    assert wrong_ref.status is MinimapMarkerStatus.FAULT
    assert wrong_ref.fault is not None
    assert wrong_ref.fault.code is MinimapMarkerFaultCode.IMAGE_REF_MISMATCH
    assert wrong_ref.evidence is not None
    assert wrong_ref.fault.image_ref == f"cas://sha256/{frame.content_hash}"
    assert wrong_ref.evidence.image_ref == f"cas://sha256/{frame.content_hash}"
    assert "frame://raw" not in wrong_ref.to_json()


def test_config_is_frozen_and_digest_roundtrips() -> None:
    config = _config()
    assert config.digest == MinimapMarkerConfig.from_dict(config.to_dict()).digest
    with pytest.raises(FrozenInstanceError):
        config.area_max = 1  # type: ignore[misc]


def test_roi_is_required_and_must_fit_content_rect() -> None:
    import tempfile

    unconfigured = MinimapMarkerConfig(
        geometry=_geometry(),
        transform_version="capture-v1",
        source_id="source",
        session_id="session",
        clock_domain="monotonic",
    )
    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        result = MinimapMarkerExtractor(unconfigured, store).extract(
            _frame(store, _pixels((0, 255, 255))), now_ns=200
        )

    assert result.status is MinimapMarkerStatus.FAULT
    assert result.fault is not None
    assert result.fault.code is MinimapMarkerFaultCode.ROI_UNCONFIGURED
    with pytest.raises(ValueError):
        MinimapMarkerConfig(
            geometry=_geometry(),
            minimap_roi=SourceRect(x=1, y=3, width=28, height=18),
        )


def test_detection_is_limited_to_roi_and_offsets_geometry_to_source() -> None:
    import tempfile

    config = MinimapMarkerConfig(
        geometry=_geometry(),
        minimap_roi=SourceRect(x=8, y=7, width=10, height=10),
        transform_version="capture-v1",
        source_id="source",
        session_id="session",
        clock_domain="monotonic",
    )
    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        result = MinimapMarkerExtractor(config, store).extract(
            _frame(store, _pixels((0, 255, 255))), now_ns=200
        )

    assert result.status is MinimapMarkerStatus.CANDIDATE
    assert result.marker is not None
    assert result.marker.source_bbox == SourceRect(x=10, y=8, width=3, height=3)
    assert result.marker.source_centroid == pytest.approx((11.0, 9.0))


def test_explicit_observation_time_and_generation_are_bound_to_candidate() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        frame = _frame(store, _pixels((0, 255, 255)))
        result = MinimapMarkerExtractor(_config(), store).extract(
            frame,
            now_ns=200,
            observed_at_ns=150,
            generation=2,
        )

    assert result.status is MinimapMarkerStatus.CANDIDATE
    assert result.candidate is not None
    assert result.evidence is not None
    assert result.candidate.observed_at_ns == 150
    assert result.candidate.generation == 2
    assert result.evidence.observed_at_ns == 150


def test_observation_time_and_clock_are_fail_closed() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        frame = _frame(store, _pixels((0, 255, 255)))
        extractor = MinimapMarkerExtractor(_config(), store)
        missing_clock = extractor.extract(frame)
        before_receive = extractor.extract(frame, now_ns=105)
        before_receive_observed = extractor.extract(frame, now_ns=200, observed_at_ns=105)
        before_capture_observed = extractor.extract(frame, now_ns=200, observed_at_ns=99)
        after_check_observed = extractor.extract(frame, now_ns=200, observed_at_ns=201)
        delayed = extractor.extract(frame, now_ns=2_000)

    for result in (
        missing_clock,
        before_receive,
        before_receive_observed,
        before_capture_observed,
        after_check_observed,
    ):
        assert result.status is MinimapMarkerStatus.FAULT
        assert result.candidate is None
    assert missing_clock.fault is not None
    assert missing_clock.fault.code is MinimapMarkerFaultCode.TIMESTAMP_MISMATCH
    assert missing_clock.evidence is not None
    assert missing_clock.evidence.checked_at_ns == frame.received_at_ns
    assert missing_clock.evidence.observed_at_ns == frame.received_at_ns
    assert missing_clock.fault.failed_at_ns == frame.received_at_ns
    assert before_receive.fault is not None
    assert before_receive.fault.code is MinimapMarkerFaultCode.TIMESTAMP_MISMATCH
    assert before_receive_observed.fault is not None
    assert before_receive_observed.fault.code is MinimapMarkerFaultCode.TIMESTAMP_MISMATCH
    assert before_capture_observed.fault is not None
    assert before_capture_observed.fault.code is MinimapMarkerFaultCode.TIMESTAMP_MISMATCH
    assert after_check_observed.fault is not None
    assert after_check_observed.fault.code is MinimapMarkerFaultCode.TIMESTAMP_MISMATCH
    assert delayed.fault is not None and delayed.fault.code is MinimapMarkerFaultCode.STALE


def test_bright_core_is_independent_and_rejects_blue_edge_pixels() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        result = MinimapMarkerExtractor(_config(), store).extract(
            _frame(store, _pixels((55, 255, 255))), now_ns=200
        )

    assert result.status is MinimapMarkerStatus.NO_CANDIDATE
    assert result.evidence is not None
    assert result.evidence.component_count == 0


def test_compressed_yellow_core_uses_configured_limits() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        result = MinimapMarkerExtractor(_config(), store).extract(
            _frame(store, _pixels((40, 240, 240))), now_ns=200
        )

    assert result.status is MinimapMarkerStatus.CANDIDATE
    assert result.marker is not None
    assert result.marker.bright_core_pixels == 9


def test_core_limits_are_serialized_and_tampering_is_rejected() -> None:
    config = _config()
    payload = config.to_dict()
    assert payload["bright_b_max"] == 50
    assert payload["bright_green_red_min"] == 220
    assert payload["bright_saturation_min"] == 205
    assert payload["bright_value_min"] == 240
    assert MinimapMarkerConfig.from_dict(payload) == config

    for field_name in (
        "bright_b_max",
        "bright_green_red_min",
        "bright_saturation_min",
        "bright_value_min",
    ):
        tampered = dict(payload)
        tampered[field_name] = tampered[field_name] - 1
        with pytest.raises(ValueError):
            MinimapMarkerConfig.from_dict(tampered)


def test_subject_id_is_fixed_anonymous_and_not_serialized_from_custom_identity() -> None:
    with pytest.raises(ValueError):
        MinimapMarkerConfig(geometry=_geometry(), subject_id="alice@example.com")

    config_payload = _config().to_hash_only()
    assert "alice@example.com" not in json.dumps(config_payload, sort_keys=True)


def test_result_rejects_nonanonymous_candidate_from_hash_only_payload() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        result = MinimapMarkerExtractor(_config(), store).extract(
            _frame(store, _pixels((0, 255, 255))), now_ns=200
        )

    assert result.candidate is not None
    payload = json.loads(result.to_json())
    payload["candidate"]["subject_id"] = "alice@example.com"
    with pytest.raises(ValueError):
        MinimapMarkerResult.from_dict(payload)
    assert "alice@example.com" not in result.to_json()
