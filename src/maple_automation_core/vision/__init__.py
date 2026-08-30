"""Read-only vision contracts for deterministic observation construction."""

from .observation_adapter import (
    DetectorBackend,
    DetectorOutput,
    ObservationAdapter,
)
from .onnx_backend import (
    DEFAULT_ONNX_BACKEND_CONFIG,
    OnnxBackendConfig,
    OnnxBackendError,
    OnnxDetectorBackend,
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
    "DEFAULT_ONNX_BACKEND_CONFIG",
    "PILOT_PREPROCESS_CONFIG",
    "PREPROCESS_VERSION",
    "DetectorBackend",
    "DetectorOutput",
    "NormalizedRoi",
    "ObservationAdapter",
    "OnnxBackendConfig",
    "OnnxBackendError",
    "OnnxDetectorBackend",
    "PreprocessConfig",
    "PreprocessError",
    "PreprocessResult",
    "PreprocessTransform",
    "build_transform",
    "preprocess_pixels",
]
