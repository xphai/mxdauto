from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

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
        result = MinimapMarkerExtractor(_config(), store).extract(frame)

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
        result = MinimapMarkerExtractor(_config(), store).extract(_frame(store, pixels))

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
        result = MinimapMarkerExtractor(_config(), store).extract(_frame(store, pixels))

    assert result.status is MinimapMarkerStatus.NO_CANDIDATE
    assert result.candidate is None
    assert result.evidence is not None and result.evidence.component_count == 2


def test_empty_frame_has_no_candidate_and_result_roundtrips_hash_only() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        result = MinimapMarkerExtractor(_config(), store).extract(_frame(store, _pixels()))

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
    from dataclasses import replace

    with tempfile.TemporaryDirectory() as directory:
        store = PixelStore(directory)
        frame = _frame(store, _pixels((0, 255, 255)))
        extractor = MinimapMarkerExtractor(_config(), store)
        stale = extractor.extract(frame, now_ns=2_000)
        wrong_ref = extractor.extract(replace(frame, image_ref="frame://raw"))

    assert stale.status is MinimapMarkerStatus.FAULT
    assert stale.fault is not None and stale.fault.code is MinimapMarkerFaultCode.STALE
    assert stale.candidate is None
    assert wrong_ref.status is MinimapMarkerStatus.FAULT
    assert wrong_ref.fault is not None
    assert wrong_ref.fault.code is MinimapMarkerFaultCode.IMAGE_REF_MISMATCH


def test_config_is_frozen_and_digest_roundtrips() -> None:
    config = _config()
    assert config.digest == MinimapMarkerConfig.from_dict(config.to_dict()).digest
    with pytest.raises(FrozenInstanceError):
        config.area_max = 1  # type: ignore[misc]
