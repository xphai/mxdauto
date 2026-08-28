from __future__ import annotations

import pytest

from maple_automation_core.domain.coordinates import PixelCoordinate
from maple_automation_core.domain.player_world import (
    PlayerState,
    Visibility,
    WorldObservation,
    WorldState,
)


def _player() -> PlayerState:
    return PlayerState(
        session_id="session-1",
        player_id="p1",
        source_frame_id=33,
        position=PixelCoordinate(12, 34),
        visibility=Visibility.VISIBLE,
        confidence=0.75,
        generation=2,
        freshness_ns=100,
    )


def test_player_state_and_world_state_roundtrip() -> None:
    player = _player()
    world = WorldState(
        session_id="session-1",
        frame_id=33,
        player=player,
        world_state_version=1,
        observed_at_ns=200,
        generation=3,
        observations=(
            WorldObservation(
                key="monster_count",
                source_frame_id=33,
                generation=1,
                confidence=0.9,
                freshness_ns=100,
                payload={"count": 4},
            ),
            WorldObservation(
                key="boss_alert",
                source_frame_id=32,
                generation=1,
                confidence=0.8,
                freshness_ns=50,
                payload={"nearby": True},
            ),
        ),
    )
    as_dict = world.to_dict()
    assert as_dict["player"]["visibility"] == Visibility.VISIBLE.value
    assert as_dict["player"]["confidence"] == 0.75
    assert as_dict["player"]["freshness_ns"] == 100
    assert as_dict["world_state_version"] == 1
    assert WorldState.from_dict(as_dict) == world


def test_player_visibility_and_confidence_fields() -> None:
    player = _player()
    assert player.visibility is Visibility.VISIBLE
    assert player.confidence == 0.75
    assert player.generation == 2
    assert player.freshness_ns == 100


def test_player_state_illegal_confidence_and_generation() -> None:
    with pytest.raises(ValueError):
        PlayerState(
            session_id="session-1",
            player_id="p1",
            source_frame_id=1,
            position=PixelCoordinate(1, 2),
            visibility=Visibility.VISIBLE,
            confidence=1.2,
            generation=-1,
            freshness_ns=100,
        )


def test_lost_player_position_rules() -> None:
    player = PlayerState(
        session_id="session-1",
        player_id="p1",
        source_frame_id=1,
        visibility=Visibility.LOST,
        confidence=0.00,
        generation=1,
        freshness_ns=100,
        position=None,
    )
    assert player.position is None
    with pytest.raises(ValueError):
        PlayerState(
            session_id="session-1",
            player_id="p1",
            source_frame_id=1,
            visibility=Visibility.VISIBLE,
            confidence=0.1,
            generation=1,
            freshness_ns=1,
            position=None,
        )


def test_world_state_session_mismatch_rejected() -> None:
    player = _player()
    with pytest.raises(ValueError):
        WorldState(
            session_id="session-2",
            frame_id=33,
            player=player,
            world_state_version=1,
            observed_at_ns=200,
            generation=3,
        )


def test_observation_keys_must_be_unique() -> None:
    player = _player()
    with pytest.raises(ValueError):
        WorldState(
            session_id="session-1",
            frame_id=33,
            player=player,
            world_state_version=1,
            observed_at_ns=200,
            generation=3,
            observations=(
                WorldObservation(
                    key="x",
                    source_frame_id=33,
                    generation=1,
                    confidence=1.0,
                    freshness_ns=1,
                    payload={"a": 1},
                ),
                WorldObservation(
                    key="x",
                    source_frame_id=33,
                    generation=2,
                    confidence=1.0,
                    freshness_ns=1,
                    payload={"b": 2},
                ),
            ),
        )


def test_world_state_rejects_player_from_different_frame() -> None:
    player = _player()
    with pytest.raises(ValueError):
        WorldState(
            session_id="session-1",
            frame_id=34,
            player=player,
            world_state_version=1,
            observed_at_ns=200,
            generation=3,
        )


def test_world_state_rejects_nested_session_version_and_generation_drift() -> None:
    with pytest.raises(ValueError, match="observation.session_id"):
        WorldState(
            session_id="session-1",
            frame_id=33,
            player=_player(),
            world_state_version=1,
            observed_at_ns=200,
            generation=3,
            observations=(
                WorldObservation(
                    key="x",
                    session_id="session-2",
                    source_frame_id=33,
                    generation=1,
                    confidence=1.0,
                    freshness_ns=1,
                    payload={},
                ),
            ),
        )

    with pytest.raises(ValueError, match="source_world_state_version"):
        WorldState(
            session_id="session-1",
            frame_id=33,
            player=PlayerState(
                session_id="session-1",
                player_id="p1",
                source_frame_id=33,
                position=PixelCoordinate(1, 2),
                visibility=Visibility.VISIBLE,
                confidence=1.0,
                generation=1,
                freshness_ns=1,
                source_world_state_version=2,
            ),
            world_state_version=1,
            observed_at_ns=200,
            generation=3,
        )

    with pytest.raises(ValueError, match="generation"):
        WorldState(
            session_id="session-1",
            frame_id=33,
            player=_player(),
            world_state_version=1,
            observed_at_ns=200,
            generation=1,
        )
