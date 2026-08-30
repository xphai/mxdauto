from __future__ import annotations

from dataclasses import FrozenInstanceError
from math import inf, nan

import pytest

from maple_automation_core.domain.coordinates import PixelCoordinate, WorldCoordinate
from maple_automation_core.domain.frame import FrameSize
from maple_automation_core.localization.transform import LocalizationTransform

CALIBRATION = "ab" * 32
MAP_FINGERPRINT = "cd" * 32
WORKING_SIZE = FrameSize(width=640, height=480)


def _transform() -> LocalizationTransform:
    return LocalizationTransform(
        matrix=((2.0, 0.5, 10.0), (-0.25, 1.5, -7.0)),
        map_id="map-01",
        map_fingerprint_sha256=MAP_FINGERPRINT,
        profile_id="profile-01",
        transform_version="world-v1",
        calibration_sha256=CALIBRATION,
        working_size=WORKING_SIZE,
    )


def test_apply_accepts_pixel_and_floating_points_at_inclusive_boundaries() -> None:
    transform = _transform()

    assert transform.apply(
        PixelCoordinate(0, 0),
        calibration_sha256=CALIBRATION,
        working_size=WORKING_SIZE,
    ) == WorldCoordinate(10.0, -7.0)
    assert transform.apply(
        (640.0, 480.0),
        calibration_sha256=CALIBRATION,
        working_size=WORKING_SIZE,
    ) == WorldCoordinate(
        10.0 + 2.0 * 640.0 + 0.5 * 480.0,
        -7.0 - 0.25 * 640.0 + 1.5 * 480.0,
    )


def test_apply_and_inverse_are_deterministic_for_floating_points() -> None:
    transform = _transform()
    point = (123.25, 456.5)

    first = transform.apply(point, calibration_sha256=CALIBRATION, working_size=WORKING_SIZE)
    second = transform.apply(point, calibration_sha256=CALIBRATION, working_size=WORKING_SIZE)
    assert first == second
    assert transform.to_working(
        first,
        calibration_sha256=CALIBRATION,
        working_size=WORKING_SIZE,
    ) == pytest.approx(point)


def test_serialization_and_digest_are_canonical() -> None:
    transform = _transform()
    payload = transform.to_dict()
    restored = LocalizationTransform.from_dict(
        payload,
        expected_calibration_sha256=CALIBRATION,
        expected_working_size=WORKING_SIZE,
    )

    assert payload == {
        "map_id": "map-01",
        "map_fingerprint_sha256": MAP_FINGERPRINT,
        "profile_id": "profile-01",
        "transform_version": "world-v1",
        "calibration_sha256": CALIBRATION,
        "working_size": {"width": 640, "height": 480},
        "matrix": [[2.0, 0.5, 10.0], [-0.25, 1.5, -7.0]],
    }
    assert restored == transform
    assert restored.digest == transform.digest
    assert len(transform.digest) == 64


def test_transform_is_immutable_and_freezes_matrix() -> None:
    transform = _transform()

    with pytest.raises(FrozenInstanceError):
        transform.map_id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        transform.matrix[0][0] = 3.0  # type: ignore[index]


@pytest.mark.parametrize(
    "matrix",
    [
        ((1.0, 2.0, 0.0), (2.0, 4.0, 1.0)),
        ((1.0, 2.0), (3.0, 4.0)),
        ((1.0, nan, 0.0), (0.0, 1.0, 0.0)),
        ((1.0, inf, 0.0), (0.0, 1.0, 0.0)),
    ],
)
def test_constructor_rejects_degenerate_nonfinite_or_malformed_matrix(matrix: object) -> None:
    with pytest.raises(ValueError):
        LocalizationTransform(
            matrix=matrix,  # type: ignore[arg-type]
            map_id="map-01",
            map_fingerprint_sha256=MAP_FINGERPRINT,
            profile_id="profile-01",
            transform_version="world-v1",
            calibration_sha256=CALIBRATION,
            working_size=WORKING_SIZE,
        )


def test_constructor_rejects_invalid_identity_and_size() -> None:
    with pytest.raises(ValueError):
        LocalizationTransform(
            matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            map_id=" ",
            map_fingerprint_sha256=MAP_FINGERPRINT,
            profile_id="profile-01",
            transform_version="world-v1",
            calibration_sha256=CALIBRATION,
            working_size=WORKING_SIZE,
        )
    with pytest.raises(ValueError):
        LocalizationTransform(
            matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            map_id="map-01",
            map_fingerprint_sha256=MAP_FINGERPRINT,
            profile_id="profile-01",
            transform_version="world-v1",
            calibration_sha256="not-a-sha",
            working_size=WORKING_SIZE,
        )
    with pytest.raises(TypeError):
        LocalizationTransform(
            matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            map_id="map-01",
            map_fingerprint_sha256=MAP_FINGERPRINT,
            profile_id="profile-01",
            transform_version="world-v1",
            calibration_sha256=CALIBRATION,
            working_size=(640, 480),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "point",
    [
        PixelCoordinate(641, 480),
        PixelCoordinate(640, 481),
        (-0.01, 10.0),
        (10.0, 480.01),
        (nan, 1.0),
        (1.0, inf),
        (1.0,),
    ],
)
def test_apply_rejects_nonfinite_or_out_of_bounds_working_points(point: object) -> None:
    transform = _transform()
    with pytest.raises(ValueError):
        transform.apply(point, calibration_sha256=CALIBRATION, working_size=WORKING_SIZE)  # type: ignore[arg-type]


def test_apply_rejects_calibration_and_working_size_mismatch() -> None:
    transform = _transform()
    with pytest.raises(ValueError, match="calibration_sha256"):
        transform.apply(
            PixelCoordinate(1, 2),
            calibration_sha256="cd" * 32,
            working_size=WORKING_SIZE,
        )
    with pytest.raises(ValueError, match="working_size"):
        transform.apply(
            PixelCoordinate(1, 2),
            calibration_sha256=CALIBRATION,
            working_size=FrameSize(width=320, height=240),
        )
    with pytest.raises(TypeError):
        transform.apply(PixelCoordinate(1, 2), calibration_sha256=CALIBRATION)  # type: ignore[call-arg]


def test_from_dict_rejects_external_context_mismatch() -> None:
    payload = _transform().to_dict()
    with pytest.raises(ValueError, match="calibration_sha256"):
        LocalizationTransform.from_dict(
            payload,
            expected_calibration_sha256="cd" * 32,
            expected_working_size=WORKING_SIZE,
        )
    with pytest.raises(ValueError, match="working_size"):
        LocalizationTransform.from_dict(
            payload,
            expected_calibration_sha256=CALIBRATION,
            expected_working_size=FrameSize(width=1, height=1),
        )
    with pytest.raises(ValueError, match="expected_map_fingerprint_sha256"):
        LocalizationTransform.from_dict(
            payload,
            expected_map_fingerprint_sha256="ef" * 32,
        )
    with pytest.raises(ValueError, match="expected_digest"):
        LocalizationTransform.from_dict(payload, expected_digest="01" * 32)


def test_from_dict_rejects_contradictory_matrix_alias() -> None:
    payload = _transform().to_dict()
    payload["affine_matrix"] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]

    with pytest.raises(ValueError, match="contradict"):
        LocalizationTransform.from_dict(payload)
