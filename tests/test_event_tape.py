from __future__ import annotations

import json
from pathlib import Path

import pytest

from maple_automation_core.replay.event_tape import GENESIS_HASH, REPLAY_SCHEMA_VERSION, EventTape


def test_event_tape_append_and_deterministic_read(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    tape = EventTape(path)

    first = tape.append(
        event_type="frame",
        payload={"frame_id": 1},
        session_id="s1",
        frame_id=1,
        world_state_version=1,
        recorded_at_ns=10,
    )
    second = tape.append(
        event_type="frame",
        payload={"frame_id": 2},
        session_id="s1",
        frame_id=2,
        world_state_version=2,
        recorded_at_ns=20,
    )

    assert first.sequence == 0
    assert second.sequence == 1
    assert second.previous_record_hash == first.record_hash

    events = tape.read_all()
    assert [e.sequence for e in events] == [0, 1]
    assert events == (first, second)


def test_event_tape_rejects_physical_out_of_order_or_missing_sequence(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    record = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "session_id": "s",
        "frame_id": 2,
        "world_state_version": 2,
        "sequence": 2,
        "event_type": "c",
        "payload": {},
        "recorded_at_ns": 0,
        "previous_record_hash": GENESIS_HASH,
        "record_hash": "a" * 64,
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Non-sequential sequence"):
        EventTape(path)


def test_event_tape_invalid_hash_rejected(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    tape = EventTape(path)
    first = tape.append(
        event_type="frame",
        payload={"frame_id": 1},
        session_id="s",
        frame_id=1,
        world_state_version=1,
        recorded_at_ns=10,
    )
    invalid_record = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "session_id": "s",
        "frame_id": 1,
        "world_state_version": 1,
        "sequence": 1,
        "event_type": "frame",
        "payload": {"frame_id": 2},
        "recorded_at_ns": 15,
        "previous_record_hash": first.record_hash,
        "record_hash": "a" * 64,
    }
    with tmp_path.joinpath("events.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(invalid_record) + "\n")
    with pytest.raises(ValueError, match="Invalid hash"):
        EventTape(path)


def test_event_tape_illegal_payload_rejected(tmp_path: Path) -> None:
    tape = EventTape(tmp_path / "events.jsonl")
    with pytest.raises(ValueError):
        tape.append(
            event_type="bad",
            payload={"x": {1, 2, 3}},
            session_id="s",
            frame_id=1,
            world_state_version=1,
            recorded_at_ns=5,
        )


def test_event_tape_rejects_tamper(tmp_path: Path) -> None:
    tape = EventTape(tmp_path / "events.jsonl")
    tape.append(
        event_type="frame",
        payload={"frame_id": 1},
        session_id="s",
        frame_id=1,
        world_state_version=1,
        recorded_at_ns=1,
    )
    tampered_record = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "session_id": "s",
        "frame_id": 1,
        "world_state_version": 1,
        "sequence": 0,
        "event_type": "frame",
        "payload": {"frame_id": 2},
        "recorded_at_ns": 1,
        "previous_record_hash": GENESIS_HASH,
        "record_hash": "b" * 64,
    }
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(tampered_record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid hash"):
        EventTape(path)


def test_event_tape_rejects_cross_session_and_time_reversal(tmp_path: Path) -> None:
    tape = EventTape(tmp_path / "events.jsonl")
    tape.append("frame", {}, "s1", 1, 1, 10)
    with pytest.raises(ValueError, match="one session"):
        tape.append("frame", {}, "s2", 2, 2, 20)
    with pytest.raises(ValueError, match="recorded_at_ns"):
        tape.append("frame", {}, "s1", 2, 2, 9)


def test_event_payload_is_deeply_immutable(tmp_path: Path) -> None:
    source = {"nested": {"values": [1, 2]}}
    record = EventTape(tmp_path / "events.jsonl").append("frame", source, "s", 1, 1, 1)
    source["nested"]["values"].append(3)
    assert record.to_dict()["payload"] == {"nested": {"values": [1, 2]}}


def test_event_tape_rejects_non_standard_json_and_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    tape = EventTape(path)
    with pytest.raises(ValueError):
        tape.append(
            event_type="bad",
            payload={"value": float("inf")},
            session_id="s",
            frame_id=1,
            world_state_version=1,
            recorded_at_ns=1,
        )

    path.write_text(
        '{"schema_version":"1.0.0","schema_version":"1.0.0"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid JSON"):
        EventTape(path)


def test_event_tape_second_instance_continues_physical_chain(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    first_tape = EventTape(path)
    first = first_tape.append("frame", {}, "s", 1, 1, 1)
    second_tape = EventTape(path)
    second = second_tape.append("frame", {}, "s", 2, 2, 2)
    assert second.sequence == 1
    assert second.previous_record_hash == first.record_hash
    assert second_tape.read_all() == (first, second)
