from __future__ import annotations

from types import MappingProxyType

import pytest

from maple_automation_core.domain._contract_utils import (
    _freeze_json_value,
    canonical_json_bytes,
    ensure_json_value,
    ensure_mapping,
    ensure_non_empty_str,
    ensure_non_negative_int,
    ensure_positive_int,
    ensure_probability,
    ensure_sha256_hex,
    ensure_time_ns,
    freeze_json_value,
    hash_payload,
    thaw_json_value,
    to_json_dict,
)
from maple_automation_core.domain.actions import ActionReference
from maple_automation_core.domain.coordinates import PixelCoordinate, Velocity, WorldCoordinate


def test_contract_primitive_validators_reject_invalid_values() -> None:
    invalid_cases = (
        (ensure_non_empty_str, (" ", "name")),
        (ensure_non_negative_int, (-1, "count")),
        (ensure_non_negative_int, (True, "count")),
        (ensure_positive_int, (0, "size")),
        (ensure_positive_int, (False, "size")),
        (ensure_time_ns, (-1, "time")),
        (ensure_time_ns, (1.5, "time")),
        (ensure_probability, (True, "confidence")),
        (ensure_probability, (float("nan"), "confidence")),
        (ensure_probability, (1.1, "confidence")),
        (ensure_mapping, ([], "mapping")),
        (ensure_mapping, ({1: "bad"}, "mapping")),
        (ensure_sha256_hex, ("x", "digest")),
        (ensure_sha256_hex, ("z" * 64, "digest")),
    )
    for validator, args in invalid_cases:
        with pytest.raises(ValueError):
            validator(*args)


def test_json_helpers_are_strict_recursive_and_canonical() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    invalid_values = (
        {"set": {1, 2}},
        {"bytes": b"x"},
        {1: "non-string key"},
        cyclic,
        float("inf"),
    )
    for value in invalid_values:
        with pytest.raises(ValueError):
            ensure_json_value(value, "payload")
        with pytest.raises(ValueError):
            freeze_json_value(value)

    frozen = freeze_json_value({"items": [1, {"ok": True}], "tuple": (None,)})
    assert isinstance(frozen, MappingProxyType)
    assert to_json_dict(frozen) == {"items": [1, {"ok": True}], "tuple": [None]}
    assert thaw_json_value(frozen) == {"items": [1, {"ok": True}], "tuple": [None]}
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert hash_payload({"a": 1}) == hash_payload({"a": 1})
    assert hash_payload([1, 2]) == hash_payload([1, 2])

    with pytest.raises(ValueError):
        _freeze_json_value(object(), set())
    recursive: list[object] = []
    recursive.append(recursive)
    with pytest.raises(ValueError):
        _freeze_json_value(recursive, set())


def test_coordinate_roundtrip_rejects_missing_and_non_finite_values() -> None:
    assert PixelCoordinate.from_dict({"x": 1, "y": 2}) == PixelCoordinate(1, 2)
    assert WorldCoordinate.from_dict({"x": 1, "y": -2}) == WorldCoordinate(1, -2)
    assert Velocity.from_dict({"dx": 1, "dy": -2}) == Velocity(1, -2)
    for constructor in (
        PixelCoordinate.from_dict,
        WorldCoordinate.from_dict,
        Velocity.from_dict,
    ):
        with pytest.raises(ValueError):
            constructor({})

    with pytest.raises(ValueError):
        WorldCoordinate("1", 2)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WorldCoordinate(float("inf"), 0)
    with pytest.raises(ValueError):
        Velocity(True, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Velocity(float("nan"), 0)


def test_action_reference_serializes_and_validates() -> None:
    reference = ActionReference("s", 2, 3)
    assert reference.version == 3
    assert reference.to_dict() == {
        "session_id": "s",
        "frame_id": 2,
        "world_state_version": 3,
    }
    with pytest.raises(ValueError):
        ActionReference("", 0, 0)
    with pytest.raises(ValueError):
        ActionReference("s", -1, 0)
