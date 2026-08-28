from __future__ import annotations

import json
from pathlib import Path

import pytest

from maple_automation_core.replay.event_tape import (
    GENESIS_HASH,
    REPLAY_SCHEMA_VERSION,
    EventRecord,
    EventTape,
    _compute_record_hash,
)


def _record_data(
    *, sequence: int = 0, session_id: str = "s", frame_id: int = 1
) -> dict[str, object]:
    previous = GENESIS_HASH
    payload: dict[str, object] = {"frame": frame_id}
    record_hash = _compute_record_hash(
        schema_version=REPLAY_SCHEMA_VERSION,
        session_id=session_id,
        frame_id=frame_id,
        world_state_version=1,
        sequence=sequence,
        event_type="frame",
        payload=payload,
        recorded_at_ns=1,
        previous_record_hash=previous,
    )
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "session_id": session_id,
        "frame_id": frame_id,
        "world_state_version": 1,
        "sequence": sequence,
        "event_type": "frame",
        "payload": payload,
        "recorded_at_ns": 1,
        "previous_record_hash": previous,
        "record_hash": record_hash,
    }


def test_event_record_roundtrip_and_constructor_validation() -> None:
    data = _record_data()
    record = EventRecord.from_dict(data)
    assert EventRecord.from_dict(record.to_dict()) == record
    for field, value in (
        ("session_id", ""),
        ("frame_id", -1),
        ("world_state_version", -1),
        ("sequence", -1),
        ("event_type", ""),
        ("payload", []),
        ("recorded_at_ns", -1),
        ("record_hash", "x"),
    ):
        invalid = {**data, field: value}
        with pytest.raises((ValueError, TypeError)):
            EventRecord.from_dict(invalid)


def test_event_tape_rejects_malformed_physical_lines(tmp_path: Path) -> None:
    malformed = (
        "not json\n",
        "[]\n",
        '{"schema_version":"1.0.0"}\n',
        '{"schema_version":"9.9.9","session_id":"s","frame_id":0,'
        '"world_state_version":0,"sequence":0,"event_type":"x","payload":{},'
        '"recorded_at_ns":0,"previous_record_hash":"'
        + GENESIS_HASH
        + '","record_hash":"'
        + "0" * 64
        + '"}\n',
    )
    for index, content in enumerate(malformed):
        path = tmp_path / f"bad-{index}.jsonl"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            EventTape(path)

    path = tmp_path / "empty.jsonl"
    path.write_text("\n  \n", encoding="utf-8")
    tape = EventTape(path)
    assert tape.read_all() == ()
    assert tape.session_id is None
    assert tape.next_sequence == 0


def test_event_tape_rejects_persisted_schema_session_chain_and_order_errors(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    first = _record_data()
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")
    tape = EventTape(path)
    assert tape.session_id == "s"
    assert tape.next_sequence == 1

    # A valid first line followed by a different session is rejected before
    # any sorting or recovery can hide the physical ordering error.
    second = _record_data(sequence=1, session_id="other", frame_id=2)
    second["previous_record_hash"] = first["record_hash"]
    second["record_hash"] = _compute_record_hash(
        schema_version=REPLAY_SCHEMA_VERSION,
        session_id="other",
        frame_id=2,
        world_state_version=1,
        sequence=1,
        event_type="frame",
        payload={"frame": 2},
        recorded_at_ns=1,
        previous_record_hash=first["record_hash"],  # type: ignore[arg-type]
    )
    path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="one session"):
        EventTape(path)

    path.write_text(json.dumps(_record_data(sequence=1)) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Non-sequential"):
        EventTape(path)


def test_event_tape_rejects_persisted_monotonicity_and_directory_path(tmp_path: Path) -> None:
    tape = EventTape(tmp_path / "source.jsonl")
    first = tape.append("frame", {}, "s", 2, 2, 2)
    second = tape.append("frame", {}, "s", 3, 3, 3)
    records = [first.to_dict(), second.to_dict()]
    for key, value in (("frame_id", 1), ("world_state_version", 1), ("recorded_at_ns", 1)):
        changed = dict(records[1])
        changed[key] = value
        changed["previous_record_hash"] = records[0]["record_hash"]
        changed["record_hash"] = _compute_record_hash(
            schema_version=changed["schema_version"],  # type: ignore[arg-type]
            session_id=changed["session_id"],  # type: ignore[arg-type]
            frame_id=changed["frame_id"],  # type: ignore[arg-type]
            world_state_version=changed["world_state_version"],  # type: ignore[arg-type]
            sequence=changed["sequence"],  # type: ignore[arg-type]
            event_type=changed["event_type"],  # type: ignore[arg-type]
            payload=changed["payload"],  # type: ignore[arg-type]
            recorded_at_ns=changed["recorded_at_ns"],  # type: ignore[arg-type]
            previous_record_hash=changed["previous_record_hash"],  # type: ignore[arg-type]
        )
        path = tmp_path / f"changed-{key}.jsonl"
        path.write_text(
            json.dumps(records[0]) + "\n" + json.dumps(changed) + "\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match=key):
            EventTape(path)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ValueError, match="not a file"):
        EventTape(directory)


def test_event_tape_constructor_and_append_argument_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        EventTape(tmp_path / "bad-schema.jsonl", schema_version=" ")
    tape = EventTape(tmp_path / "args.jsonl")
    invalid_calls = (
        ("", {}, "s", 0, 0, 0),
        ("event", {}, "", 0, 0, 0),
        ("event", {}, "s", -1, 0, 0),
        ("event", {}, "s", 0, -1, 0),
        ("event", {}, "s", 0, 0, -1),
        ("event", [], "s", 0, 0, 0),
    )
    for args in invalid_calls:
        with pytest.raises(ValueError):
            tape.append(*args)  # type: ignore[arg-type]
