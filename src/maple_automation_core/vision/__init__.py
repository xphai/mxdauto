"""Read-only vision contracts for deterministic observation construction."""

from .observation_adapter import (
    DetectorBackend,
    DetectorOutput,
    ObservationAdapter,
)
from .preprocess import (
    PILOT_PREPROCESS_CONFIG,
    PREPROCESS_VERSION,
    NormalizedRoi,
    PreprocessConfig,
    PreprocessError,
    PreprocessResult,
    PreprocessTransform,
    build_transform,
    preprocess_pixels,
)

__all__ = [
    "PILOT_PREPROCESS_CONFIG",
    "PREPROCESS_VERSION",
    "DetectorBackend",
    "DetectorOutput",
    "NormalizedRoi",
    "ObservationAdapter",
    "PreprocessConfig",
    "PreprocessError",
    "PreprocessResult",
    "PreprocessTransform",
    "build_transform",
    "preprocess_pixels",
]
