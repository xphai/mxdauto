from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ._contract_utils import (
    ensure_json_value,
    ensure_mapping,
    ensure_non_empty_str,
    ensure_non_negative_int,
    ensure_probability,
    ensure_time_ns,
    freeze_json_value,
    to_json_dict,
)
from .coordinates import PixelCoordinate, Velocity


class Visibility(Enum):
    VISIBLE = "visible"
    PARTIAL = "partial"
    OCCLUDED = "occluded"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class PlayerState:
    """Immutable per-player observation state."""

    session_id: str
    player_id: str
    source_frame_id: int
    visibility: Visibility
    confidence: float
    generation: int
    freshness_ns: int
    position: PixelCoordinate | None = None
    velocity: Velocity | None = None
    source_world_state_version: int | None = None

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.player_id, "player_id")
        ensure_non_negative_int(self.source_frame_id, "source_frame_id")
        if not isinstance(self.visibility, Visibility):
            raise TypeError("visibility must be Visibility.")
        if self.visibility is not Visibility.LOST and self.position is None:
            raise ValueError("position is required when visibility is not lost.")
        if self.visibility is Visibility.LOST and self.position is not None:
            raise ValueError("position must be None when visibility is lost.")
        ensure_probability(self.confidence, "confidence")
        object.__setattr__(self, "confidence", float(self.confidence))
        ensure_non_negative_int(self.generation, "generation")
        ensure_non_negative_int(self.freshness_ns, "freshness_ns")
        if self.velocity is not None and not isinstance(self.velocity, Velocity):
            raise TypeError("velocity must be Velocity or None.")
        if self.source_world_state_version is not None:
            ensure_non_negative_int(
                self.source_world_state_version,
                "source_world_state_version",
            )

    @property
    def has_position(self) -> bool:
        return self.position is not None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "session_id": self.session_id,
            "player_id": self.player_id,
            "source_frame_id": self.source_frame_id,
            "visibility": self.visibility.value,
            "confidence": self.confidence,
            "generation": self.generation,
            "freshness_ns": self.freshness_ns,
            "position": None if self.position is None else self.position.to_dict(),
            "velocity": None if self.velocity is None else self.velocity.to_dict(),
        }
        if self.source_world_state_version is not None:
            data["source_world_state_version"] = self.source_world_state_version
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PlayerState:
        values = ensure_mapping(data, "PlayerState payload")
        try:
            return cls(
                session_id=values["session_id"],
                player_id=values["player_id"],
                source_frame_id=values["source_frame_id"],
                visibility=Visibility(values["visibility"]),
                confidence=values["confidence"],
                generation=values["generation"],
                freshness_ns=values["freshness_ns"],
                position=None
                if values.get("position") is None
                else PixelCoordinate.from_dict(values["position"]),
                velocity=None
                if values.get("velocity") is None
                else Velocity.from_dict(values["velocity"]),
                source_world_state_version=values.get("source_world_state_version"),
            )
        except KeyError as exc:
            raise ValueError(f"PlayerState payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class WorldObservation:
    """Typed per-key observation record."""

    key: str
    source_frame_id: int
    generation: int
    confidence: float
    freshness_ns: int
    payload: Mapping[str, Any]
    session_id: str | None = None
    source_world_state_version: int | None = None

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.key, "key")
        ensure_non_negative_int(self.source_frame_id, "source_frame_id")
        ensure_non_negative_int(self.generation, "generation")
        ensure_probability(self.confidence, "confidence")
        object.__setattr__(self, "confidence", float(self.confidence))
        ensure_non_negative_int(self.freshness_ns, "freshness_ns")
        payload = ensure_mapping(self.payload, "payload")
        ensure_json_value(payload, "payload")
        object.__setattr__(
            self,
            "payload",
            freeze_json_value(payload),
        )
        if self.session_id is not None:
            ensure_non_empty_str(self.session_id, "session_id")
        if self.source_world_state_version is not None:
            ensure_non_negative_int(
                self.source_world_state_version,
                "source_world_state_version",
            )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "key": self.key,
            "source_frame_id": self.source_frame_id,
            "generation": self.generation,
            "confidence": self.confidence,
            "freshness_ns": self.freshness_ns,
            "payload": to_json_dict(self.payload),
        }
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.source_world_state_version is not None:
            data["source_world_state_version"] = self.source_world_state_version
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorldObservation:
        values = ensure_mapping(data, "WorldObservation payload")
        try:
            payload = values.get("payload", {})
            return cls(
                key=values["key"],
                source_frame_id=values["source_frame_id"],
                generation=values["generation"],
                confidence=values["confidence"],
                freshness_ns=values["freshness_ns"],
                payload=ensure_mapping(payload, "payload"),
                session_id=values.get("session_id"),
                source_world_state_version=values.get("source_world_state_version"),
            )
        except KeyError as exc:
            raise ValueError(f"WorldObservation payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class WorldState:
    """Immutable global world snapshot."""

    session_id: str
    frame_id: int
    player: PlayerState
    world_state_version: int
    observed_at_ns: int
    generation: int
    observations: tuple[WorldObservation, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_negative_int(self.world_state_version, "world_state_version")
        ensure_non_negative_int(self.frame_id, "frame_id")
        if not isinstance(self.player, PlayerState):
            raise TypeError("player must be PlayerState.")
        if self.player.session_id != self.session_id:
            raise ValueError("player.session_id must match world session_id.")
        if self.player.source_frame_id != self.frame_id:
            raise ValueError("player.source_frame_id must match world frame_id.")
        if (
            self.player.source_world_state_version is not None
            and self.player.source_world_state_version != self.world_state_version
        ):
            raise ValueError("player.source_world_state_version must match world_state_version.")
        ensure_time_ns(self.observed_at_ns, "observed_at_ns")
        ensure_non_negative_int(self.generation, "generation")
        if not isinstance(self.observations, tuple):
            raise TypeError("observations must be a tuple.")
        if any(not isinstance(obs, WorldObservation) for obs in self.observations):
            raise TypeError("observations must be WorldObservation tuple.")
        if any(obs.source_frame_id > self.frame_id for obs in self.observations):
            raise ValueError("observation source_frame_id must not be in the future.")
        if any(
            obs.session_id is not None and obs.session_id != self.session_id
            for obs in self.observations
        ):
            raise ValueError("observation.session_id must match world session_id.")
        if any(obs.generation > self.generation for obs in self.observations):
            raise ValueError("observation generation must not exceed world generation.")
        if any(
            obs.source_world_state_version is not None
            and obs.source_world_state_version > self.world_state_version
            for obs in self.observations
        ):
            raise ValueError(
                "observation source_world_state_version must not be newer than world state."
            )
        if self.player.generation > self.generation:
            raise ValueError("player generation must not exceed world generation.")
        keys = [obs.key for obs in self.observations]
        if len(set(keys)) != len(keys):
            raise ValueError("WorldObservation keys must be unique.")

    @property
    def visibility(self) -> Visibility:
        return self.player.visibility

    @property
    def confidence(self) -> float:
        return self.player.confidence

    @property
    def version(self) -> int:
        """Short alias used by planner and replay consumers."""

        return self.world_state_version

    @property
    def player_freshness(self) -> int:
        return self.player.freshness_ns

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "player": self.player.to_dict(),
            "world_state_version": self.world_state_version,
            "observed_at_ns": self.observed_at_ns,
            "generation": self.generation,
            "observations": [obs.to_dict() for obs in self.observations],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorldState:
        values = ensure_mapping(data, "WorldState payload")
        try:
            raw_observations = values.get("observations", [])
            if not isinstance(raw_observations, list | tuple):
                raise ValueError("observations must be a list or tuple.")
            observations = tuple(WorldObservation.from_dict(item) for item in raw_observations)
            return cls(
                session_id=values["session_id"],
                frame_id=values["frame_id"],
                player=PlayerState.from_dict(values["player"]),
                world_state_version=values["world_state_version"],
                observed_at_ns=values["observed_at_ns"],
                generation=values["generation"],
                observations=observations,
            )
        except KeyError as exc:
            raise ValueError(f"WorldState payload missing key: {exc.args[0]}") from exc
