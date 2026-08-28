from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from hashlib import sha256
from types import MappingProxyType
from typing import Any

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | Sequence[JSONValue] | Mapping[str, JSONValue]


def ensure_non_empty_str(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")


def ensure_non_negative_int(value: int, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be an integer >= 0.")


def ensure_positive_int(value: int, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be an integer > 0.")


def ensure_time_ns(value: int, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer (nanoseconds).")


def ensure_probability(value: float, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or not (0.0 <= float(value) <= 1.0)
    ):
        raise ValueError(f"{field_name} must be a float between 0 and 1.")


def _is_json_value(value: Any, active: set[int] | None = None) -> bool:
    """Return whether *value* is a strict JSON value.

    The contract deliberately accepts only the Python values with a direct JSON
    representation.  In particular, arbitrary ``Sequence`` implementations,
    sets, bytes, enums and numeric proxy types are not silently converted.  A
    recursion guard keeps a self-referential container from escaping as a
    ``RecursionError``.
    """

    if type(value) is str or type(value) is int or type(value) is bool or value is None:
        return True
    if type(value) is float:
        return math.isfinite(value)

    if active is None:
        active = set()
    container_id = id(value)
    if container_id in active:
        return False

    if type(value) is list or type(value) is tuple:
        active.add(container_id)
        try:
            return all(_is_json_value(item, active) for item in value)
        finally:
            active.remove(container_id)

    if isinstance(value, Mapping):
        active.add(container_id)
        try:
            return all(
                type(key) is str and _is_json_value(item, active) for key, item in value.items()
            )
        finally:
            active.remove(container_id)
    return False


def ensure_json_value(value: Any, field_name: str) -> None:
    if not _is_json_value(value):
        raise ValueError(f"{field_name} must be JSON-serializable.")


def freeze_json_value(value: Any) -> Any:
    """Recursively convert a JSON value into immutable containers.

    Lists/tuples become tuples and mappings become read-only mapping proxies.
    Validation happens before conversion so callers consistently receive a
    ``ValueError`` for malformed payloads instead of a low-level ``TypeError``.
    """

    ensure_json_value(value, "value")
    return _freeze_json_value(value, set())


def _freeze_json_value(value: Any, active: set[int]) -> Any:
    if type(value) is str or type(value) is int or type(value) is float or type(value) is bool:
        return value
    if value is None:
        return None

    container_id = id(value)
    if container_id in active:
        # ``ensure_json_value`` already catches this; retain a defensive check
        # for callers of this private helper.
        raise ValueError("value must not contain cyclic containers.")

    if isinstance(value, list):
        active.add(container_id)
        try:
            return tuple(_freeze_json_value(item, active) for item in value)
        finally:
            active.remove(container_id)
    if isinstance(value, tuple):
        active.add(container_id)
        try:
            return tuple(_freeze_json_value(item, active) for item in value)
        finally:
            active.remove(container_id)
    if isinstance(value, Mapping):
        active.add(container_id)
        try:
            return MappingProxyType(
                {key: _freeze_json_value(item, active) for key, item in value.items()}
            )
        finally:
            active.remove(container_id)
    # This branch is unreachable after strict validation, but makes the helper
    # robust if it is reused internally in the future.
    raise ValueError(f"Unsupported JSON value type: {type(value)!r}")


def thaw_json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [thaw_json_value(v) for v in value]
    if isinstance(value, Mapping):
        return {k: thaw_json_value(v) for k, v in value.items()}
    return value


def to_json_dict(value: Any) -> Any:
    ensure_json_value(value, "value")
    return thaw_json_value(value)


def ensure_sha256_hex(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field_name} must be a 64-character SHA-256 hex string.")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be hexadecimal SHA-256 digest.") from exc


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    ensure_json_value(payload, "payload")
    return json.dumps(
        to_json_dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def hash_payload(payload: Any) -> str:
    canonical_payload = payload if isinstance(payload, Mapping) else {"payload": payload}
    return sha256(canonical_json_bytes(canonical_payload)).hexdigest()


def ensure_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    """Validate a mapping boundary used by the public contracts."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping.")
    if any(type(key) is not str for key in value):
        raise ValueError(f"{field_name} keys must be strings.")
    return value
