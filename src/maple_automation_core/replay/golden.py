from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maple_automation_core.domain._contract_utils import (
    canonical_json_bytes,
    ensure_json_value,
    ensure_mapping,
    ensure_non_empty_str,
    ensure_non_negative_int,
    ensure_positive_int,
    ensure_time_ns,
    freeze_json_value,
    to_json_dict,
)
from maple_automation_core.domain.actions import (
    ActionHandle,
    ActionResult,
    ActionSpec,
    ActionTermination,
)
from maple_automation_core.domain.frame import FramePacket
from maple_automation_core.domain.player_world import WorldState
from maple_automation_core.replay.event_tape import EventRecord, EventTape

GOLDEN_FIXTURE_VERSION = "1.0.0"
REPLAY_REPORT_VERSION = "1.0.0"


class ReplayError(ValueError):
    """Raised when a golden input violates replay invariants."""


class ReplayDeterminismError(ReplayError):
    """Raised when repeated replay runs produce different output digests."""


@dataclass(frozen=True, slots=True)
class GoldenAction:
    """Action input and deterministic post-state reference from the fixture."""

    spec: ActionSpec
    termination: ActionTermination
    result_frame_id: int
    result_world_state_version: int
    completed_at_ns: int
    details: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.spec, ActionSpec):
            raise TypeError("spec must be ActionSpec.")
        if not isinstance(self.termination, ActionTermination):
            raise TypeError("termination must be ActionTermination.")
        ensure_non_negative_int(self.result_frame_id, "result_frame_id")
        ensure_non_negative_int(
            self.result_world_state_version,
            "result_world_state_version",
        )
        ensure_time_ns(self.completed_at_ns, "completed_at_ns")
        details = ensure_mapping(self.details, "details")
        evidence = ensure_mapping(self.evidence, "evidence")
        ensure_json_value(details, "details")
        ensure_json_value(evidence, "evidence")
        object.__setattr__(self, "details", freeze_json_value(details))
        object.__setattr__(self, "evidence", freeze_json_value(evidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "termination": self.termination.value,
            "result_frame_id": self.result_frame_id,
            "result_world_state_version": self.result_world_state_version,
            "completed_at_ns": self.completed_at_ns,
            "details": to_json_dict(self.details),
            "evidence": to_json_dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GoldenAction:
        values = ensure_mapping(data, "golden action")
        try:
            details = ensure_mapping(values.get("details", {}), "details")
            evidence = ensure_mapping(values.get("evidence", {}), "evidence")
            return cls(
                spec=ActionSpec.from_dict(values["spec"]),
                termination=ActionTermination(values["termination"]),
                result_frame_id=values["result_frame_id"],
                result_world_state_version=values["result_world_state_version"],
                completed_at_ns=values["completed_at_ns"],
                details=details,
                evidence=evidence,
            )
        except KeyError as exc:
            raise ReplayError(f"golden action missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class GoldenFrame:
    """A frame, its immutable world snapshot, and an optional planned action."""

    packet: FramePacket
    world_state: WorldState
    action: GoldenAction | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.packet, FramePacket):
            raise TypeError("packet must be FramePacket.")
        if not isinstance(self.world_state, WorldState):
            raise TypeError("world_state must be WorldState.")
        if self.packet.session_id != self.world_state.session_id:
            raise ReplayError("frame and world state session_id must match.")
        if self.packet.frame_id != self.world_state.frame_id:
            raise ReplayError("frame and world state frame_id must match.")
        if self.action is not None and not isinstance(self.action, GoldenAction):
            raise TypeError("action must be GoldenAction or None.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet": self.packet.to_dict(),
            "world_state": self.world_state.to_dict(),
            "action": None if self.action is None else self.action.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GoldenFrame:
        values = ensure_mapping(data, "golden frame")
        try:
            raw_action = values.get("action")
            return cls(
                packet=FramePacket.from_dict(values["packet"]),
                world_state=WorldState.from_dict(values["world_state"]),
                action=None if raw_action is None else GoldenAction.from_dict(raw_action),
            )
        except KeyError as exc:
            raise ReplayError(f"golden frame missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class GoldenFixture:
    """Validated, de-identified deterministic input corpus."""

    fixture_id: str
    session_id: str
    bundle: Mapping[str, Any]
    frames: tuple[GoldenFrame, ...]
    legacy_observed_actions: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    source: str = "synthetic-deidentified"
    version: str = GOLDEN_FIXTURE_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.fixture_id, "fixture_id")
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.source, "source")
        ensure_non_empty_str(self.version, "version")
        bundle = ensure_mapping(self.bundle, "bundle")
        ensure_json_value(bundle, "bundle")
        object.__setattr__(self, "bundle", freeze_json_value(bundle))
        if not self.bundle_id:
            raise ReplayError("bundle must contain a non-empty bundle_id or release_id.")
        metadata = ensure_mapping(self.metadata, "metadata")
        ensure_json_value(metadata, "metadata")
        object.__setattr__(self, "metadata", freeze_json_value(metadata))
        if not isinstance(self.frames, tuple) or not self.frames:
            raise ReplayError("golden fixture must contain at least one frame.")
        if any(not isinstance(frame, GoldenFrame) for frame in self.frames):
            raise TypeError("frames must be a tuple of GoldenFrame.")
        if not isinstance(self.legacy_observed_actions, tuple):
            raise TypeError("legacy_observed_actions must be a tuple.")
        normalized_legacy = tuple(
            freeze_json_value(ensure_mapping(item, "legacy_observed_action"))
            for item in self.legacy_observed_actions
        )
        object.__setattr__(self, "legacy_observed_actions", normalized_legacy)
        previous_frame = -1
        previous_version = -1
        previous_captured = -1
        previous_received = -1
        previous_observed = -1
        frame_versions: dict[int, int] = {}
        frame_received: dict[int, int] = {}
        action_ids: set[str] = set()
        first_geometry = self.frames[0].packet.source_geometry
        first_source_id = self.frames[0].packet.source_id
        first_clock_domain = self.frames[0].packet.clock_domain
        first_transform_version = self.frames[0].packet.transform_version
        for item in self.frames:
            packet = item.packet
            world = item.world_state
            if packet.session_id != self.session_id or world.session_id != self.session_id:
                raise ReplayError("fixture frame session_id does not match fixture session_id.")
            if packet.source_id != first_source_id:
                raise ReplayError("golden fixture source_id values must be consistent.")
            if packet.clock_domain != first_clock_domain:
                raise ReplayError("golden fixture clock_domain values must be consistent.")
            if packet.transform_version != first_transform_version:
                raise ReplayError("golden fixture transform_version values must be consistent.")
            if packet.source_geometry != first_geometry:
                raise ReplayError("golden fixture source_geometry values must be consistent.")
            if packet.frame_id <= previous_frame:
                raise ReplayError("golden frame_id values must be strictly increasing.")
            if world.world_state_version <= previous_version:
                raise ReplayError("golden world_state_version values must be strictly increasing.")
            if packet.captured_at_ns < previous_captured:
                raise ReplayError("golden captured_at_ns values must not move backwards.")
            if packet.received_at_ns < previous_received:
                raise ReplayError("golden received_at_ns values must not move backwards.")
            if world.observed_at_ns < packet.received_at_ns:
                raise ReplayError("world observed_at_ns must be >= frame received_at_ns.")
            if world.observed_at_ns < previous_observed:
                raise ReplayError("golden observed_at_ns values must not move backwards.")
            frame_versions[packet.frame_id] = world.world_state_version
            frame_received[packet.frame_id] = packet.received_at_ns
            previous_frame = packet.frame_id
            previous_version = world.world_state_version
            previous_captured = packet.captured_at_ns
            previous_received = packet.received_at_ns
            previous_observed = world.observed_at_ns

            action = item.action
            if action is None:
                continue
            spec = action.spec
            if spec.session_id != self.session_id:
                raise ReplayError("action session_id must match fixture session_id.")
            if spec.origin_frame_id != packet.frame_id:
                raise ReplayError("action origin_frame_id must match its frame.")
            if spec.origin_world_state_version != world.world_state_version:
                raise ReplayError("action origin version must match its world state.")
            if spec.action_id in action_ids:
                raise ReplayError("action_id values must be unique in a fixture.")
            action_ids.add(spec.action_id)
            if spec.requested_at_ns < packet.received_at_ns:
                raise ReplayError("action requested_at_ns predates received frame.")
            if action.result_frame_id < spec.origin_frame_id:
                raise ReplayError("action result_frame_id must not predate origin frame.")
            if action.result_world_state_version <= spec.origin_world_state_version:
                raise ReplayError("action result version must be newer than origin version.")
            if action.completed_at_ns < spec.requested_at_ns:
                raise ReplayError("action completed_at_ns predates requested_at_ns.")

        for legacy_item in self.legacy_observed_actions:
            values = ensure_mapping(legacy_item, "legacy_observed_action")
            ensure_json_value(values, "legacy_observed_action")
            try:
                if values["session_id"] != self.session_id:
                    raise ReplayError("legacy action session_id must match fixture session_id.")
                ensure_non_empty_str(values["action_id"], "action_id")
                ensure_non_negative_int(values["frame_id"], "frame_id")
                ensure_non_negative_int(
                    values["world_state_version"],
                    "world_state_version",
                )
                ensure_time_ns(values["observed_at_ns"], "observed_at_ns")
            except KeyError as exc:
                raise ReplayError(f"legacy_observed_action missing key: {exc.args[0]}") from exc

        legacy_ids: set[str] = set()
        legacy_previous_time = -1
        for legacy_item in self.legacy_observed_actions:
            values = ensure_mapping(legacy_item, "legacy_observed_action")
            action_id = values["action_id"]
            ensure_non_empty_str(action_id, "action_id")
            if action_id in legacy_ids:
                raise ReplayError("legacy action_id values must be unique in a fixture.")
            legacy_ids.add(action_id)
            frame_id = values["frame_id"]
            if frame_id not in frame_versions:
                raise ReplayError("legacy action references an unknown frame.")
            if frame_versions[frame_id] != values["world_state_version"]:
                raise ReplayError("legacy action frame/version pair is inconsistent.")
            observed_at_ns = values["observed_at_ns"]
            if observed_at_ns < frame_received[frame_id]:
                raise ReplayError("legacy action observed_at_ns predates its frame.")
            if observed_at_ns < legacy_previous_time:
                raise ReplayError("legacy action observed_at_ns values must not move backwards.")
            legacy_previous_time = observed_at_ns

        for item in self.frames:
            action = item.action
            if action is None:
                continue
            if action.result_frame_id not in frame_versions:
                raise ReplayError("action result_frame_id is not present in fixture.")
            if frame_versions[action.result_frame_id] != action.result_world_state_version:
                raise ReplayError("action result frame/version pair is inconsistent.")
            if action.completed_at_ns < frame_received[action.result_frame_id]:
                raise ReplayError("action completed_at_ns predates result frame.")

    @property
    def bundle_id(self) -> str:
        value = self.bundle.get("bundle_id")
        if not isinstance(value, str) or not value:
            value = self.bundle.get("release_id")
        if not isinstance(value, str) or not value:
            raise ReplayError("bundle must contain a non-empty bundle_id or release_id.")
        ensure_non_empty_str(value, "bundle_id")
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "session_id": self.session_id,
            "bundle": to_json_dict(self.bundle),
            "frames": [frame.to_dict() for frame in self.frames],
            "legacy_observed_actions": [
                to_json_dict(item) for item in self.legacy_observed_actions
            ],
            "source": self.source,
            "version": self.version,
            "metadata": to_json_dict(self.metadata),
        }

    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()

    @property
    def bundle_digest(self) -> str:
        return _digest(self.bundle)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GoldenFixture:
        values = ensure_mapping(data, "golden fixture")
        try:
            raw_frames = values["frames"]
            if not isinstance(raw_frames, list | tuple):
                raise ReplayError("golden fixture frames must be a list.")
            raw_legacy = values.get("legacy_observed_actions", [])
            if not isinstance(raw_legacy, list | tuple):
                raise ReplayError("legacy_observed_actions must be a list.")
            frames = tuple(GoldenFrame.from_dict(item) for item in raw_frames)
            legacy = tuple(ensure_mapping(item, "legacy_observed_action") for item in raw_legacy)
            return cls(
                fixture_id=values["fixture_id"],
                session_id=values["session_id"],
                bundle=ensure_mapping(values["bundle"], "bundle"),
                frames=frames,
                legacy_observed_actions=legacy,
                source=values.get("source", "synthetic-deidentified"),
                version=values.get("version", GOLDEN_FIXTURE_VERSION),
                metadata=ensure_mapping(values.get("metadata", {}), "metadata"),
            )
        except KeyError as exc:
            raise ReplayError(f"golden fixture missing key: {exc.args[0]}") from exc

    @classmethod
    def from_path(cls, path: str | Path) -> GoldenFixture:
        fixture_path = Path(path)
        try:
            raw = fixture_path.read_bytes()
            data = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReplayError(f"invalid golden fixture JSON: {fixture_path}") from exc
        return cls.from_dict(ensure_mapping(data, "golden fixture"))

    @classmethod
    def from_path_with_digest(cls, path: str | Path) -> tuple[GoldenFixture, str]:
        """Load a fixture and return its exact source-file SHA-256 as evidence."""

        fixture_path = Path(path)
        try:
            raw = fixture_path.read_bytes()
        except OSError as exc:
            raise ReplayError(f"invalid golden fixture JSON: {fixture_path}") from exc
        return cls.from_path(fixture_path), hashlib.sha256(raw).hexdigest()


def _reject_json_constant(value: str) -> Any:
    raise ReplayError(f"non-standard JSON constant {value!r} is not allowed.")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError(f"duplicate JSON key {key!r} is not allowed.")
        result[key] = value
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _append_scheduled(
    scheduled: list[tuple[int, int, int, str, Mapping[str, Any]]],
    ordinal: int,
    recorded_at_ns: int,
    phase: int,
    event_type: str,
    payload: Mapping[str, Any],
) -> int:
    scheduled.append((recorded_at_ns, phase, ordinal, event_type, payload))
    return ordinal + 1


@dataclass(frozen=True, slots=True)
class ReplayRun:
    """One deterministic replay result."""

    fixture_id: str
    bundle_id: str
    session_id: str
    fixture_digest: str
    world_states: tuple[WorldState, ...]
    action_specs: tuple[ActionSpec, ...]
    action_handles: tuple[ActionHandle, ...]
    action_results: tuple[ActionResult, ...]
    events: tuple[EventRecord, ...]
    world_state_digest: str
    action_digest: str
    event_digest: str
    output_digest: str
    bundle_digest: str = ""

    @property
    def event_sequence(self) -> tuple[str, ...]:
        """The physical event type order used for deterministic comparison."""

        return tuple(item.event_type for item in self.events)

    @property
    def event_sequence_digest(self) -> str:
        return _digest(list(self.event_sequence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "bundle_id": self.bundle_id,
            "session_id": self.session_id,
            "fixture_digest": self.fixture_digest,
            "world_states": [item.to_dict() for item in self.world_states],
            "action_specs": [item.to_dict() for item in self.action_specs],
            "action_handles": [item.to_dict() for item in self.action_handles],
            "action_results": [item.to_dict() for item in self.action_results],
            "events": [item.to_dict() for item in self.events],
            "event_sequence": list(self.event_sequence),
            "event_sequence_digest": self.event_sequence_digest,
            "world_state_digest": self.world_state_digest,
            "action_digest": self.action_digest,
            "event_digest": self.event_digest,
            "output_digest": self.output_digest,
            "bundle_digest": self.bundle_digest,
        }


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """Machine-readable repeated replay evidence."""

    fixture_id: str
    bundle_id: str
    session_id: str
    fixture_digest: str
    repeat_count: int
    deterministic: bool
    runs: tuple[Mapping[str, Any], ...]
    output_digest: str
    status: str
    report_version: str = REPLAY_REPORT_VERSION

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.fixture_id, "fixture_id")
        ensure_non_empty_str(self.bundle_id, "bundle_id")
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.fixture_digest, "fixture_digest")
        ensure_positive_int(self.repeat_count, "repeat_count")
        if len(self.runs) != self.repeat_count:
            raise ReplayError("repeat_count must match report runs.")
        if not isinstance(self.runs, tuple):
            raise TypeError("runs must be a tuple.")
        normalized_runs: list[Mapping[str, Any]] = []
        for run in self.runs:
            run_mapping = ensure_mapping(run, "run")
            ensure_json_value(run_mapping, "run")
            normalized_runs.append(freeze_json_value(run_mapping))
        object.__setattr__(self, "runs", tuple(normalized_runs))
        if type(self.deterministic) is not bool:
            raise TypeError("deterministic must be a bool.")
        ensure_non_empty_str(self.output_digest, "output_digest")
        if self.status not in {"PASS", "FAIL"}:
            raise ReplayError("status must be PASS or FAIL.")
        ensure_non_empty_str(self.report_version, "report_version")

    @property
    def report_id(self) -> str:
        return f"replay-{self.fixture_id}-{self.bundle_id}"

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "report_type": "golden_replay",
            "report_version": self.report_version,
            "report_id": self.report_id,
            "fixture_id": self.fixture_id,
            "fixture_digest": self.fixture_digest,
            "bundle_id": self.bundle_id,
            "bundle_digest": self.runs[0].get("bundle_digest", "") if self.runs else "",
            "session_id": self.session_id,
            "repeat_count": self.repeat_count,
            "deterministic": self.deterministic,
            "status": self.status,
            "runs": [to_json_dict(item) for item in self.runs],
            "output_digest": self.output_digest,
        }
        body["report_digest"] = _digest(body)
        return body

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=False)

    def write_json(self, path: str | Path) -> Path:
        report_path = Path(path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(self.to_json() + "\n", encoding="utf-8")
        return report_path

    def assert_deterministic(self) -> None:
        if not self.deterministic:
            raise ReplayDeterminismError(
                f"golden replay produced {self.repeat_count} different outputs."
            )


class GoldenReplayRunner:
    """Turn a fixed fixture into WorldState, actions, and a chained EventTape."""

    def __init__(
        self,
        fixture: GoldenFixture | Mapping[str, Any] | str | Path,
        *,
        fixture_digest: str | None = None,
        tape_path: str | Path | None = None,
    ) -> None:
        self._fixture_path: Path | None = None
        if isinstance(fixture, str | Path):
            self._fixture_path = Path(fixture)
            loaded, content_digest = GoldenFixture.from_path_with_digest(self._fixture_path)
            self.fixture = loaded
            self.fixture_digest = content_digest
        elif isinstance(fixture, GoldenFixture):
            self.fixture = fixture
            self.fixture_digest = fixture_digest or fixture.digest()
        else:
            self.fixture = GoldenFixture.from_dict(fixture)
            self.fixture_digest = fixture_digest or self.fixture.digest()
        ensure_non_empty_str(self.fixture_digest, "fixture_digest")
        self.tape_path = None if tape_path is None else Path(tape_path)

    def _make_handle(self, action: GoldenAction) -> ActionHandle:
        spec = action.spec
        if spec.timeout_ns <= 1:
            raise ReplayError("golden action timeout must leave room for a start event.")
        return ActionHandle(
            handle_id=f"{spec.action_id}:handle:{spec.origin_world_state_version}",
            session_id=spec.session_id,
            spec=spec,
            issued_at_ns=spec.requested_at_ns,
            expires_at_ns=spec.deadline_ns - 1,
            generation=1,
            started_at_ns=spec.requested_at_ns + 1,
            evidence={"source": "golden-replay"},
        )

    def _make_result(self, action: GoldenAction, handle: ActionHandle) -> ActionResult:
        result = ActionResult(
            handle_id=handle.handle_id,
            action_id=action.spec.action_id,
            session_id=action.spec.session_id,
            termination=action.termination,
            started_at_ns=handle.started_at_ns
            if handle.started_at_ns is not None
            else handle.issued_at_ns,
            completed_at_ns=action.completed_at_ns,
            origin_frame_id=action.spec.origin_frame_id,
            origin_world_state_version=action.spec.origin_world_state_version,
            result_frame_id=action.result_frame_id,
            result_world_state_version=action.result_world_state_version,
            generation=handle.generation,
            details=action.details,
            evidence=action.evidence,
        )
        handle.validate_result(result)
        return result

    def run(self) -> ReplayRun:
        actions = tuple(item.action for item in self.fixture.frames if item.action is not None)
        handles = tuple(self._make_handle(item) for item in actions if item is not None)
        results = tuple(
            self._make_result(item, handle)
            for item, handle in zip(actions, handles, strict=True)
            if item is not None
        )
        handle_by_action = {handle.action_id: handle for handle in handles}
        result_by_action = {result.action_id: result for result in results}
        actions_by_result_frame: dict[int, list[GoldenAction]] = {}
        for item in actions:
            if item is not None:
                actions_by_result_frame.setdefault(item.result_frame_id, []).append(item)

        temporary: tempfile.TemporaryDirectory[str] | None = None
        if self.tape_path is None:
            temporary = tempfile.TemporaryDirectory(prefix="maple-golden-replay-")
            tape_path = Path(temporary.name) / "events.jsonl"
        else:
            tape_path = self.tape_path
            if tape_path.exists():
                tape_path.unlink()
        try:
            tape = EventTape(tape_path)
            for frame in self.fixture.frames:
                world = frame.world_state
                packet = frame.packet
                scheduled: list[tuple[int, int, int, str, Mapping[str, Any]]] = []
                ordinal = 0
                ordinal = _append_scheduled(
                    scheduled,
                    ordinal,
                    packet.received_at_ns,
                    0,
                    "frame.observed",
                    {"packet": packet.to_dict(), "world_state": world.to_dict()},
                )
                for planned in actions_by_result_frame.get(packet.frame_id, []):
                    result = result_by_action[planned.spec.action_id]
                    # A result carried from an earlier origin is emitted before
                    # a new plan at the same timestamp.  A same-frame result is
                    # kept after that frame's own lifecycle events.
                    is_carried_result = planned.spec.origin_frame_id < packet.frame_id
                    terminal_phase = 1 if is_carried_result else 6
                    ordinal = _append_scheduled(
                        scheduled,
                        ordinal,
                        result.completed_at_ns,
                        terminal_phase,
                        "action.terminal",
                        {
                            "provenance": "core_v2_result",
                            "action": result.to_dict(),
                        },
                    )
                    ordinal = _append_scheduled(
                        scheduled,
                        ordinal,
                        result.completed_at_ns,
                        2 if is_carried_result else 7,
                        "input.release_all",
                        {
                            "provenance": "core_v2_simulated",
                            "action_id": result.action_id,
                            "handle_id": result.handle_id,
                            "generation": result.generation,
                        },
                    )
                if frame.action is not None:
                    spec = frame.action.spec
                    handle = handle_by_action[spec.action_id]
                    ordinal = _append_scheduled(
                        scheduled,
                        ordinal,
                        spec.requested_at_ns,
                        3,
                        "action.proposed",
                        {"provenance": "core_v2_planned", "action": spec.to_dict()},
                    )
                    ordinal = _append_scheduled(
                        scheduled,
                        ordinal,
                        handle.issued_at_ns,
                        4,
                        "action.issued",
                        {"provenance": "core_v2_planned", "handle": handle.to_dict()},
                    )
                    ordinal = _append_scheduled(
                        scheduled,
                        ordinal,
                        handle.started_at_ns
                        if handle.started_at_ns is not None
                        else handle.issued_at_ns,
                        5,
                        "action.started",
                        {"provenance": "core_v2_planned", "handle": handle.to_dict()},
                    )
                for recorded_at_ns, _, _, event_type, payload in sorted(scheduled):
                    tape.append(
                        event_type=event_type,
                        payload=payload,
                        session_id=self.fixture.session_id,
                        frame_id=packet.frame_id,
                        world_state_version=world.world_state_version,
                        recorded_at_ns=recorded_at_ns,
                    )
            events = tape.read_all()
        finally:
            if temporary is not None:
                temporary.cleanup()

        world_states = tuple(item.world_state for item in self.fixture.frames)
        world_payload = [item.to_dict() for item in world_states]
        action_payload = {
            "specs": [item.spec.to_dict() for item in actions if item is not None],
            "handles": [item.to_dict() for item in handles],
            "results": [item.to_dict() for item in results],
        }
        event_payload = [item.to_dict() for item in events]
        world_digest = _digest(world_payload)
        action_digest = _digest(action_payload)
        event_digest = _digest(event_payload)
        output_digest = _digest(
            {
                "fixture_digest": self.fixture_digest,
                "bundle_id": self.fixture.bundle_id,
                "bundle_digest": self.fixture.bundle_digest,
                "world_state_digest": world_digest,
                "action_digest": action_digest,
                "event_digest": event_digest,
            }
        )
        return ReplayRun(
            fixture_id=self.fixture.fixture_id,
            bundle_id=self.fixture.bundle_id,
            session_id=self.fixture.session_id,
            fixture_digest=self.fixture_digest,
            world_states=world_states,
            action_specs=tuple(item.spec for item in actions if item is not None),
            action_handles=handles,
            action_results=results,
            events=events,
            world_state_digest=world_digest,
            action_digest=action_digest,
            event_digest=event_digest,
            output_digest=output_digest,
            bundle_digest=self.fixture.bundle_digest,
        )

    replay = run

    def run_repeated(self, repetitions: int = 3) -> ReplayReport:
        ensure_positive_int(repetitions, "repetitions")
        results = tuple(self.run() for _ in range(repetitions))
        output_digests = tuple(item.output_digest for item in results)
        deterministic = len(set(output_digests)) == 1
        report_runs = tuple(
            {
                "run_index": index,
                "world_state_digest": item.world_state_digest,
                "action_digest": item.action_digest,
                "event_digest": item.event_digest,
                "event_sequence_digest": item.event_sequence_digest,
                "event_sequence": list(item.event_sequence),
                "output_digest": item.output_digest,
                "event_count": len(item.events),
                "planned_action_count": len(item.action_specs),
                "bundle_digest": item.bundle_digest,
            }
            for index, item in enumerate(results)
        )
        report_output_digest = _digest(
            {
                "fixture_digest": self.fixture_digest,
                "bundle_id": self.fixture.bundle_id,
                "bundle_digest": self.fixture.bundle_digest,
                "runs": report_runs,
            }
        )
        return ReplayReport(
            fixture_id=self.fixture.fixture_id,
            bundle_id=self.fixture.bundle_id,
            session_id=self.fixture.session_id,
            fixture_digest=self.fixture_digest,
            repeat_count=repetitions,
            deterministic=deterministic,
            runs=report_runs,
            output_digest=report_output_digest,
            status="PASS" if deterministic else "FAIL",
        )

    def run_three_times(self) -> ReplayReport:
        return self.run_repeated(3)
