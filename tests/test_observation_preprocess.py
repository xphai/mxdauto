from __future__ import annotations

from math import nan

import numpy as np
import pytest

from maple_automation_core.capture.pixel_store import PixelSpec, pixel_digest
from maple_automation_core.domain.frame import FrameSize, SourceGeometry, SourceRect
from maple_automation_core.vision.preprocess import (
    NormalizedRoi,
    PreprocessConfig,
    PreprocessError,
    PreprocessResult,
    build_transform,
    preprocess_pixels,
)


def _solid_bgr_bytes(blue: int = 10, green: int = 20, red: int = 30) -> bytes:
    pixel = bytes((blue, green, red))
    return pixel * (1920 * 1080)


def test_pilot_transform_has_frozen_roi_and_letterbox_golden_values() -> None:
    transform = build_transform(PreprocessConfig())

    assert transform.roi_rect == SourceRect(x=52, y=0, width=1218, height=588)
    assert transform.resized_size == FrameSize(width=640, height=309)
    assert transform.scale == pytest.approx(640 / 1218)
    assert transform.scale_x == pytest.approx(640 / 1218)
    assert transform.scale_y == pytest.approx(309 / 588)
    assert (
        transform.pad_left,
        transform.pad_top,
        transform.pad_right,
        transform.pad_bottom,
    ) == (0, 165, 0, 166)
    assert transform.content_model_bounds == (0.0, 165.0, 640.0, 474.0)
    assert len(transform.preprocess_sha256) == 64


def test_preprocess_emits_immutable_rgb_float32_nchw_and_is_repeatable() -> None:
    spec = PixelSpec(width=1920, height=1080)
    pixels = _solid_bgr_bytes()
    expected = pixel_digest(spec, pixels)

    first = preprocess_pixels(pixels, spec, expected_pixel_digest=expected)
    second = preprocess_pixels(pixels, spec, expected_pixel_digest=expected)
    third = preprocess_pixels(pixels, spec, expected_pixel_digest=expected)

    assert first.tensor.shape == (1, 3, 640, 640)
    assert first.tensor.dtype == np.float32
    assert first.tensor.flags.c_contiguous
    assert not first.tensor.flags.writeable
    assert first.tensor_sha256 == second.tensor_sha256
    assert first.tensor_sha256 == third.tensor_sha256
    assert first.transform.to_dict() == second.transform.to_dict()
    assert first.transform.to_dict() == third.transform.to_dict()
    assert first.source_pixel_digest == expected
    assert first.tensor[0, :, 0, 0] == pytest.approx(np.full(3, 114 / 255))
    assert first.tensor[0, :, 200, 200] == pytest.approx(np.array([30, 20, 10]) / 255)

    with pytest.raises(PreprocessError, match="tensor_sha256"):
        PreprocessResult(
            tensor=first.tensor,
            transform=first.transform,
            source_pixel_digest=first.source_pixel_digest,
            tensor_sha256="0" * 64,
        )


def test_box_projection_round_trips_working_coordinates() -> None:
    transform = build_transform(PreprocessConfig())
    working = (100.0, 100.0, 300.0, 300.0)

    model = transform.working_box_to_model(working)
    restored = transform.model_box_to_working(model)

    assert restored == pytest.approx(working)
    assert transform.model_box_to_working(transform.content_model_bounds) == pytest.approx(
        (52.0, 0.0, 1270.0, 588.0)
    )


def test_model_padding_is_clipped_and_empty_padding_box_is_rejected() -> None:
    transform = build_transform(PreprocessConfig())

    assert transform.model_box_to_working((10.0, 150.0, 100.0, 200.0)) == pytest.approx(
        (71.03125, 0.0, 242.3125, 66.60194174757281)
    )
    with pytest.raises(PreprocessError, match="positive width and height"):
        transform.model_box_to_working((10.0, 10.0, 100.0, 100.0))


def test_preprocess_rejects_dimension_digest_and_buffer_drift() -> None:
    pixels = _solid_bgr_bytes()

    with pytest.raises(PreprocessError, match="source geometry"):
        preprocess_pixels(pixels, PixelSpec(width=1280, height=720))
    with pytest.raises(PreprocessError, match="length"):
        preprocess_pixels(pixels[:-1], PixelSpec(width=1920, height=1080))
    with pytest.raises(PreprocessError, match="expected_pixel_digest"):
        preprocess_pixels(
            pixels,
            PixelSpec(width=1920, height=1080),
            expected_pixel_digest="0" * 64,
        )


@pytest.mark.parametrize(
    "roi",
    [
        NormalizedRoi(left=0.0, top=0.0, right=1.0, bottom=1.0),
        NormalizedRoi(left=0.1, top=0.2, right=0.9, bottom=0.8),
    ],
)
def test_preprocess_config_digest_binds_roi(roi: NormalizedRoi) -> None:
    config = PreprocessConfig(roi=roi)
    assert config.digest == PreprocessConfig(roi=roi).digest


def test_invalid_roi_geometry_and_boxes_fail_closed() -> None:
    with pytest.raises(PreprocessError):
        NormalizedRoi(left=0.8, top=0.0, right=0.2, bottom=1.0)
    with pytest.raises(PreprocessError, match="finite"):
        NormalizedRoi(left=nan, top=0.0, right=1.0, bottom=1.0)

    transform = build_transform(PreprocessConfig())
    with pytest.raises(PreprocessError, match="contained by the configured ROI"):
        transform.working_box_to_model((0.0, 0.0, 100.0, 100.0))
    with pytest.raises(PreprocessError, match="finite"):
        transform.model_box_to_working((0.0, 0.0, nan, 10.0))


def test_extreme_roi_rounding_stays_inside_a_one_pixel_working_size() -> None:
    roi = NormalizedRoi(left=0.9, top=0.9, right=1.0, bottom=1.0)

    assert roi.pixel_rect(FrameSize(width=1, height=1)) == SourceRect(
        x=0,
        y=0,
        width=1,
        height=1,
    )


def test_preprocess_rejects_geometry_that_disagrees_with_pixel_spec() -> None:
    config = PreprocessConfig(
        geometry=SourceGeometry(
            source_size=FrameSize(1280, 720),
            content_rect=SourceRect(0, 0, 1280, 720),
            working_size=FrameSize(1296, 700),
        )
    )
    with pytest.raises(PreprocessError, match="source geometry"):
        preprocess_pixels(_solid_bgr_bytes(), PixelSpec(), config=config)
