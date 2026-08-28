from __future__ import annotations

import hmac
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any

from maple_automation_core.domain._contract_utils import (
    canonical_json_bytes,
    ensure_json_value,
    ensure_mapping,
    ensure_non_empty_str,
    ensure_non_negative_int,
    ensure_sha256_hex,
    ensure_time_ns,
    freeze_json_value,
    to_json_dict,
)

REPLAY_SCHEMA_VERSION = "1.0.0"
GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One immutable, hash-chained event in a replay tape."""

    schema_version: str
    session_id: str
    frame_id: int
    world_state_version: int
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    recorded_at_ns: int
    previous_record_hash: str
    record_hash: str

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.schema_version, "schema_version")
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_negative_int(self.frame_id, "frame_id")
        ensure_non_negative_int(self.world_state_version, "world_state_version")
        ensure_non_negative_int(self.sequence, "sequence")
        ensure_non_empty_str(self.event_type, "event_type")
        payload = ensure_mapping(self.payload, "payload")
        ensure_json_value(payload, "payload")
        ensure_time_ns(self.recorded_at_ns, "recorded_at_ns")
        ensure_sha256_hex(self.previous_record_hash, "previous_record_hash")
        ensure_sha256_hex(self.record_hash, "record_hash")
        object.__setattr__(self, "payload", freeze_json_value(payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "world_state_version": self.world_state_version,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "payload": to_json_dict(self.payload),
            "recorded_at_ns": self.recorded_at_ns,
            "previous_record_hash": self.previous_record_hash,
            "record_hash": self.record_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EventRecord:
        values = ensure_mapping(data, "EventRecord payload")
        try:
            return cls(
                schema_version=values["schema_version"],
                session_id=values["session_id"],
                frame_id=values["frame_id"],
                world_state_version=values["world_state_version"],
                sequence=values["sequence"],
                event_type=values["event_type"],
                payload=ensure_mapping(values["payload"], "payload"),
                recorded_at_ns=values["recorded_at_ns"],
                previous_record_hash=values["previous_record_hash"],
                record_hash=values["record_hash"],
            )
        except KeyError as exc:
            raise ValueError(f"EventRecord payload missing key: {exc.args[0]}") from exc


def _digest(payload: Mapping[str, Any]) -> str:
    return sha256(canonical_json_bytes(payload)).hexdigest()


def _compute_record_hash(
    schema_version: str,
    session_id: str,
    frame_id: int,
    world_state_version: int,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any],
    recorded_at_ns: int,
    previous_record_hash: str,
) -> str:
    body = {
        "schema_version": schema_version,
        "session_id": session_id,
        "frame_id": frame_id,
        "world_state_version": world_state_version,
        "sequence": sequence,
        "event_type": event_type,
        "payload": to_json_dict(payload),
        "recorded_at_ns": recorded_at_ns,
        "previous_record_hash": previous_record_hash,
    }
    return _digest(body)


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Non-standard JSON constant {value!r} is not permitted.")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


class EventTape:
    """Append-only deterministic JSONL recorder with tamper-evident chaining.

    The physical line order is authoritative. Records are never sorted while
    loading or reading: sequence, session, monotonic frame/version/timestamp,
    and the previous-hash link are checked in exactly the order stored on disk.
    """

    def __init__(
        self,
        path: str | Path,
        schema_version: str = REPLAY_SCHEMA_VERSION,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_non_empty_str(schema_version, "schema_version")
        self.schema_version = schema_version
        self._lock = Lock()
        self._next_sequence = 0
        self._next_previous_hash = GENESIS_HASH
        self._session_id: str | None = None
        self._last_frame_id = -1
        self._last_world_state_version = -1
        self._last_recorded_at_ns = -1
        self._load_existing()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def _load_existing(self) -> None:
        records = self._read_validated()
        if not records:
            self._next_sequence = 0
            self._next_previous_hash = GENESIS_HASH
            self._session_id = None
            self._last_frame_id = -1
            self._last_world_state_version = -1
            self._last_recorded_at_ns = -1
            return

        last = records[-1]
        self._next_sequence = last.sequence + 1
        self._next_previous_hash = last.record_hash
        self._session_id = last.session_id
        self._last_frame_id = last.frame_id
        self._last_world_state_version = last.world_state_version
        self._last_recorded_at_ns = last.recorded_at_ns

    def _parse_line(self, line: str, physical_line: int | None = None) -> dict[str, Any]:
        try:
            data = json.loads(
                line,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            suffix = "" if physical_line is None else f" at physical line {physical_line}"
            raise ValueError(f"Invalid JSON in tape{suffix}.") from exc
        values = ensure_mapping(data, "Tape line")
        required = (
            "schema_version",
            "session_id",
            "frame_id",
            "world_state_version",
            "sequence",
            "event_type",
            "payload",
            "recorded_at_ns",
            "previous_record_hash",
            "record_hash",
        )
        for key in required:
            if key not in values:
                raise ValueError(f"Missing required key in tape record: {key}")
        return dict(values)

    def _read_validated(self) -> tuple[EventRecord, ...]:
        if not self.path.exists():
            return ()
        if not self.path.is_file():
            raise ValueError(f"Event tape path is not a file: {self.path}")

        records: list[EventRecord] = []
        expected_sequence = 0
        expected_previous_hash = GENESIS_HASH
        expected_session: str | None = None
        last_frame_id = -1
        last_world_state_version = -1
        last_recorded_at_ns = -1

        for physical_line, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            # Empty lines do not represent records and are harmless when a
            # recorder is interrupted between writes.
            if not line.strip():
                continue
            record = EventRecord.from_dict(self._parse_line(line, physical_line))
            if record.schema_version != self.schema_version:
                raise ValueError("schema_version mismatch in persisted tape.")
            if record.sequence != expected_sequence:
                raise ValueError(
                    "Non-sequential sequence in persisted tape: "
                    f"expected {expected_sequence}, got {record.sequence}."
                )
            if expected_session is None:
                expected_session = record.session_id
            elif record.session_id != expected_session:
                raise ValueError("A tape is bound to exactly one session_id.")
            if record.previous_record_hash != expected_previous_hash:
                raise ValueError(f"Broken hash chain at sequence {record.sequence}.")

            expected_hash = _compute_record_hash(
                schema_version=record.schema_version,
                session_id=record.session_id,
                frame_id=record.frame_id,
                world_state_version=record.world_state_version,
                sequence=record.sequence,
                event_type=record.event_type,
                payload=record.payload,
                recorded_at_ns=record.recorded_at_ns,
                previous_record_hash=record.previous_record_hash,
            )
            if not hmac.compare_digest(record.record_hash, expected_hash):
                raise ValueError(f"Invalid hash at sequence {record.sequence}.")

            if record.frame_id < last_frame_id:
                raise ValueError("frame_id moved backwards in persisted tape.")
            if record.world_state_version < last_world_state_version:
                raise ValueError("world_state_version moved backwards in persisted tape.")
            if record.recorded_at_ns < last_recorded_at_ns:
                raise ValueError("recorded_at_ns moved backwards in persisted tape.")

            records.append(record)
            expected_sequence += 1
            expected_previous_hash = record.record_hash
            last_frame_id = record.frame_id
            last_world_state_version = record.world_state_version
            last_recorded_at_ns = record.recorded_at_ns

        return tuple(records)

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        session_id: str,
        frame_id: int,
        world_state_version: int,
        recorded_at_ns: int,
    ) -> EventRecord:
        ensure_non_empty_str(event_type, "event_type")
        ensure_non_empty_str(session_id, "session_id")
        ensure_non_negative_int(frame_id, "frame_id")
        ensure_non_negative_int(world_state_version, "world_state_version")
        ensure_time_ns(recorded_at_ns, "recorded_at_ns")
        payload_mapping = ensure_mapping(payload, "payload")
        ensure_json_value(payload_mapping, "payload")
        frozen_payload = freeze_json_value(payload_mapping)

        with self._lock:
            # Refresh the tail under the append lock so a second EventTape
            # instance cannot reuse a sequence/hash after writing to the same
            # path.
            self._load_existing()
            if self._session_id is not None and session_id != self._session_id:
                raise ValueError("A tape is bound to exactly one session_id.")
            if frame_id < self._last_frame_id:
                raise ValueError("frame_id must not move backwards.")
            if world_state_version < self._last_world_state_version:
                raise ValueError("world_state_version must not move backwards.")
            if recorded_at_ns < self._last_recorded_at_ns:
                raise ValueError("recorded_at_ns must not move backwards.")

            sequence = self._next_sequence
            previous_hash = self._next_previous_hash
            record = EventRecord(
                schema_version=self.schema_version,
                session_id=session_id,
                frame_id=frame_id,
                world_state_version=world_state_version,
                sequence=sequence,
                event_type=event_type,
                payload=frozen_payload,
                recorded_at_ns=recorded_at_ns,
                previous_record_hash=previous_hash,
                record_hash=_compute_record_hash(
                    schema_version=self.schema_version,
                    session_id=session_id,
                    frame_id=frame_id,
                    world_state_version=world_state_version,
                    sequence=sequence,
                    event_type=event_type,
                    payload=frozen_payload,
                    recorded_at_ns=recorded_at_ns,
                    previous_record_hash=previous_hash,
                ),
            )
            with self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(canonical_json_bytes(record.to_dict()).decode("utf-8") + "\n")
                file.flush()
                os.fsync(file.fileno())

            self._next_sequence = sequence + 1
            self._next_previous_hash = record.record_hash
            self._session_id = session_id
            self._last_frame_id = frame_id
            self._last_world_state_version = world_state_version
            self._last_recorded_at_ns = recorded_at_ns
            return record

    def iter_records(self) -> Iterable[EventRecord]:
        return self.read_all()

    def read_all(self) -> tuple[EventRecord, ...]:
        with self._lock:
            return self._read_validated()
