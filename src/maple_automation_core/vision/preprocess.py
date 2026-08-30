"""Deterministic, hash-bound preprocessing for the G1 observation boundary.

The public pipeline is deliberately narrower than a general image utility:
packed BGR8 source pixels are validated at the Pixel V1 boundary, cropped and
resized through the frozen FrameSource geometry, clipped to the DEC-001 ROI,
letterboxed with a fixed value, and emitted as an immutable RGB float32 NCHW
tensor.  Every geometric decision is recorded by :class:`PreprocessTransform`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from math import isfinite
from typing import Any, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from maple_automation_core.capture.pixel_store import PixelSpec, pixel_digest, validate_pixels
from maple_automation_core.domain.frame import FrameSize, SourceGeometry, SourceRect

PREPROCESS_VERSION = "g1-observation-preprocess-v1"
_BOX_LENGTH = 4


class PreprocessError(ValueError):
    """The source pixels or deterministic transform contract were invalid."""


def _ensure_finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PreprocessError(f"{field_name} must be a finite number.")
    result = float(value)
    if not isfinite(result):
        raise PreprocessError(f"{field_name} must be a finite number.")
    return result


def _box_tuple(box: Sequence[float], field_name: str) -> tuple[float, float, float, float]:
    if isinstance(box, str | bytes) or len(box) != _BOX_LENGTH:
        raise PreprocessError(f"{field_name} must contain four xyxy coordinates.")
    left, top, right, bottom = (
        _ensure_finite_number(value, f"{field_name}[{index}]") for index, value in enumerate(box)
    )
    if right <= left or bottom <= top:
        raise PreprocessError(f"{field_name} must have positive width and height.")
    return (left, top, right, bottom)


@dataclass(frozen=True, slots=True)
class NormalizedRoi:
    """Exclusive normalized ROI edges in working-image space."""

    left: float = 0.04
    top: float = 0.0
    right: float = 0.98
    bottom: float = 0.84

    def __post_init__(self) -> None:
        values = (
            _ensure_finite_number(self.left, "left"),
            _ensure_finite_number(self.top, "top"),
            _ensure_finite_number(self.right, "right"),
            _ensure_finite_number(self.bottom, "bottom"),
        )
        if not all(0.0 <= value <= 1.0 for value in values):
            raise PreprocessError("normalized ROI edges must be in [0, 1].")
        if values[2] <= values[0] or values[3] <= values[1]:
            raise PreprocessError("normalized ROI must have positive width and height.")
        object.__setattr__(self, "left", values[0])
        object.__setattr__(self, "top", values[1])
        object.__setattr__(self, "right", values[2])
        object.__setattr__(self, "bottom", values[3])

    def pixel_rect(self, size: FrameSize) -> SourceRect:
        if not isinstance(size, FrameSize):
            raise TypeError("size must be FrameSize.")
        # Mirrors the accepted Legacy candidate while making the rule part of
        # the hashed contract: Python round-to-nearest-even, then clamp.
        left = max(0, min(size.width - 1, round(self.left * size.width)))
        top = max(0, min(size.height - 1, round(self.top * size.height)))
        right = max(left + 1, min(size.width, round(self.right * size.width)))
        bottom = max(top + 1, min(size.height, round(self.bottom * size.height)))
        return SourceRect(x=left, y=top, width=right - left, height=bottom - top)

    def to_dict(self) -> dict[str, float]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }


def _pilot_geometry() -> SourceGeometry:
    return SourceGeometry(
        source_size=FrameSize(width=1920, height=1080),
        content_rect=SourceRect(x=277, y=167, width=1366, height=768),
        working_size=FrameSize(width=1296, height=700),
    )


@dataclass(frozen=True, slots=True)
class PreprocessConfig:
    """Hashable preprocessing configuration bound to one model contract."""

    geometry: SourceGeometry = field(default_factory=_pilot_geometry)
    roi: NormalizedRoi = field(default_factory=NormalizedRoi)
    model_size: FrameSize = field(default_factory=lambda: FrameSize(width=640, height=640))
    padding_value: int = 114
    version: str = PREPROCESS_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.geometry, SourceGeometry):
            raise TypeError("geometry must be SourceGeometry.")
        if not isinstance(self.roi, NormalizedRoi):
            raise TypeError("roi must be NormalizedRoi.")
        if not isinstance(self.model_size, FrameSize):
            raise TypeError("model_size must be FrameSize.")
        if isinstance(self.padding_value, bool) or not isinstance(self.padding_value, int):
            raise PreprocessError("padding_value must be an integer.")
        if not 0 <= self.padding_value <= 255:
            raise PreprocessError("padding_value must be in [0, 255].")
        if not isinstance(self.version, str) or not self.version.strip():
            raise PreprocessError("version must be a non-empty string.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "geometry": self.geometry.to_dict(),
            "roi": self.roi.to_dict(),
            "model_size": self.model_size.to_dict(),
            "padding_value": self.padding_value,
            "source_layout": "BGR8/HWC/uint8",
            "output_layout": "RGB/NCHW/float32",
            "normalization": "divide-by-255",
            "resize_interpolation": "opencv-inter-linear",
            "roi_rounding": "python-round-clamp-exclusive",
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PreprocessTransform:
    """Exact crop/resize/ROI/letterbox transform and inverse projection."""

    geometry: SourceGeometry
    roi: NormalizedRoi
    roi_rect: SourceRect
    model_size: FrameSize
    resized_size: FrameSize
    scale_x: float
    scale_y: float
    pad_left: int
    pad_top: int
    pad_right: int
    pad_bottom: int
    padding_value: int
    preprocess_sha256: str
    version: str = PREPROCESS_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.geometry, SourceGeometry):
            raise TypeError("geometry must be SourceGeometry.")
        if not isinstance(self.roi, NormalizedRoi):
            raise TypeError("roi must be NormalizedRoi.")
        if not isinstance(self.roi_rect, SourceRect):
            raise TypeError("roi_rect must be SourceRect.")
        if not isinstance(self.model_size, FrameSize) or not isinstance(
            self.resized_size, FrameSize
        ):
            raise TypeError("model_size and resized_size must be FrameSize.")
        scale_x = _ensure_finite_number(self.scale_x, "scale_x")
        scale_y = _ensure_finite_number(self.scale_y, "scale_y")
        if scale_x <= 0.0 or scale_y <= 0.0:
            raise PreprocessError("scale_x and scale_y must be positive.")
        if self.roi_rect != self.roi.pixel_rect(self.geometry.working_size):
            raise PreprocessError("roi_rect must be derived from roi and working_size.")
        for name in ("pad_left", "pad_top", "pad_right", "pad_bottom"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PreprocessError(f"{name} must be a non-negative integer.")
        if self.pad_left + self.resized_size.width + self.pad_right != self.model_size.width:
            raise PreprocessError("horizontal padding does not fill model_size.")
        if self.pad_top + self.resized_size.height + self.pad_bottom != self.model_size.height:
            raise PreprocessError("vertical padding does not fill model_size.")
        if scale_x != self.resized_size.width / self.roi_rect.width:
            raise PreprocessError("scale_x must match the rounded resize width.")
        if scale_y != self.resized_size.height / self.roi_rect.height:
            raise PreprocessError("scale_y must match the rounded resize height.")
        if len(self.preprocess_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.preprocess_sha256
        ):
            raise PreprocessError("preprocess_sha256 must be lowercase SHA-256 hex.")
        object.__setattr__(self, "scale_x", scale_x)
        object.__setattr__(self, "scale_y", scale_y)

    @property
    def scale(self) -> float:
        """Conservative scalar view; exact projection uses ``scale_x/scale_y``."""

        return min(self.scale_x, self.scale_y)

    @property
    def content_model_bounds(self) -> tuple[float, float, float, float]:
        return (
            float(self.pad_left),
            float(self.pad_top),
            float(self.pad_left + self.resized_size.width),
            float(self.pad_top + self.resized_size.height),
        )

    def working_box_to_model(self, box: Sequence[float]) -> tuple[float, float, float, float]:
        left, top, right, bottom = _box_tuple(box, "working box")
        roi_right = self.roi_rect.x2
        roi_bottom = self.roi_rect.y2
        if (
            left < self.roi_rect.x
            or top < self.roi_rect.y
            or right > roi_right
            or bottom > roi_bottom
        ):
            raise PreprocessError("working box must be contained by the configured ROI.")
        return (
            (left - self.roi_rect.x) * self.scale_x + self.pad_left,
            (top - self.roi_rect.y) * self.scale_y + self.pad_top,
            (right - self.roi_rect.x) * self.scale_x + self.pad_left,
            (bottom - self.roi_rect.y) * self.scale_y + self.pad_top,
        )

    def model_box_to_working(
        self,
        box: Sequence[float],
        *,
        clip: bool = True,
    ) -> tuple[float, float, float, float]:
        left, top, right, bottom = _box_tuple(box, "model box")
        if (
            left < 0.0
            or top < 0.0
            or right > self.model_size.width
            or bottom > self.model_size.height
        ):
            raise PreprocessError("model box must be contained by model_size.")
        mapped = (
            (left - self.pad_left) / self.scale_x + self.roi_rect.x,
            (top - self.pad_top) / self.scale_y + self.roi_rect.y,
            (right - self.pad_left) / self.scale_x + self.roi_rect.x,
            (bottom - self.pad_top) / self.scale_y + self.roi_rect.y,
        )
        if not clip:
            return mapped
        clipped = (
            max(float(self.roi_rect.x), min(float(self.roi_rect.x2), mapped[0])),
            max(float(self.roi_rect.y), min(float(self.roi_rect.y2), mapped[1])),
            max(float(self.roi_rect.x), min(float(self.roi_rect.x2), mapped[2])),
            max(float(self.roi_rect.y), min(float(self.roi_rect.y2), mapped[3])),
        )
        return _box_tuple(clipped, "clipped working box")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "preprocess_sha256": self.preprocess_sha256,
            "geometry": self.geometry.to_dict(),
            "roi": self.roi.to_dict(),
            "roi_rect": self.roi_rect.to_dict(),
            "model_size": self.model_size.to_dict(),
            "resized_size": self.resized_size.to_dict(),
            "scale": {"x": self.scale_x, "y": self.scale_y},
            "padding": {
                "left": self.pad_left,
                "top": self.pad_top,
                "right": self.pad_right,
                "bottom": self.pad_bottom,
                "value": self.padding_value,
            },
        }


@dataclass(frozen=True, slots=True)
class PreprocessResult:
    """Immutable tensor result and its byte/transform identities."""

    tensor: NDArray[np.float32] = field(repr=False, compare=False)
    transform: PreprocessTransform
    source_pixel_digest: str
    tensor_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.tensor, np.ndarray):
            raise TypeError("tensor must be a numpy array.")
        if not isinstance(self.transform, PreprocessTransform):
            raise TypeError("transform must be PreprocessTransform.")
        expected_shape = (
            1,
            3,
            self.transform.model_size.height,
            self.transform.model_size.width,
        )
        if self.tensor.shape != expected_shape or self.tensor.dtype != np.float32:
            raise PreprocessError(f"tensor must be float32 NCHW with shape {expected_shape!r}.")
        if self.tensor.flags.writeable or not self.tensor.flags.c_contiguous:
            raise PreprocessError("tensor must be immutable and C-contiguous.")
        for name, value in (
            ("source_pixel_digest", self.source_pixel_digest),
            ("tensor_sha256", self.tensor_sha256),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise PreprocessError(f"{name} must be lowercase SHA-256 hex.")
        if not np.isfinite(self.tensor).all():
            raise PreprocessError("tensor must contain only finite values.")
        actual_tensor_sha256 = sha256(memoryview(self.tensor).cast("B")).hexdigest()
        if self.tensor_sha256 != actual_tensor_sha256:
            raise PreprocessError("tensor_sha256 does not match the immutable tensor bytes.")


def build_transform(config: PreprocessConfig) -> PreprocessTransform:
    if not isinstance(config, PreprocessConfig):
        raise TypeError("config must be PreprocessConfig.")
    roi_rect = config.roi.pixel_rect(config.geometry.working_size)
    scale = min(
        config.model_size.width / roi_rect.width,
        config.model_size.height / roi_rect.height,
    )
    resized_width = max(1, round(roi_rect.width * scale))
    resized_height = max(1, round(roi_rect.height * scale))
    pad_left = (config.model_size.width - resized_width) // 2
    pad_top = (config.model_size.height - resized_height) // 2
    pad_right = config.model_size.width - resized_width - pad_left
    pad_bottom = config.model_size.height - resized_height - pad_top
    return PreprocessTransform(
        geometry=config.geometry,
        roi=config.roi,
        roi_rect=roi_rect,
        model_size=config.model_size,
        resized_size=FrameSize(width=resized_width, height=resized_height),
        scale_x=resized_width / roi_rect.width,
        scale_y=resized_height / roi_rect.height,
        pad_left=pad_left,
        pad_top=pad_top,
        pad_right=pad_right,
        pad_bottom=pad_bottom,
        padding_value=config.padding_value,
        preprocess_sha256=config.digest,
        version=config.version,
    )


def preprocess_pixels(
    pixels: object,
    spec: PixelSpec,
    *,
    config: PreprocessConfig | None = None,
    expected_pixel_digest: str | None = None,
) -> PreprocessResult:
    """Validate Pixel V1 bytes and return one deterministic model tensor."""

    if not isinstance(spec, PixelSpec):
        raise TypeError("spec must be PixelSpec.")
    selected = config if config is not None else PreprocessConfig()
    if not isinstance(selected, PreprocessConfig):
        raise TypeError("config must be PreprocessConfig or None.")
    source_size = selected.geometry.source_size
    if (spec.width, spec.height) != (source_size.width, source_size.height):
        raise PreprocessError("PixelSpec dimensions must match preprocessing source geometry.")

    try:
        data = validate_pixels(spec, pixels)
        actual_pixel_digest = pixel_digest(spec, data)
    except (TypeError, ValueError) as exc:
        raise PreprocessError(str(exc)) from exc
    if expected_pixel_digest is not None and actual_pixel_digest != expected_pixel_digest:
        raise PreprocessError("expected_pixel_digest does not match canonical Pixel V1 bytes.")

    transform = build_transform(selected)
    source = np.frombuffer(data, dtype=np.uint8).reshape(spec.shape)
    content_rect = selected.geometry.content_rect
    content = source[content_rect.y : content_rect.y2, content_rect.x : content_rect.x2]
    working = cast(
        NDArray[np.uint8],
        cv2.resize(
            content,
            (selected.geometry.working_size.width, selected.geometry.working_size.height),
            interpolation=cv2.INTER_LINEAR,
        ),
    )
    roi_rect = transform.roi_rect
    roi_pixels = working[roi_rect.y : roi_rect.y2, roi_rect.x : roi_rect.x2]
    resized = cast(
        NDArray[np.uint8],
        cv2.resize(
            roi_pixels,
            (transform.resized_size.width, transform.resized_size.height),
            interpolation=cv2.INTER_LINEAR,
        ),
    )
    canvas: NDArray[np.uint8] = np.full(
        (selected.model_size.height, selected.model_size.width, 3),
        selected.padding_value,
        dtype=np.uint8,
    )
    canvas[
        transform.pad_top : transform.pad_top + transform.resized_size.height,
        transform.pad_left : transform.pad_left + transform.resized_size.width,
    ] = resized

    rgb_chw = canvas[:, :, ::-1].transpose(2, 0, 1)
    tensor = np.ascontiguousarray(rgb_chw[np.newaxis, ...], dtype=np.float32)
    tensor /= np.float32(255.0)
    if not np.isfinite(tensor).all():
        raise PreprocessError("normalization produced non-finite tensor values.")
    tensor.setflags(write=False)
    tensor_sha256 = sha256(memoryview(tensor).cast("B")).hexdigest()
    return PreprocessResult(
        tensor=tensor,
        transform=transform,
        source_pixel_digest=actual_pixel_digest,
        tensor_sha256=tensor_sha256,
    )


PILOT_PREPROCESS_CONFIG = PreprocessConfig()


__all__ = [
    "PILOT_PREPROCESS_CONFIG",
    "PREPROCESS_VERSION",
    "NormalizedRoi",
    "PreprocessConfig",
    "PreprocessError",
    "PreprocessResult",
    "PreprocessTransform",
    "build_transform",
    "preprocess_pixels",
]
