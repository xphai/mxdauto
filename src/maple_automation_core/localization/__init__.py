"""Deterministic, read-only player localization contracts."""

from .platform import (
    PlatformGraph,
    PlatformMatch,
    PlatformMatchStatus,
    PlatformSegment,
)
from .player_localizer import (
    IdentityStatus,
    LocalizationFault,
    LocalizationFaultCode,
    LocalizationPolicy,
    LocalizationResult,
    LocalizationStatus,
    LocationState,
    PlayerAnchorSource,
    PlayerCandidate,
    PlayerLocation,
    WorkingPoint,
    resolve_player_location,
)
from .transform import LocalizationTransform

__all__ = [
    "IdentityStatus",
    "LocalizationFault",
    "LocalizationFaultCode",
    "LocalizationPolicy",
    "LocalizationResult",
    "LocalizationStatus",
    "LocalizationTransform",
    "LocationState",
    "PlatformGraph",
    "PlatformMatch",
    "PlatformMatchStatus",
    "PlatformSegment",
    "PlayerAnchorSource",
    "PlayerCandidate",
    "PlayerLocation",
    "WorkingPoint",
    "resolve_player_location",
]
