from __future__ import annotations

from dataclasses import FrozenInstanceError
from math import inf, nan

import pytest

from maple_automation_core.domain.coordinates import WorldCoordinate
from maple_automation_core.localization.platform import (
    PlatformGraph,
    PlatformMatchStatus,
    PlatformSegment,
)

SHA = "ab" * 32


def _segment(
    platform_id: str,
    start: tuple[float, float],
    end: tuple[float, float],
) -> PlatformSegment:
    return PlatformSegment(
        platform_id=platform_id,
        start=WorldCoordinate(*start),
        end=WorldCoordinate(*end),
    )


def _graph(*platforms: PlatformSegment, margin: float = 0.5) -> PlatformGraph:
    return PlatformGraph(
        map_id="map-01",
        map_fingerprint_sha256=SHA,
        graph_version="platform-v1",
        platforms=tuple(platforms),
        ambiguity_margin=margin,
        max_vertical_distance=5.0,
        max_horizontal_distance=10.0,
    )


def test_segment_and_graph_are_immutable_and_canonical() -> None:
    upper = _segment("upper", (20, 20), (40, 18))
    lower = _segment("lower", (0, 10), (10, 10))
    graph = _graph(upper, lower)

    assert graph.platforms == (lower, upper)
    assert graph.platform_ids == ("lower", "upper")
    assert graph.map_fingerprint_sha256 == SHA
    assert graph.to_dict() == {
        "map_id": "map-01",
        "map_fingerprint_sha256": SHA,
        "graph_version": "platform-v1",
        "platforms": [
            {
                "platform_id": "lower",
                "start": {"x": 0.0, "y": 10.0},
                "end": {"x": 10.0, "y": 10.0},
            },
            {
                "platform_id": "upper",
                "start": {"x": 20.0, "y": 20.0},
                "end": {"x": 40.0, "y": 18.0},
            },
        ],
        "ambiguity_margin": 0.5,
        "max_vertical_distance": 5.0,
        "max_horizontal_distance": 10.0,
    }
    assert PlatformGraph.from_dict(graph.to_dict()) == graph
    assert graph.digest == PlatformGraph.from_dict(graph.to_dict()).digest
    assert len(graph.digest) == 64
    assert len(lower.digest) == 64

    with pytest.raises(FrozenInstanceError):
        graph.map_id = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        lower.platform_id = "other"  # type: ignore[misc]


def test_resolve_uses_linear_slope_projection_and_confirms_unique_candidate() -> None:
    slope = _segment("slope", (0, 10), (10, 20))
    match = _graph(slope).resolve(WorldCoordinate(5, 12))

    assert match.status is PlatformMatchStatus.CONFIRMED
    assert match.platform_id == "slope"
    assert match.vertical_error == pytest.approx(3.0)
    assert match.horizontal_error == pytest.approx(0.0)
    assert match.projected_point == WorldCoordinate(5, 15)
    assert match.candidates == ("slope",)


def test_resolve_sorts_vertical_then_horizontal_then_platform_id() -> None:
    # At x=12, the two same-height platforms are equally close vertically;
    # the platform whose endpoint is closer wins and is then confirmed.
    near = _segment("near", (0, 10), (10, 10))
    far = _segment("far", (20, 10), (30, 10))
    match = _graph(far, near, margin=0.1).resolve(WorldCoordinate(12, 10))

    assert match.status is PlatformMatchStatus.CONFIRMED
    assert match.platform_id == "near"
    assert match.horizontal_error == pytest.approx(2.0)
    assert match.candidates == ("near", "far")


def test_resolve_reports_shared_endpoint_and_near_tie_as_ambiguous() -> None:
    left = _segment("left", (0, 10), (10, 10))
    right = _segment("right", (10, 10), (20, 10))
    graph = _graph(left, right)

    shared = graph.resolve(WorldCoordinate(10, 10))
    assert shared.status is PlatformMatchStatus.AMBIGUOUS
    assert shared.platform_id is None
    assert shared.candidates == ("left", "right")

    lower = _segment("lower", (0, 10), (20, 10))
    upper = _segment("upper", (0, 10.3), (20, 10.3))
    near_tie = _graph(lower, upper, margin=0.5).resolve(WorldCoordinate(10, 10.1))
    assert near_tie.status is PlatformMatchStatus.AMBIGUOUS
    assert near_tie.platform_id is None


def test_resolve_returns_unknown_when_vertical_threshold_excludes_all() -> None:
    graph = _graph(_segment("p", (0, 10), (20, 10)))
    result = graph.resolve(WorldCoordinate(10, 20))

    assert result.status is PlatformMatchStatus.UNKNOWN
    assert result.platform_id is None
    assert result.candidates == ()
    assert result.vertical_error is None


def test_resolve_returns_unknown_when_horizontal_threshold_excludes_all() -> None:
    graph = _graph(_segment("p", (0, 10), (20, 10)))
    result = graph.resolve(WorldCoordinate(31, 10))

    assert result.status is PlatformMatchStatus.UNKNOWN
    assert result.platform_id is None
    assert result.candidates == ()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ((0, 0), (0, 0)),
        ((nan, 0), (1, 1)),
        ((0, inf), (1, 1)),
    ],
)
def test_segment_rejects_degenerate_or_nonfinite_geometry(
    start: tuple[float, float], end: tuple[float, float]
) -> None:
    with pytest.raises(ValueError):
        _segment("bad", start, end)


def test_graph_rejects_duplicate_ids_bad_hash_and_invalid_policy() -> None:
    first = _segment("same", (0, 0), (1, 0))
    second = _segment("same", (2, 0), (3, 0))

    with pytest.raises(ValueError, match="unique"):
        _graph(first, second)
    with pytest.raises(ValueError, match="SHA-256"):
        PlatformGraph(
            map_id="map",
            map_fingerprint_sha256="not-a-hash",
            graph_version="v1",
            platforms=(first,),
            ambiguity_margin=0.5,
            max_vertical_distance=5,
            max_horizontal_distance=10,
        )
    with pytest.raises(ValueError):
        _graph(first, margin=-1)
    with pytest.raises(ValueError):
        PlatformGraph(
            map_id="map",
            map_fingerprint_sha256=SHA,
            graph_version="v1",
            platforms=(first,),
            ambiguity_margin=0.5,
            max_vertical_distance=inf,
            max_horizontal_distance=10,
        )
    with pytest.raises(ValueError):
        PlatformGraph(
            map_id="map",
            map_fingerprint_sha256=SHA,
            graph_version="v1",
            platforms=(first,),
            ambiguity_margin=0.5,
            max_vertical_distance=5,
            max_horizontal_distance=inf,
        )
    with pytest.raises(TypeError, match="tuple"):
        PlatformGraph(
            map_id="map",
            map_fingerprint_sha256=SHA,
            graph_version="v1",
            platforms=[first],  # type: ignore[arg-type]
            ambiguity_margin=0.5,
            max_vertical_distance=5,
            max_horizontal_distance=10,
        )


def test_match_roundtrip_and_invalid_world_point_boundary() -> None:
    graph = _graph(_segment("p", (0, 0), (10, 0)))
    match = graph.resolve(WorldCoordinate(3, 1))
    restored = type(match).from_dict(match.to_dict())

    assert restored == match
    assert restored.digest == match.digest
    with pytest.raises(TypeError, match="WorldCoordinate"):
        graph.resolve((3, 1))  # type: ignore[arg-type]
