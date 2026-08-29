"""Capture boundary contracts and frame admission policy."""

from .frame_source import (
    Clock,
    FrameAdmissionEvent,
    FrameAdmissionResult,
    FrameAdmissionStatus,
    FrameReader,
    FrameSource,
    FrameSourceAdapter,
    FrameSourceConfig,
    LatestFrameBuffer,
    RawFrame,
    calibration_sha256,
    canonical_calibration_hash,
    canonical_calibration_sha256,
    canonical_geometry_hash,
    canonical_geometry_sha256,
    geometry_sha256,
)

__all__ = [
    "Clock",
    "FrameAdmissionEvent",
    "FrameAdmissionResult",
    "FrameAdmissionStatus",
    "FrameReader",
    "FrameSource",
    "FrameSourceAdapter",
    "FrameSourceConfig",
    "LatestFrameBuffer",
    "RawFrame",
    "calibration_sha256",
    "canonical_calibration_hash",
    "canonical_calibration_sha256",
    "canonical_geometry_hash",
    "canonical_geometry_sha256",
    "geometry_sha256",
]
