from __future__ import annotations

import json
from pathlib import Path
from threading import Barrier, Lock, Thread

import pytest

from maple_automation_core.replay.event_tape import EventTape


def test_concurrent_instances_share_normalized_path_lock(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "events.jsonl"
    aliases = (path, path.parent / "." / path.name, path.resolve())
    tapes = [EventTape(alias) for alias in aliases]
    barrier = Barrier(len(tapes))
    result_lock = Lock()
    records = []
    errors: list[BaseException] = []

    def append_from_instance(index: int, tape: EventTape) -> None:
        try:
            barrier.wait(timeout=5)
            record = tape.append(
                event_type="frame",
                payload={"worker": index},
                session_id="shared",
                frame_id=0,
                world_state_version=0,
                recorded_at_ns=0,
            )
            with result_lock:
                records.append(record)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    threads = [
        Thread(target=append_from_instance, args=(index, tape)) for index, tape in enumerate(tapes)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(records) == len(tapes)

    persisted = EventTape(path).read_all()
    assert [record.sequence for record in persisted] == list(range(len(tapes)))
    assert {record.payload["worker"] for record in persisted} == set(range(len(tapes)))
    assert len({record.record_hash for record in persisted}) == len(tapes)
    assert all(
        record.previous_record_hash == (persisted[index - 1].record_hash if index else "0" * 64)
        for index, record in enumerate(persisted)
    )


def test_persisted_extra_top_level_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    tape = EventTape(path)
    forged = tape.append("frame", {}, "session", 0, 0, 0).to_dict()
    forged["forged"] = True
    path.write_text(json.dumps(forged) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unexpected key"):
        EventTape(path)
