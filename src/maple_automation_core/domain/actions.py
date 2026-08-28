from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from ._contract_utils import (
    ensure_json_value,
    ensure_mapping,
    ensure_non_empty_str,
    ensure_non_negative_int,
    ensure_positive_int,
    ensure_time_ns,
    freeze_json_value,
    to_json_dict,
)


class ActionKind(Enum):
    MOVE = "move"
    JUMP = "jump"
    DROP = "drop"
    CLIMB = "climb"
    ATTACK = "attack"
    STOP = "stop"


class ActionTermination(Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    PRECONDITION_LOST = "precondition_lost"
    OBSERVATION_STALE = "observation_stale"
    INPUT_FAILURE = "input_failure"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ActionReference:
    """Immutable identity of the world snapshot associated with an action."""

    session_id: str
    frame_id: int
    world_state_version: int

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_negative_int(self.frame_id, "frame_id")
        ensure_non_negative_int(self.world_state_version, "world_state_version")

    @property
    def version(self) -> int:
        return self.world_state_version

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "world_state_version": self.world_state_version,
        }


@dataclass(frozen=True, slots=True)
class ActionSpec:
    session_id: str
    action_id: str
    kind: ActionKind
    requested_at_ns: int
    timeout_ns: int
    origin_frame_id: int
    origin_world_state_version: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.action_id, "action_id")
        if not isinstance(self.kind, ActionKind):
            raise TypeError("kind must be ActionKind.")
        ensure_time_ns(self.requested_at_ns, "requested_at_ns")
        ensure_positive_int(self.timeout_ns, "timeout_ns")
        ensure_non_negative_int(self.origin_frame_id, "origin_frame_id")
        ensure_non_negative_int(self.origin_world_state_version, "origin_world_state_version")
        payload = ensure_mapping(self.payload, "payload")
        evidence = ensure_mapping(self.evidence, "evidence")
        ensure_json_value(payload, "payload")
        ensure_json_value(evidence, "evidence")
        object.__setattr__(self, "payload", freeze_json_value(payload))
        object.__setattr__(self, "evidence", freeze_json_value(evidence))

    @property
    def deadline_ns(self) -> int:
        return self.requested_at_ns + self.timeout_ns

    @property
    def origin(self) -> ActionReference:
        return ActionReference(
            session_id=self.session_id,
            frame_id=self.origin_frame_id,
            world_state_version=self.origin_world_state_version,
        )

    def is_stale_at(self, now_ns: int) -> bool:
        ensure_time_ns(now_ns, "now_ns")
        return now_ns >= self.deadline_ns

    def is_expired_at(self, now_ns: int) -> bool:
        return self.is_stale_at(now_ns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "action_id": self.action_id,
            "kind": self.kind.value,
            "requested_at_ns": self.requested_at_ns,
            "timeout_ns": self.timeout_ns,
            "origin_frame_id": self.origin_frame_id,
            "origin_world_state_version": self.origin_world_state_version,
            "payload": to_json_dict(self.payload),
            "evidence": to_json_dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActionSpec:
        values = ensure_mapping(data, "ActionSpec payload")
        try:
            payload = ensure_mapping(values.get("payload", {}), "payload")
            evidence = ensure_mapping(values.get("evidence", {}), "evidence")
            return cls(
                session_id=values["session_id"],
                action_id=values["action_id"],
                kind=ActionKind(values["kind"]),
                requested_at_ns=values["requested_at_ns"],
                timeout_ns=values["timeout_ns"],
                origin_frame_id=values["origin_frame_id"],
                origin_world_state_version=values["origin_world_state_version"],
                payload=payload,
                evidence=evidence,
            )
        except KeyError as exc:
            raise ValueError(f"ActionSpec payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class ActionHandle:
    """Unique execution handle for one requested action."""

    handle_id: str
    session_id: str
    spec: ActionSpec
    issued_at_ns: int
    expires_at_ns: int
    generation: int
    started_at_ns: int | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.handle_id, "handle_id")
        ensure_non_empty_str(self.session_id, "session_id")
        if not isinstance(self.spec, ActionSpec):
            raise TypeError("spec must be ActionSpec.")
        if self.session_id != self.spec.session_id:
            raise ValueError("handle session_id must match spec session_id.")
        ensure_time_ns(self.issued_at_ns, "issued_at_ns")
        ensure_time_ns(self.expires_at_ns, "expires_at_ns")
        ensure_non_negative_int(self.generation, "generation")
        if self.issued_at_ns < self.spec.requested_at_ns:
            raise ValueError("issued_at_ns must be >= spec.requested_at_ns.")
        if self.issued_at_ns > self.spec.deadline_ns:
            raise ValueError("issued_at_ns must be <= spec.deadline_ns.")
        if self.expires_at_ns > self.spec.deadline_ns:
            raise ValueError("expires_at_ns must be <= spec.deadline_ns.")
        if self.expires_at_ns < self.issued_at_ns:
            raise ValueError("expires_at_ns must be >= issued_at_ns.")
        if self.started_at_ns is not None:
            ensure_time_ns(self.started_at_ns, "started_at_ns")
            if not self.issued_at_ns <= self.started_at_ns <= self.expires_at_ns:
                raise ValueError("started_at_ns must be within the handle lease.")
        evidence = ensure_mapping(self.evidence, "evidence")
        ensure_json_value(evidence, "evidence")
        object.__setattr__(self, "evidence", freeze_json_value(evidence))

    @property
    def action_id(self) -> str:
        return self.spec.action_id

    @property
    def origin(self) -> ActionReference:
        return self.spec.origin

    @property
    def origin_frame_id(self) -> int:
        return self.spec.origin_frame_id

    @property
    def origin_world_state_version(self) -> int:
        return self.spec.origin_world_state_version

    @property
    def deadline_ns(self) -> int:
        return self.spec.deadline_ns

    @property
    def is_started(self) -> bool:
        return self.started_at_ns is not None

    def is_active_at(self, now_ns: int) -> bool:
        ensure_time_ns(now_ns, "now_ns")
        return self.started_at_ns is not None and self.issued_at_ns <= now_ns < self.expires_at_ns

    def start(
        self,
        started_at_ns: int,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> ActionHandle:
        """Return the started form of this immutable execution handle."""

        if self.started_at_ns is not None:
            raise ValueError("action handle has already started.")
        ensure_time_ns(started_at_ns, "started_at_ns")
        if not self.issued_at_ns <= started_at_ns <= self.expires_at_ns:
            raise ValueError("started_at_ns must be within the handle lease.")
        merged_evidence = self.evidence if evidence is None else evidence
        return replace(self, started_at_ns=started_at_ns, evidence=merged_evidence)

    def validate_result(self, result: ActionResult) -> None:
        """Validate that a terminal result belongs to this handle."""

        if not isinstance(result, ActionResult):
            raise TypeError("result must be ActionResult.")
        if result.handle_id != self.handle_id:
            raise ValueError("result.handle_id must match handle_id.")
        if result.action_id != self.spec.action_id:
            raise ValueError("result.action_id must match spec.action_id.")
        if result.session_id != self.session_id:
            raise ValueError("result.session_id must match handle session_id.")
        if result.origin != self.origin:
            raise ValueError("result origin must match action origin.")
        if result.generation != self.generation:
            raise ValueError("result.generation must match handle generation.")
        lower_bound = self.issued_at_ns if self.started_at_ns is None else self.started_at_ns
        if result.started_at_ns < lower_bound:
            raise ValueError("result.started_at_ns predates handle lifecycle.")

    def is_expired(self, now_ns: int) -> bool:
        if not isinstance(now_ns, int) or now_ns < 0:
            raise ValueError("now_ns must be non-negative int.")
        return now_ns >= self.expires_at_ns

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle_id": self.handle_id,
            "session_id": self.session_id,
            "spec": self.spec.to_dict(),
            "issued_at_ns": self.issued_at_ns,
            "expires_at_ns": self.expires_at_ns,
            "generation": self.generation,
            "started_at_ns": self.started_at_ns,
            "evidence": to_json_dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActionHandle:
        values = ensure_mapping(data, "ActionHandle payload")
        try:
            evidence = ensure_mapping(values.get("evidence", {}), "evidence")
            return cls(
                handle_id=values["handle_id"],
                session_id=values["session_id"],
                spec=ActionSpec.from_dict(values["spec"]),
                issued_at_ns=values["issued_at_ns"],
                expires_at_ns=values["expires_at_ns"],
                generation=values["generation"],
                started_at_ns=values.get("started_at_ns"),
                evidence=evidence,
            )
        except KeyError as exc:
            raise ValueError(f"ActionHandle payload missing key: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Terminal result for an action handle."""

    handle_id: str
    action_id: str
    session_id: str
    termination: ActionTermination
    started_at_ns: int
    completed_at_ns: int
    origin_frame_id: int
    origin_world_state_version: int
    result_frame_id: int
    result_world_state_version: int
    generation: int
    details: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.handle_id, "handle_id")
        ensure_non_empty_str(self.action_id, "action_id")
        ensure_non_empty_str(self.session_id, "session_id")
        if not isinstance(self.termination, ActionTermination):
            raise TypeError("termination must be ActionTermination.")
        ensure_time_ns(self.started_at_ns, "started_at_ns")
        ensure_time_ns(self.completed_at_ns, "completed_at_ns")
        if self.completed_at_ns < self.started_at_ns:
            raise ValueError("completed_at_ns must be >= started_at_ns.")
        ensure_non_negative_int(self.origin_frame_id, "origin_frame_id")
        ensure_non_negative_int(self.origin_world_state_version, "origin_world_state_version")
        ensure_non_negative_int(self.result_frame_id, "result_frame_id")
        ensure_non_negative_int(self.result_world_state_version, "result_world_state_version")
        if self.result_frame_id < self.origin_frame_id:
            raise ValueError("result_frame_id must be >= origin_frame_id.")
        if self.result_world_state_version <= self.origin_world_state_version:
            raise ValueError(
                "result_world_state_version must be newer than origin_world_state_version."
            )
        ensure_non_negative_int(self.generation, "generation")
        details = ensure_mapping(self.details, "details")
        evidence = ensure_mapping(self.evidence, "evidence")
        ensure_json_value(details, "details")
        ensure_json_value(evidence, "evidence")
        object.__setattr__(self, "details", freeze_json_value(details))
        object.__setattr__(self, "evidence", freeze_json_value(evidence))

    @property
    def duration_ns(self) -> int:
        return self.completed_at_ns - self.started_at_ns

    @property
    def origin(self) -> ActionReference:
        return ActionReference(
            session_id=self.session_id,
            frame_id=self.origin_frame_id,
            world_state_version=self.origin_world_state_version,
        )

    @property
    def result(self) -> ActionReference:
        return ActionReference(
            session_id=self.session_id,
            frame_id=self.result_frame_id,
            world_state_version=self.result_world_state_version,
        )

    def validate_against(self, handle: ActionHandle) -> None:
        if not isinstance(handle, ActionHandle):
            raise TypeError("handle must be ActionHandle.")
        handle.validate_result(self)

    @classmethod
    def from_handle(
        cls,
        handle: ActionHandle,
        *,
        termination: ActionTermination,
        completed_at_ns: int,
        result_frame_id: int,
        result_world_state_version: int,
        started_at_ns: int | None = None,
        details: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> ActionResult:
        if not isinstance(handle, ActionHandle):
            raise TypeError("handle must be ActionHandle.")
        actual_started = (
            handle.issued_at_ns if handle.started_at_ns is None else handle.started_at_ns
        )
        if started_at_ns is not None:
            actual_started = started_at_ns
        result = cls(
            handle_id=handle.handle_id,
            action_id=handle.action_id,
            session_id=handle.session_id,
            termination=termination,
            started_at_ns=actual_started,
            completed_at_ns=completed_at_ns,
            origin_frame_id=handle.origin_frame_id,
            origin_world_state_version=handle.origin_world_state_version,
            result_frame_id=result_frame_id,
            result_world_state_version=result_world_state_version,
            generation=handle.generation,
            details={} if details is None else details,
            evidence={} if evidence is None else evidence,
        )
        handle.validate_result(result)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle_id": self.handle_id,
            "action_id": self.action_id,
            "session_id": self.session_id,
            "termination": self.termination.value,
            "started_at_ns": self.started_at_ns,
            "completed_at_ns": self.completed_at_ns,
            "origin_frame_id": self.origin_frame_id,
            "origin_world_state_version": self.origin_world_state_version,
            "result_frame_id": self.result_frame_id,
            "result_world_state_version": self.result_world_state_version,
            "generation": self.generation,
            "details": to_json_dict(self.details),
            "evidence": to_json_dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActionResult:
        values = ensure_mapping(data, "ActionResult payload")
        try:
            details = ensure_mapping(values.get("details", {}), "details")
            evidence = ensure_mapping(values.get("evidence", {}), "evidence")
            return cls(
                handle_id=values["handle_id"],
                action_id=values["action_id"],
                session_id=values["session_id"],
                termination=ActionTermination(values["termination"]),
                started_at_ns=values["started_at_ns"],
                completed_at_ns=values["completed_at_ns"],
                origin_frame_id=values["origin_frame_id"],
                origin_world_state_version=values["origin_world_state_version"],
                result_frame_id=values["result_frame_id"],
                result_world_state_version=values["result_world_state_version"],
                generation=values["generation"],
                details=details,
                evidence=evidence,
            )
        except KeyError as exc:
            raise ValueError(f"ActionResult payload missing key: {exc.args[0]}") from exc
