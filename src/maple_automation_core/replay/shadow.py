from __future__ import annotations

import hashlib
import json
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
    ensure_time_ns,
    freeze_json_value,
    to_json_dict,
)
from maple_automation_core.domain.actions import ActionKind, ActionSpec
from maple_automation_core.replay.golden import (
    GoldenFixture,
    GoldenReplayRunner,
    ReplayRun,
)

SHADOW_REPORT_VERSION = "1.0.0"


class ShadowError(ValueError):
    """Raised when a Shadow input or input-ownership invariant is invalid."""


@dataclass(frozen=True, slots=True)
class DryRunReceipt:
    """Receipt for a simulated call; it is never sent to a real endpoint."""

    operation: str
    sequence: int
    simulated: bool = True

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.operation, "operation")
        ensure_non_negative_int(self.sequence, "sequence")
        if type(self.simulated) is not bool:
            raise TypeError("simulated must be a bool.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "sequence": self.sequence,
            "simulated": self.simulated,
        }


class DryRunInputSink:
    """Explicit sink that records plans without touching keyboard/receiver APIs."""

    def __init__(self) -> None:
        self._receipts: list[DryRunReceipt] = []
        self._planned_actions: list[dict[str, Any]] = []
        self._real_boundary_attempts: list[dict[str, str]] = []
        self._connected = False

    def _record(self, operation: str) -> DryRunReceipt:
        ensure_non_empty_str(operation, "operation")
        receipt = DryRunReceipt(operation=operation, sequence=len(self._receipts))
        self._receipts.append(receipt)
        return receipt

    def connect(self) -> DryRunReceipt:
        if self._connected:
            raise ShadowError("DryRunInputSink is already connected.")
        self._connected = True
        return self._record("connect")

    def close(self) -> DryRunReceipt:
        if not self._connected:
            raise ShadowError("DryRunInputSink is not connected.")
        self._connected = False
        return self._record("close")

    def reset(self) -> None:
        """Clear one run's audit state before a deterministic replay."""

        if self._connected:
            raise ShadowError("cannot reset a connected DryRunInputSink.")
        self._receipts.clear()
        self._planned_actions.clear()
        self._real_boundary_attempts.clear()

    def record_real_boundary_attempt(self, boundary: str, operation: str) -> None:
        """Instrument a forbidden adapter call without executing that call."""

        allowed = {"keyboard", "mouse", "receiver", "window"}
        if boundary not in allowed:
            raise ShadowError(f"Unknown real-input boundary: {boundary}")
        ensure_non_empty_str(operation, "operation")
        self._real_boundary_attempts.append({"boundary": boundary, "operation": operation})

    def key_down(self, key: str) -> DryRunReceipt:
        self._ensure_connected()
        return self._record(f"key_down:{key}")

    def key_up(self, key: str) -> DryRunReceipt:
        self._ensure_connected()
        return self._record(f"key_up:{key}")

    def release_all(self) -> DryRunReceipt:
        self._ensure_connected()
        return self._record("release_all")

    def record_planned_action(self, spec: ActionSpec) -> DryRunReceipt:
        if not isinstance(spec, ActionSpec):
            raise TypeError("spec must be ActionSpec.")
        self._ensure_connected()
        self._planned_actions.append(spec.to_dict())
        return self._record("planned_action")

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ShadowError("DryRunInputSink is not connected.")

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def receipts(self) -> tuple[DryRunReceipt, ...]:
        return tuple(self._receipts)

    @property
    def dry_run_call_count(self) -> int:
        return len(self._receipts)

    @property
    def input_call_count(self) -> int:
        """Count of simulated sink calls; it is not a live input count."""

        return self.dry_run_call_count

    @property
    def real_input_call_count(self) -> int:
        """Count instrumented attempts to cross any real-input boundary."""

        return len(self._real_boundary_attempts)

    @property
    def planned_action_count(self) -> int:
        return len(self._planned_actions)

    def assert_no_real_input(self) -> None:
        if self.real_input_call_count != 0:
            raise ShadowError("DryRunInputSink reported a real input call.")

    def audit(self) -> dict[str, Any]:
        boundary_counts = {
            boundary: sum(
                attempt["boundary"] == boundary for attempt in self._real_boundary_attempts
            )
            for boundary in ("keyboard", "mouse", "receiver", "window")
        }
        return {
            "sink_type": type(self).__name__,
            "connected": self.connected,
            "dry_run_call_count": self.dry_run_call_count,
            "planned_action_count": self.planned_action_count,
            "real_input_call_count": self.real_input_call_count,
            "keyboard_call_count": boundary_counts["keyboard"],
            "mouse_call_count": boundary_counts["mouse"],
            "receiver_call_count": boundary_counts["receiver"],
            "window_call_count": boundary_counts["window"],
            "boundary_attempts": list(self._real_boundary_attempts),
            "proof": "instrumented boundary ledger; concrete dry-run sink has no live adapter",
            "receipts": [item.to_dict() for item in self.receipts],
        }


@dataclass(frozen=True, slots=True)
class LegacyObservedAction:
    """An action observed from Legacy; it is not a Core v2 execution result."""

    action_id: str
    session_id: str
    frame_id: int
    world_state_version: int
    kind: ActionKind
    observed_at_ns: int
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.action_id, "action_id")
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_negative_int(self.frame_id, "frame_id")
        ensure_non_negative_int(self.world_state_version, "world_state_version")
        if not isinstance(self.kind, ActionKind):
            raise TypeError("kind must be ActionKind.")
        ensure_time_ns(self.observed_at_ns, "observed_at_ns")
        details = ensure_mapping(self.details, "details")
        ensure_json_value(details, "details")
        object.__setattr__(self, "details", freeze_json_value(details))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LegacyObservedAction:
        values = ensure_mapping(data, "legacy observed action")
        try:
            return cls(
                action_id=values["action_id"],
                session_id=values["session_id"],
                frame_id=values["frame_id"],
                world_state_version=values["world_state_version"],
                kind=ActionKind(values["kind"]),
                observed_at_ns=values["observed_at_ns"],
                details=ensure_mapping(values.get("details", {}), "details"),
            )
        except KeyError as exc:
            raise ShadowError(f"legacy observed action missing key: {exc.args[0]}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "provenance": "legacy_observed",
            "execution_status": "observed_not_core_executed",
            "action_id": self.action_id,
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "world_state_version": self.world_state_version,
            "kind": self.kind.value,
            "observed_at_ns": self.observed_at_ns,
            "details": to_json_dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """Machine-readable Core-v2-plan vs Legacy-observed evidence."""

    fixture_id: str
    fixture_digest: str
    bundle_id: str
    bundle_digest: str
    session_id: str
    replay_output_digest: str
    planned_actions: tuple[Mapping[str, Any], ...]
    legacy_observed_actions: tuple[Mapping[str, Any], ...]
    diffs: tuple[Mapping[str, Any], ...]
    input_audit: Mapping[str, Any]
    output_digest: str
    status: str
    report_version: str = SHADOW_REPORT_VERSION

    def __post_init__(self) -> None:
        ensure_non_empty_str(self.fixture_id, "fixture_id")
        ensure_non_empty_str(self.fixture_digest, "fixture_digest")
        ensure_non_empty_str(self.bundle_id, "bundle_id")
        ensure_non_empty_str(self.bundle_digest, "bundle_digest")
        ensure_non_empty_str(self.session_id, "session_id")
        ensure_non_empty_str(self.replay_output_digest, "replay_output_digest")
        ensure_non_empty_str(self.output_digest, "output_digest")
        ensure_non_empty_str(self.report_version, "report_version")
        if self.status not in {"PASS", "FAIL"}:
            raise ShadowError("status must be PASS or FAIL.")
        for field_name, records in (
            ("planned_actions", self.planned_actions),
            ("legacy_observed_actions", self.legacy_observed_actions),
            ("diffs", self.diffs),
        ):
            if not isinstance(records, tuple):
                raise TypeError(f"{field_name} must be a tuple.")
            for record in records:
                ensure_json_value(record, field_name)
        normalized_audit = ensure_mapping(self.input_audit, "input_audit")
        ensure_json_value(normalized_audit, "input_audit")
        object.__setattr__(self, "input_audit", freeze_json_value(normalized_audit))
        normalized_records = {
            "planned_actions": self.planned_actions,
            "legacy_observed_actions": self.legacy_observed_actions,
            "diffs": self.diffs,
        }
        for field_name, records in normalized_records.items():
            object.__setattr__(
                self,
                field_name,
                tuple(freeze_json_value(ensure_mapping(item, field_name)) for item in records),
            )
        real_count = self.input_audit.get("real_input_call_count", 0)
        if type(real_count) is not int or real_count < 0:
            raise ShadowError("input_audit real_input_call_count must be >= 0.")

    @property
    def report_id(self) -> str:
        return f"shadow-{self.fixture_id}-{self.bundle_id}"

    @property
    def core_v2_real_input_call_count(self) -> int:
        value = self.input_audit.get(
            "core_v2_real_input_call_count",
            self.input_audit.get("real_input_call_count", -1),
        )
        return value if isinstance(value, int) else -1

    def to_dict(self) -> dict[str, Any]:
        taxonomy_counts: dict[str, int] = {}
        unclassified_diff_count = 0
        allowed_taxonomy = {"MATCH", "KIND_MISMATCH", "PLANNED_ONLY", "LEGACY_ONLY"}
        for item in self.diffs:
            taxonomy = item.get("taxonomy")
            if isinstance(taxonomy, str):
                taxonomy_counts[taxonomy] = taxonomy_counts.get(taxonomy, 0) + 1
            if taxonomy not in allowed_taxonomy:
                unclassified_diff_count += 1
        body: dict[str, Any] = {
            "report_type": "shadow",
            "report_version": self.report_version,
            "report_id": self.report_id,
            "fixture_id": self.fixture_id,
            "fixture_digest": self.fixture_digest,
            "bundle_id": self.bundle_id,
            "bundle_digest": self.bundle_digest,
            "session_id": self.session_id,
            "replay_output_digest": self.replay_output_digest,
            "planned_actions": [to_json_dict(item) for item in self.planned_actions],
            "legacy_observed_actions": [
                to_json_dict(item) for item in self.legacy_observed_actions
            ],
            "diffs": [to_json_dict(item) for item in self.diffs],
            "diff_summary": {
                "taxonomy_counts": taxonomy_counts,
                "unclassified_diff_count": unclassified_diff_count,
            },
            "input_audit": to_json_dict(self.input_audit),
            "status": self.status,
            "output_digest": self.output_digest,
        }
        body["report_digest"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        return body

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=False)

    def write_json(self, path: str | Path) -> Path:
        report_path = Path(path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(self.to_json() + "\n", encoding="utf-8")
        return report_path

    def assert_no_real_input(self) -> None:
        if self.core_v2_real_input_call_count != 0:
            raise ShadowError("Shadow report contains a non-zero real input count.")


class ShadowRunner:
    """Replay Core v2 plans while preserving Legacy's real-input ownership."""

    def __init__(
        self,
        fixture: GoldenFixture | Mapping[str, Any] | str | Path | GoldenReplayRunner,
        *,
        input_sink: DryRunInputSink | None = None,
    ) -> None:
        self.replay_runner = (
            fixture if isinstance(fixture, GoldenReplayRunner) else GoldenReplayRunner(fixture)
        )
        self.input_sink = DryRunInputSink() if input_sink is None else input_sink
        if type(self.input_sink) is not DryRunInputSink:
            raise TypeError("ShadowRunner requires an explicit DryRunInputSink.")

    @property
    def fixture(self) -> GoldenFixture:
        return self.replay_runner.fixture

    def _legacy_actions(self) -> tuple[LegacyObservedAction, ...]:
        actions = tuple(
            LegacyObservedAction.from_dict(item) for item in self.fixture.legacy_observed_actions
        )
        frame_versions = {
            frame.packet.frame_id: frame.world_state.world_state_version
            for frame in self.fixture.frames
        }
        last_time = -1
        for observed_item in actions:
            if observed_item.session_id != self.fixture.session_id:
                raise ShadowError("legacy observed action session mismatch.")
            if observed_item.frame_id not in frame_versions:
                raise ShadowError("legacy observed action references unknown frame.")
            if frame_versions[observed_item.frame_id] != observed_item.world_state_version:
                raise ShadowError("legacy observed action frame/version mismatch.")
            if observed_item.observed_at_ns < last_time:
                raise ShadowError("legacy observed action time moved backwards.")
            last_time = observed_item.observed_at_ns
        return actions

    @staticmethod
    def _diffs(
        planned: tuple[ActionSpec, ...],
        observed: tuple[LegacyObservedAction, ...],
    ) -> tuple[Mapping[str, Any], ...]:
        planned_by_frame: dict[int, list[ActionSpec]] = {}
        observed_by_frame: dict[int, list[LegacyObservedAction]] = {}
        for planned_item in planned:
            planned_by_frame.setdefault(planned_item.origin_frame_id, []).append(planned_item)
        for observed_item in observed:
            observed_by_frame.setdefault(observed_item.frame_id, []).append(observed_item)
        records: list[Mapping[str, Any]] = []
        for frame_id in sorted(set(planned_by_frame) | set(observed_by_frame)):
            left = planned_by_frame.get(frame_id, [])
            right = observed_by_frame.get(frame_id, [])
            pairs = min(len(left), len(right))
            for index in range(pairs):
                planned_item = left[index]
                observed_item = right[index]
                taxonomy = (
                    "MATCH"
                    if planned_item.kind.value == observed_item.kind.value
                    else "KIND_MISMATCH"
                )
                records.append(
                    {
                        "frame_id": frame_id,
                        "taxonomy": taxonomy,
                        "planned_action_id": planned_item.action_id,
                        "planned_kind": planned_item.kind.value,
                        "legacy_action_id": observed_item.action_id,
                        "legacy_kind": observed_item.kind.value,
                    }
                )
            for planned_item in left[pairs:]:
                records.append(
                    {
                        "frame_id": frame_id,
                        "taxonomy": "PLANNED_ONLY",
                        "planned_action_id": planned_item.action_id,
                        "planned_kind": planned_item.kind.value,
                    }
                )
            for observed_item in right[pairs:]:
                records.append(
                    {
                        "frame_id": frame_id,
                        "taxonomy": "LEGACY_ONLY",
                        "legacy_action_id": observed_item.action_id,
                        "legacy_kind": observed_item.kind.value,
                    }
                )
        return tuple(records)

    def run(self) -> ShadowReport:
        replay: ReplayRun = self.replay_runner.run()
        observed = self._legacy_actions()
        self.input_sink.reset()
        self.input_sink.connect()
        planned_records: list[Mapping[str, Any]] = []
        try:
            for spec in replay.action_specs:
                self.input_sink.record_planned_action(spec)
                planned_records.append(
                    {
                        "provenance": "core_v2_planned",
                        "execution_status": "planned_not_executed",
                        "action": spec.to_dict(),
                    }
                )
        finally:
            if self.input_sink.connected:
                try:
                    if replay.action_specs:
                        self.input_sink.release_all()
                finally:
                    self.input_sink.close()
        self.input_sink.assert_no_real_input()
        diffs = self._diffs(replay.action_specs, observed)
        audit = self.input_sink.audit()
        audit["legacy_observed_action_count"] = len(observed)
        audit["planned_action_count"] = len(replay.action_specs)
        audit["core_v2_real_input_call_count"] = audit["real_input_call_count"]
        core_execution_events = sum(
            item.get("execution_status") != "planned_not_executed" for item in planned_records
        )
        legacy_execution_events = sum(
            item.to_dict().get("execution_status") != "observed_not_core_executed"
            for item in observed
        )
        audit["core_execution_event_count"] = core_execution_events
        audit["legacy_execution_event_count"] = legacy_execution_events
        audit["double_write_event_count"] = min(
            core_execution_events,
            legacy_execution_events,
        )
        payload = {
            "fixture_id": self.fixture.fixture_id,
            "bundle_id": self.fixture.bundle_id,
            "session_id": self.fixture.session_id,
            "replay_output_digest": replay.output_digest,
            "planned_actions": planned_records,
            "legacy_observed_actions": [item.to_dict() for item in observed],
            "diffs": list(diffs),
            "input_audit": audit,
        }
        output_digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        return ShadowReport(
            fixture_id=self.fixture.fixture_id,
            fixture_digest=replay.fixture_digest,
            bundle_id=self.fixture.bundle_id,
            bundle_digest=replay.bundle_digest,
            session_id=self.fixture.session_id,
            replay_output_digest=replay.output_digest,
            planned_actions=tuple(planned_records),
            legacy_observed_actions=tuple(item.to_dict() for item in observed),
            diffs=diffs,
            input_audit=audit,
            output_digest=output_digest,
            status=(
                "PASS"
                if audit["real_input_call_count"] == 0 and audit["double_write_event_count"] == 0
                else "FAIL"
            ),
        )

    shadow = run
