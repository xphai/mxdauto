"""Deterministic Frame Admission replay and evidence contracts.

This module executes a small, source-free fixture against the real capture
admission boundary.  It intentionally records only metadata and hashes; no
pixels are copied and no input-device calls are made.  Every scenario owns a
fresh adapter so a fatal latch in one scenario cannot hide another fault.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from maple_automation_core.capture import (
    FrameAdmissionResult,
    FrameAdmissionStatus,
    FrameSourceAdapter,
    FrameSourceConfig,
    RawFrame,
    canonical_calibration_sha256,
    canonical_geometry_sha256,
)
from maple_automation_core.domain.frame import FrameSize, SourceGeometry, SourceRect

FRAME_ADMISSION_SCHEMA_VERSION = "1.0.0"
FRAME_ADMISSION_REPORT_TYPE = "frame_admission"
FRAME_ADMISSION_REQUIRED_STATUSES = tuple(status.value for status in FrameAdmissionStatus)
FRAME_ADMISSION_REQUIRED_COVERAGE = (
    "accepted_boundary",
    "stale_recovery",
    "gap_accepted",
    "fatal_latch_duplicate",
    "fatal_latch_out_of_order",
    "fatal_latch_timestamp_regression",
    "fatal_latch_frame_size",
    "fatal_latch_identity",
    "fatal_latch_source_error",
    "latest_overwrite",
    "latest_fresh_expire",
    "reset_recovery",
)


class FrameAdmissionReplayError(ValueError):
    """Raised when a Frame Admission fixture is malformed."""


class FrameAdmissionDeterminismError(RuntimeError):
    """Raised when repeated execution produces different canonical output."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 digest of a canonical JSON value."""

    return sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise FrameAdmissionReplayError(f"{field_name} must be a non-empty string")
    return value


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FrameAdmissionReplayError(f"{field_name} must be an object")
    return value


def _list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise FrameAdmissionReplayError(f"{field_name} must be an array")
    return value


def _git_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = completed.stdout.strip()
    if completed.returncode != 0 or len(commit) != 40:
        raise FrameAdmissionReplayError("could not resolve git HEAD for evidence binding")
    return commit


def _default_geometry() -> SourceGeometry:
    """DEC-001 pilot geometry used when a fixture omits an explicit value."""

    return SourceGeometry(
        source_size=FrameSize(width=1920, height=1080),
        content_rect=SourceRect(x=277, y=167, width=1366, height=768),
        working_size=FrameSize(width=1296, height=700),
    )


@dataclass(frozen=True, slots=True)
class FrameAdmissionFixture:
    """Validated deterministic fixture description."""

    fixture_id: str
    config: FrameSourceConfig
    scenarios: tuple[Mapping[str, Any], ...]
    schema_version: str = FRAME_ADMISSION_SCHEMA_VERSION
    raw_payload: Mapping[str, Any] | None = None
    source_path: Path | None = None
    fixture_file_sha256: str = ""

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FrameAdmissionFixture:
        values = _mapping(payload, "fixture")
        schema_version = _non_empty_string(values.get("schema_version"), "schema_version")
        if schema_version != FRAME_ADMISSION_SCHEMA_VERSION:
            raise FrameAdmissionReplayError(
                f"unsupported Frame Admission fixture schema: {schema_version}"
            )
        fixture_id = _non_empty_string(values.get("fixture_id"), "fixture_id")
        config_value = _mapping(values.get("config"), "config")
        try:
            config = FrameSourceConfig.from_dict(config_value)
        except (TypeError, ValueError) as exc:
            raise FrameAdmissionReplayError(f"invalid fixture config: {exc}") from exc
        scenario_values = _list(values.get("scenarios"), "scenarios")
        if not scenario_values:
            raise FrameAdmissionReplayError("scenarios must contain at least one scenario")
        scenarios: list[Mapping[str, Any]] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(scenario_values):
            scenario = _mapping(item, f"scenarios[{index}]")
            scenario_id = _non_empty_string(scenario.get("scenario_id"), "scenario_id")
            if scenario_id in seen_ids:
                raise FrameAdmissionReplayError(f"duplicate scenario_id: {scenario_id}")
            seen_ids.add(scenario_id)
            operations = _list(scenario.get("operations"), f"scenarios[{index}].operations")
            if not operations:
                raise FrameAdmissionReplayError(f"scenario {scenario_id} has no operations")
            for op_index, operation_value in enumerate(operations):
                operation = _mapping(
                    operation_value,
                    f"scenarios[{index}].operations[{op_index}]",
                )
                _non_empty_string(operation.get("type"), "operation.type")
            tags = scenario.get("coverage_tags", [])
            if not isinstance(tags, list) or any(
                not isinstance(tag, str) or not tag for tag in tags
            ):
                raise FrameAdmissionReplayError(
                    f"scenario {scenario_id}.coverage_tags must contain strings"
                )
            # A shallow copy prevents callers from modifying the validated list while
            # keeping fixture data JSON-native and easy to inspect in diagnostics.
            scenarios.append(dict(scenario))
        return cls(
            fixture_id=fixture_id,
            config=config,
            scenarios=tuple(scenarios),
            schema_version=schema_version,
            raw_payload=dict(values),
        )

    @classmethod
    def from_path(cls, path: Path) -> FrameAdmissionFixture:
        resolved = path.resolve()
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FrameAdmissionReplayError(
                f"invalid Frame Admission fixture JSON: {resolved}"
            ) from exc
        fixture = cls.from_dict(_mapping(raw, "fixture"))
        return cls(
            fixture_id=fixture.fixture_id,
            config=fixture.config,
            scenarios=fixture.scenarios,
            schema_version=fixture.schema_version,
            raw_payload=fixture.raw_payload,
            source_path=resolved,
            fixture_file_sha256=_sha256_file(resolved),
        )

    def digest(self) -> str:
        """Digest the canonical fixture payload, independent of file formatting."""

        if self.raw_payload is None:
            payload: dict[str, Any] = {
                "schema_version": self.schema_version,
                "fixture_id": self.fixture_id,
                "config": self.config.to_dict(),
                "scenarios": list(self.scenarios),
            }
        else:
            payload = dict(self.raw_payload)
        return canonical_digest(payload)


class _ScriptedSource:
    """Minimal source adapter used to exercise ``poll`` and source exceptions."""

    def __init__(self) -> None:
        self._queue: list[RawFrame | None | BaseException] = []

    def push(self, value: RawFrame | None | BaseException) -> None:
        self._queue.append(value)

    def read(self) -> RawFrame | None:
        if not self._queue:
            return None
        value = self._queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _geometry_from_raw(value: Any, fallback: SourceGeometry) -> SourceGeometry:
    if value is None:
        return fallback
    try:
        return SourceGeometry.from_dict(_mapping(value, "frame.source_geometry"))
    except (TypeError, ValueError) as exc:
        raise FrameAdmissionReplayError(f"invalid frame source_geometry: {exc}") from exc


def _raw_from_operation(value: Any, config: FrameSourceConfig) -> RawFrame:
    data = _mapping(value, "operation.frame")
    geometry = _geometry_from_raw(
        data.get("source_geometry", data.get("geometry")),
        config.source_geometry,
    )
    source_size_value = data.get("source_size", data.get("frame_size"))
    if source_size_value is None:
        source_size = geometry.source_size
    else:
        try:
            source_size = FrameSize.from_dict(_mapping(source_size_value, "frame.source_size"))
        except (TypeError, ValueError) as exc:
            raise FrameAdmissionReplayError(f"invalid frame source_size: {exc}") from exc
    payload = dict(data)
    payload["source_geometry"] = geometry.to_dict()
    payload["source_size"] = source_size.to_dict()
    payload.setdefault("source_id", config.source_id)
    payload.setdefault("session_id", config.session_id)
    payload.setdefault("clock_domain", config.clock_domain)
    payload.setdefault("transform_version", config.transform_version)
    payload.setdefault("image_metadata", {"fixture": "g1-frame-admission"})
    try:
        return RawFrame.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise FrameAdmissionReplayError(f"invalid operation.frame: {exc}") from exc


def _exception_for_operation(value: Any) -> BaseException:
    exception_type = value if isinstance(value, str) else "RuntimeError"
    exception_classes: dict[str, type[Exception]] = {
        "RuntimeError": RuntimeError,
        "OSError": OSError,
        "ValueError": ValueError,
    }
    exception_class = exception_classes.get(exception_type, RuntimeError)
    return exception_class("synthetic frame source failure")


def _packet_digest(result: FrameAdmissionResult) -> str | None:
    if result.packet is None:
        return None
    return canonical_digest(result.packet.to_dict())


def _expected_failure(
    scenario_id: str,
    operation_index: int,
    message: str,
) -> str:
    return f"{scenario_id}[{operation_index}]: {message}"


def _observed_behavior_tags(run: FrameAdmissionRun, max_age_ns: int) -> set[str]:
    """Derive coverage from emitted evidence rather than fixture declarations."""

    observed: set[str] = set()
    by_scenario: dict[str, list[Mapping[str, Any]]] = {}
    for event in run.events:
        scenario_id = cast(str, event["scenario_id"])
        by_scenario.setdefault(scenario_id, []).append(event)
    observations_by_scenario: dict[str, list[Mapping[str, Any]]] = {}
    for observation in run.observations:
        scenario_id = cast(str, observation["scenario_id"])
        observations_by_scenario.setdefault(scenario_id, []).append(observation)

    for scenario_id, events in by_scenario.items():
        statuses = [cast(str, event["status"]) for event in events]
        if any(
            event["status"] == "accepted" and event["capture_age_ns"] == max_age_ns
            for event in events
        ):
            observed.add("accepted_boundary")
        if "stale" in statuses and "accepted" in statuses[statuses.index("stale") + 1 :]:
            observed.add("stale_recovery")
        if any(cast(Mapping[str, Any], event["event"])["gap_detected"] for event in events):
            observed.add("gap_accepted")
        if "duplicate" in statuses:
            observed.add("fatal_latch_duplicate")
        if "out_of_order" in statuses:
            observed.add("fatal_latch_out_of_order")
        if "timestamp_regression" in statuses:
            observed.add("fatal_latch_timestamp_regression")
        if "frame_size_changed" in statuses:
            observed.add("fatal_latch_frame_size")
        if {"source_mismatch", "session_mismatch", "clock_domain_mismatch"} & set(statuses):
            observed.add("fatal_latch_identity")
        if "source_error" in statuses:
            observed.add("fatal_latch_source_error")
        if any(
            cast(Mapping[str, Any], event["event"])["superseded_count"] > 0
            for event in events
            if event["status"] == "accepted"
        ):
            observed.add("latest_overwrite")

        scenario_observations = observations_by_scenario.get(scenario_id, [])
        latest_presence = [bool(item["latest_present"]) for item in scenario_observations]
        if True in latest_presence and False in latest_presence[latest_presence.index(True) + 1 :]:
            observed.add("latest_fresh_expire")
        reset_indexes = [
            cast(int, item["operation_index"])
            for item in scenario_observations
            if item["operation_type"] == "reset_session"
        ]
        if any(
            any(
                event["status"] == "accepted" and cast(int, event["operation_index"]) > reset_index
                for event in events
            )
            for reset_index in reset_indexes
        ):
            observed.add("reset_recovery")
    return observed


@dataclass(frozen=True, slots=True)
class FrameAdmissionRun:
    """One deterministic execution of every fixture scenario."""

    run_index: int
    scenarios: tuple[Mapping[str, Any], ...]
    events: tuple[Mapping[str, Any], ...]
    observations: tuple[Mapping[str, Any], ...]
    status_counts: Mapping[str, int]
    failures: tuple[str, ...]
    event_digest: str
    output_digest: str
    status: str

    @property
    def deterministic_payload(self) -> dict[str, Any]:
        return {
            "scenarios": list(self.scenarios),
            "events": list(self.events),
            "observations": list(self.observations),
            "status_counts": dict(self.status_counts),
            "failures": list(self.failures),
            "status": self.status,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_index": self.run_index,
            "scenario_count": len(self.scenarios),
            "event_count": len(self.events),
            "status": self.status,
            "event_digest": self.event_digest,
            "output_digest": self.output_digest,
            "status_counts": dict(self.status_counts),
            "scenarios": list(self.scenarios),
            "events": list(self.events),
            "observations": list(self.observations),
            "failures": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class FrameAdmissionReport:
    """Machine-readable G1-FRM-001A evidence report."""

    payload: Mapping[str, Any]

    @property
    def status(self) -> str:
        return cast(str, self.payload["status"])

    @property
    def deterministic(self) -> bool:
        return bool(self.payload["deterministic"])

    @property
    def repeat_count(self) -> int:
        return int(self.payload["repeat_count"])

    @property
    def report_digest(self) -> str:
        return cast(str, self.payload["report_digest"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)

    def to_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, indent=2, sort_keys=True)

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8", newline="\n")
        return path

    def assert_deterministic(self) -> None:
        if not self.deterministic:
            raise FrameAdmissionDeterminismError("Frame Admission repeated digests differ")


class FrameAdmissionReplayRunner:
    """Execute a validated Frame Admission fixture against the capture API."""

    def __init__(
        self,
        fixture: FrameAdmissionFixture | Mapping[str, Any] | Path,
        *,
        repo_root: Path | None = None,
        fixture_file_sha256: str | None = None,
    ) -> None:
        if isinstance(fixture, FrameAdmissionFixture):
            parsed = fixture
        elif isinstance(fixture, Path):
            parsed = FrameAdmissionFixture.from_path(fixture)
        elif isinstance(fixture, Mapping):
            parsed = FrameAdmissionFixture.from_dict(fixture)
        else:
            raise TypeError("fixture must be FrameAdmissionFixture, mapping, or Path")
        self.fixture = parsed
        inferred_root = (
            parsed.source_path.parent.parent.parent if parsed.source_path else Path.cwd()
        )
        self.repo_root = (repo_root or inferred_root).resolve()
        supplied_digest = fixture_file_sha256 or parsed.fixture_file_sha256
        self.fixture_file_sha256 = supplied_digest or parsed.digest()
        if len(self.fixture_file_sha256) != 64:
            raise FrameAdmissionReplayError("fixture_file_sha256 must be a SHA-256 hex digest")

    def _new_adapter(self) -> tuple[_ScriptedSource, FrameSourceAdapter]:
        source = _ScriptedSource()
        adapter = FrameSourceAdapter(
            source,
            self.fixture.config,
            clock=lambda: 0,
        )
        return source, adapter

    def _check_event_expectations(
        self,
        scenario_id: str,
        operation_index: int,
        operation: Mapping[str, Any],
        result: FrameAdmissionResult,
    ) -> list[str]:
        failures: list[str] = []
        expected_status = operation.get("expect_status")
        if expected_status is not None:
            if not isinstance(expected_status, str):
                failures.append(
                    _expected_failure(scenario_id, operation_index, "expect_status is not a string")
                )
            elif result.status.value != expected_status:
                failures.append(
                    _expected_failure(
                        scenario_id,
                        operation_index,
                        f"expected status {expected_status}, observed {result.status.value}",
                    )
                )
        for key, observed_bool in (
            ("expect_fault_latched", result.fault_latched),
            ("expect_plan_suppressed", result.plan_suppressed),
            ("expect_gap_detected", result.gap_detected),
        ):
            expected = operation.get(key)
            if expected is not None:
                if type(expected) is not bool:
                    failures.append(
                        _expected_failure(scenario_id, operation_index, f"{key} is not boolean")
                    )
                elif observed_bool != expected:
                    failures.append(
                        _expected_failure(
                            scenario_id,
                            operation_index,
                            f"expected {key}={expected}, observed {observed_bool}",
                        )
                    )
        for key, observed_value in (
            ("expect_frame_id", result.event.frame_id),
            ("expect_missing_frame_count", result.event.missing_frame_count),
            ("expect_superseded_count", result.event.superseded_count),
        ):
            expected = operation.get(key)
            if expected is not None and expected != observed_value:
                failures.append(
                    _expected_failure(
                        scenario_id,
                        operation_index,
                        f"expected {key}={expected}, observed {observed_value}",
                    )
                )
        return failures

    def _check_observation_expectations(
        self,
        scenario_id: str,
        operation_index: int,
        operation: Mapping[str, Any],
        *,
        latest_frame_id: int | None,
        latest_present: bool,
        superseded_count: int,
        session_id: str,
    ) -> list[str]:
        failures: list[str] = []
        expected_latest = operation.get("expect_latest_frame_id")
        if expected_latest is not None and expected_latest != latest_frame_id:
            failures.append(
                _expected_failure(
                    scenario_id,
                    operation_index,
                    f"expected latest frame {expected_latest}, observed {latest_frame_id}",
                )
            )
        expected_present = operation.get("expect_latest_present")
        if expected_present is not None:
            if type(expected_present) is not bool:
                failures.append(
                    _expected_failure(
                        scenario_id, operation_index, "expect_latest_present is not boolean"
                    )
                )
            elif expected_present != latest_present:
                failures.append(
                    _expected_failure(
                        scenario_id,
                        operation_index,
                        f"expected latest present={expected_present}, observed {latest_present}",
                    )
                )
        expected_superseded = operation.get("expect_buffer_superseded_count")
        if expected_superseded is not None and expected_superseded != superseded_count:
            failures.append(
                _expected_failure(
                    scenario_id,
                    operation_index,
                    "expected buffer superseded_count="
                    f"{expected_superseded}, observed {superseded_count}",
                )
            )
        expected_session = operation.get("expect_session_id")
        if expected_session is not None and expected_session != session_id:
            failures.append(
                _expected_failure(
                    scenario_id,
                    operation_index,
                    f"expected session {expected_session}, observed {session_id}",
                )
            )
        return failures

    def run(self, run_index: int = 1) -> FrameAdmissionRun:
        """Run all scenarios once; expected mismatches are recorded as failures."""

        if run_index < 1:
            raise ValueError("run_index must be positive")
        events: list[Mapping[str, Any]] = []
        observations: list[Mapping[str, Any]] = []
        failures: list[str] = []
        counts: Counter[str] = Counter()
        scenario_summaries: list[Mapping[str, Any]] = []

        for scenario_value in self.fixture.scenarios:
            scenario_id = _non_empty_string(scenario_value.get("scenario_id"), "scenario_id")
            tags = tuple(cast(list[str], scenario_value.get("coverage_tags", [])))
            source, adapter = self._new_adapter()
            scenario_event_start = len(events)
            scenario_failure_start = len(failures)
            operations = cast(list[Any], scenario_value["operations"])
            for operation_index, operation_value in enumerate(operations):
                operation = _mapping(
                    operation_value, f"{scenario_id}.operations[{operation_index}]"
                )
                operation_type = _non_empty_string(operation.get("type"), "operation.type")
                if operation_type == "reset_session":
                    session_value = operation.get("session_id")
                    if session_value is None:
                        raise FrameAdmissionReplayError(
                            f"{scenario_id}[{operation_index}] reset_session requires session_id"
                        )
                    adapter.reset_session(_non_empty_string(session_value, "operation.session_id"))
                    latest = adapter.read_latest(operation.get("now_ns", 0))
                    latest_id = None if latest is None else latest.frame_id
                    observations.append(
                        {
                            "scenario_id": scenario_id,
                            "operation_index": operation_index,
                            "operation_type": operation_type,
                            "latest_frame_id": latest_id,
                            "latest_present": latest is not None,
                            "buffer_superseded_count": adapter.superseded_count,
                            "session_id": adapter.session_id,
                        }
                    )
                    failures.extend(
                        self._check_observation_expectations(
                            scenario_id,
                            operation_index,
                            operation,
                            latest_frame_id=latest_id,
                            latest_present=latest is not None,
                            superseded_count=adapter.superseded_count,
                            session_id=adapter.session_id,
                        )
                    )
                    continue

                if operation_type in {"read_latest", "latest"}:
                    now_value = operation.get("now_ns", 0)
                    if (
                        not isinstance(now_value, int)
                        or isinstance(now_value, bool)
                        or now_value < 0
                    ):
                        raise FrameAdmissionReplayError(
                            f"{scenario_id}[{operation_index}] now_ns must be a "
                            "non-negative integer"
                        )
                    latest = adapter.read_latest(now_value)
                    latest_id = None if latest is None else latest.frame_id
                    observations.append(
                        {
                            "scenario_id": scenario_id,
                            "operation_index": operation_index,
                            "operation_type": operation_type,
                            "latest_frame_id": latest_id,
                            "latest_present": latest is not None,
                            "buffer_superseded_count": adapter.superseded_count,
                            "session_id": adapter.session_id,
                        }
                    )
                    failures.extend(
                        self._check_observation_expectations(
                            scenario_id,
                            operation_index,
                            operation,
                            latest_frame_id=latest_id,
                            latest_present=latest is not None,
                            superseded_count=adapter.superseded_count,
                            session_id=adapter.session_id,
                        )
                    )
                    continue

                now_value = operation.get("now_ns")
                if not isinstance(now_value, int) or isinstance(now_value, bool) or now_value < 0:
                    raise FrameAdmissionReplayError(
                        f"{scenario_id}[{operation_index}] now_ns must be a non-negative integer"
                    )
                input_captured_at_ns: int | None = None
                if operation_type in {"ingest", "admit"}:
                    raw_value = operation.get("frame")
                    if raw_value is None:
                        result = adapter.ingest(None, received_at_ns=now_value)
                    else:
                        raw = _raw_from_operation(raw_value, self.fixture.config)
                        input_captured_at_ns = raw.captured_at_ns
                        result = adapter.ingest(raw, received_at_ns=now_value)
                elif operation_type == "no_frame":
                    result = adapter.ingest(None, received_at_ns=now_value)
                elif operation_type in {"poll", "source_error"}:
                    if operation_type == "source_error":
                        source.push(_exception_for_operation(operation.get("exception_type")))
                    else:
                        frame_value = operation.get("frame")
                        if frame_value is None:
                            source.push(None)
                        else:
                            raw = _raw_from_operation(frame_value, self.fixture.config)
                            input_captured_at_ns = raw.captured_at_ns
                            source.push(raw)
                    result = adapter.poll(now_value)
                else:
                    raise FrameAdmissionReplayError(
                        f"{scenario_id}[{operation_index}] unsupported operation type: "
                        f"{operation_type}"
                    )

                counts[result.status.value] += 1
                event_payload: dict[str, Any] = {
                    "scenario_id": scenario_id,
                    "operation_index": operation_index,
                    "operation_type": operation_type,
                    "status": result.status.value,
                    "event": result.event.to_dict(),
                    "packet_frame_id": None if result.packet is None else result.packet.frame_id,
                    "packet_digest": _packet_digest(result),
                    "input_captured_at_ns": input_captured_at_ns,
                    "capture_age_ns": (
                        None
                        if input_captured_at_ns is None or input_captured_at_ns > now_value
                        else now_value - input_captured_at_ns
                    ),
                }
                events.append(event_payload)
                failures.extend(
                    self._check_event_expectations(scenario_id, operation_index, operation, result)
                )

            scenario_summaries.append(
                {
                    "scenario_id": scenario_id,
                    "coverage_tags": list(tags),
                    "event_count": len(events) - scenario_event_start,
                    "failure_count": len(failures) - scenario_failure_start,
                    "status": "PASS" if len(failures) == scenario_failure_start else "FAIL",
                }
            )

        deterministic_payload = {
            "scenarios": scenario_summaries,
            "events": events,
            "observations": observations,
            "status_counts": {
                status: counts.get(status, 0) for status in FRAME_ADMISSION_REQUIRED_STATUSES
            },
            "failures": failures,
            "status": "PASS" if not failures else "FAIL",
        }
        digest = canonical_digest(deterministic_payload)
        return FrameAdmissionRun(
            run_index=run_index,
            scenarios=tuple(scenario_summaries),
            events=tuple(events),
            observations=tuple(observations),
            status_counts={
                status: counts.get(status, 0) for status in FRAME_ADMISSION_REQUIRED_STATUSES
            },
            failures=tuple(failures),
            event_digest=canonical_digest({"events": events}),
            output_digest=digest,
            status="PASS" if not failures else "FAIL",
        )

    def run_repeated(
        self,
        repetitions: int = 3,
        *,
        source_commit: str | None = None,
        generated_at: str | None = None,
    ) -> FrameAdmissionReport:
        """Execute the fixture repeatedly and build a self-binding report."""

        if repetitions < 3:
            raise ValueError("repetitions must be at least 3 for deterministic evidence")
        commit = source_commit or _git_head(self.repo_root)
        if len(commit) != 40 or any(
            character not in "0123456789abcdefABCDEF" for character in commit
        ):
            raise FrameAdmissionReplayError("source_commit must be a 40-character git SHA-1")
        runs = tuple(self.run(index) for index in range(1, repetitions + 1))
        event_digests = [run.event_digest for run in runs]
        output_digests = [run.output_digest for run in runs]
        deterministic = len(set(event_digests)) == 1 and len(set(output_digests)) == 1
        all_passed = all(run.status == "PASS" for run in runs)
        counts: Counter[str] = Counter()
        for run in runs:
            counts.update(run.status_counts)
        status_counts = {
            status: counts.get(status, 0) // repetitions
            for status in FRAME_ADMISSION_REQUIRED_STATUSES
        }
        observed_tags = sorted(_observed_behavior_tags(runs[0], self.fixture.config.max_age_ns))
        behavior_coverage = {
            "required_tags": list(FRAME_ADMISSION_REQUIRED_COVERAGE),
            "observed_tags": observed_tags,
            "complete": set(FRAME_ADMISSION_REQUIRED_COVERAGE).issubset(observed_tags),
        }
        all_required_statuses_observed = all(
            status_counts[status] > 0 for status in FRAME_ADMISSION_REQUIRED_STATUSES
        )
        payload: dict[str, Any] = {
            "schema_version": FRAME_ADMISSION_SCHEMA_VERSION,
            "report_type": FRAME_ADMISSION_REPORT_TYPE,
            "report_id": f"frame-admission-{self.fixture.fixture_id}-{commit[:12]}",
            "status": "PASS"
            if (
                all_passed
                and deterministic
                and all_required_statuses_observed
                and behavior_coverage["complete"]
            )
            else "FAIL",
            "source_commit": commit,
            "fixture_id": self.fixture.fixture_id,
            "fixture_file_sha256": self.fixture_file_sha256,
            "fixture_canonical_sha256": self.fixture.digest(),
            "geometry_hash": canonical_geometry_sha256(self.fixture.config.source_geometry),
            "calibration_hash": canonical_calibration_sha256(
                self.fixture.config.source_geometry,
                self.fixture.config.transform_version,
            ),
            "deterministic": deterministic,
            "repeat_count": repetitions,
            "runs": [
                {
                    **run.to_dict(),
                    "run_digest": run.output_digest,
                }
                for run in runs
            ],
            "status_coverage": {
                "required_statuses": list(FRAME_ADMISSION_REQUIRED_STATUSES),
                "counts": status_counts,
                "observed_statuses": sorted(
                    status for status, count in status_counts.items() if count > 0
                ),
                "all_required_statuses_observed": all_required_statuses_observed,
            },
            "behavior_coverage": behavior_coverage,
            "input_audit": {
                "real_input_call_count": 0,
                "core_v2_real_input_call_count": 0,
                "double_write_event_count": 0,
                "connected": False,
                "input_owner": "legacy",
            },
            "execution_mode": "offline",
            "generated_at": generated_at or _timestamp(),
        }
        digest_payload = dict(payload)
        digest_payload.pop("report_digest", None)
        digest_payload.pop("generated_at", None)
        payload["report_digest"] = canonical_digest(digest_payload)
        return FrameAdmissionReport(payload=payload)


# Short aliases mirror the naming used by the other replay modules.
FrameAdmissionRunner = FrameAdmissionReplayRunner


def verify_frame_admission_report(
    payload: Mapping[str, Any],
    fixture: FrameAdmissionFixture,
) -> None:
    """Fail closed when a report's internal evidence graph is inconsistent.

    JSON Schema validates physical shape; this verifier recomputes every
    digest, count, repetition invariant, derived coverage flag and PASS result
    against the referenced fixture.
    """

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise FrameAdmissionReplayError(message)

    runs_value = payload.get("runs")
    require(isinstance(runs_value, list), "report runs must be an array")
    runs = cast(list[Any], runs_value)
    repeat_count = payload.get("repeat_count")
    require(
        type(repeat_count) is int and repeat_count >= 3,
        "report repeat_count must be at least 3",
    )
    require(repeat_count == len(runs), "report repeat_count must equal run count")

    expected_file_sha = fixture.fixture_file_sha256 or fixture.digest()
    require(payload.get("fixture_id") == fixture.fixture_id, "report fixture_id mismatch")
    require(
        payload.get("fixture_file_sha256") == expected_file_sha,
        "report fixture file SHA-256 mismatch",
    )
    require(
        payload.get("fixture_canonical_sha256") == fixture.digest(),
        "report fixture canonical SHA-256 mismatch",
    )
    require(
        payload.get("geometry_hash") == canonical_geometry_sha256(fixture.config.source_geometry),
        "report geometry hash mismatch",
    )
    require(
        payload.get("calibration_hash")
        == canonical_calibration_sha256(
            fixture.config.source_geometry,
            fixture.config.transform_version,
        ),
        "report calibration hash mismatch",
    )

    run_objects: list[FrameAdmissionRun] = []
    expected_runner = FrameAdmissionReplayRunner(fixture)
    for expected_index, run_value in enumerate(runs, start=1):
        run = _mapping(run_value, f"runs[{expected_index - 1}]")
        expected_run = expected_runner.run(expected_index).to_dict()
        for field_name in (
            "run_index",
            "scenario_count",
            "event_count",
            "status",
            "status_counts",
            "scenarios",
            "events",
            "observations",
            "failures",
        ):
            require(
                run.get(field_name) == expected_run[field_name],
                f"run {expected_index} {field_name} differs from fixture replay",
            )
        require(run.get("run_index") == expected_index, "report run indexes are not sequential")
        scenarios = _list(run.get("scenarios"), "run.scenarios")
        events = _list(run.get("events"), "run.events")
        observations = _list(run.get("observations"), "run.observations")
        failures = _list(run.get("failures"), "run.failures")
        status = run.get("status")
        require(status in {"PASS", "FAIL"}, "run status is invalid")
        require(run.get("scenario_count") == len(scenarios), "run scenario_count mismatch")
        require(run.get("event_count") == len(events), "run event_count mismatch")

        observed_counts = {status_name: 0 for status_name in FRAME_ADMISSION_REQUIRED_STATUSES}
        event_mappings: list[Mapping[str, Any]] = []
        for event_index, event_value in enumerate(events):
            event = _mapping(event_value, f"run.events[{event_index}]")
            event_status = event.get("status")
            require(
                isinstance(event_status, str) and event_status in observed_counts,
                "run event contains an unknown status",
            )
            observed_counts[cast(str, event_status)] += 1
            event_mappings.append(event)
        require(run.get("status_counts") == observed_counts, "run status_counts mismatch")

        failure_strings = [_non_empty_string(value, "run failure") for value in failures]
        scenario_mappings: list[Mapping[str, Any]] = []
        seen_scenarios: set[str] = set()
        fixture_scenarios = {cast(str, item["scenario_id"]): item for item in fixture.scenarios}
        for scenario_index, scenario_value in enumerate(scenarios):
            scenario = _mapping(scenario_value, f"run.scenarios[{scenario_index}]")
            scenario_id = _non_empty_string(scenario.get("scenario_id"), "scenario_id")
            require(scenario_id not in seen_scenarios, "run contains duplicate scenario_id")
            fixture_scenario = fixture_scenarios.get(scenario_id)
            require(fixture_scenario is not None, "run contains an unknown scenario_id")
            seen_scenarios.add(scenario_id)
            actual_event_count = sum(
                event.get("scenario_id") == scenario_id for event in event_mappings
            )
            actual_failure_count = sum(
                failure.startswith(f"{scenario_id}[") for failure in failure_strings
            )
            require(
                scenario.get("event_count") == actual_event_count,
                f"scenario {scenario_id} event_count mismatch",
            )
            require(
                scenario.get("failure_count") == actual_failure_count,
                f"scenario {scenario_id} failure_count mismatch",
            )
            expected_status = "PASS" if actual_failure_count == 0 else "FAIL"
            require(
                scenario.get("status") == expected_status,
                f"scenario {scenario_id} status mismatch",
            )
            require(
                scenario.get("coverage_tags")
                == cast(Mapping[str, Any], fixture_scenario).get("coverage_tags", []),
                f"scenario {scenario_id} coverage tags mismatch",
            )
            scenario_mappings.append(scenario)
        require(
            seen_scenarios == set(fixture_scenarios),
            "run scenario set does not match fixture",
        )
        require(
            sum(
                failure.startswith(f"{scenario_id}[")
                for failure in failure_strings
                for scenario_id in seen_scenarios
            )
            == len(failure_strings),
            "run contains a failure not bound to one scenario",
        )
        require(
            all(event.get("scenario_id") in seen_scenarios for event in event_mappings),
            "run contains an event bound to an unknown scenario",
        )
        observation_mappings = [_mapping(item, "run observation") for item in observations]
        require(
            all(
                observation.get("scenario_id") in seen_scenarios
                for observation in observation_mappings
            ),
            "run contains an observation bound to an unknown scenario",
        )

        expected_run_status = "PASS" if not failure_strings else "FAIL"
        require(status == expected_run_status, "run PASS/FAIL contradicts failures")
        expected_event_digest = canonical_digest({"events": events})
        require(run.get("event_digest") == expected_event_digest, "run event_digest mismatch")
        deterministic_payload = {
            "scenarios": scenarios,
            "events": events,
            "observations": observations,
            "status_counts": observed_counts,
            "failures": failure_strings,
            "status": status,
        }
        expected_output_digest = canonical_digest(deterministic_payload)
        require(run.get("output_digest") == expected_output_digest, "run output_digest mismatch")
        require(run.get("run_digest") == expected_output_digest, "run run_digest mismatch")
        run_objects.append(
            FrameAdmissionRun(
                run_index=expected_index,
                scenarios=tuple(scenario_mappings),
                events=tuple(event_mappings),
                observations=tuple(observation_mappings),
                status_counts=observed_counts,
                failures=tuple(failure_strings),
                event_digest=expected_event_digest,
                output_digest=expected_output_digest,
                status=cast(str, status),
            )
        )

    event_digests = {run.event_digest for run in run_objects}
    output_digests = {run.output_digest for run in run_objects}
    deterministic = len(event_digests) == 1 and len(output_digests) == 1
    require(payload.get("deterministic") is deterministic, "report deterministic flag mismatch")

    first_run = run_objects[0]
    require(
        all(run.status_counts == first_run.status_counts for run in run_objects),
        "repeated run status counts differ",
    )
    status_coverage = _mapping(payload.get("status_coverage"), "status_coverage")
    require(
        status_coverage.get("required_statuses") == list(FRAME_ADMISSION_REQUIRED_STATUSES),
        "required status list mismatch",
    )
    require(status_coverage.get("counts") == first_run.status_counts, "status coverage mismatch")
    observed_statuses = sorted(
        status for status, count in first_run.status_counts.items() if count > 0
    )
    require(
        status_coverage.get("observed_statuses") == observed_statuses,
        "observed status list mismatch",
    )
    all_statuses = all(
        first_run.status_counts[status] > 0 for status in FRAME_ADMISSION_REQUIRED_STATUSES
    )
    require(
        status_coverage.get("all_required_statuses_observed") is all_statuses,
        "required status coverage flag mismatch",
    )

    behavior_coverage = _mapping(payload.get("behavior_coverage"), "behavior_coverage")
    observed_tags = sorted(_observed_behavior_tags(first_run, fixture.config.max_age_ns))
    require(
        behavior_coverage.get("required_tags") == list(FRAME_ADMISSION_REQUIRED_COVERAGE),
        "required behavior tag list mismatch",
    )
    require(behavior_coverage.get("observed_tags") == observed_tags, "behavior coverage mismatch")
    behavior_complete = set(FRAME_ADMISSION_REQUIRED_COVERAGE).issubset(observed_tags)
    require(
        behavior_coverage.get("complete") is behavior_complete,
        "behavior coverage flag mismatch",
    )

    expected_pass = (
        all(run.status == "PASS" for run in run_objects)
        and deterministic
        and all_statuses
        and behavior_complete
    )
    require(
        payload.get("status") == ("PASS" if expected_pass else "FAIL"),
        "report PASS/FAIL contradicts derived evidence",
    )
    digest_payload = dict(payload)
    declared_digest = digest_payload.pop("report_digest", None)
    digest_payload.pop("generated_at", None)
    require(
        declared_digest == canonical_digest(digest_payload),
        "report_digest does not match canonical payload",
    )


__all__ = [
    "FRAME_ADMISSION_REPORT_TYPE",
    "FRAME_ADMISSION_REQUIRED_COVERAGE",
    "FRAME_ADMISSION_REQUIRED_STATUSES",
    "FRAME_ADMISSION_SCHEMA_VERSION",
    "FrameAdmissionDeterminismError",
    "FrameAdmissionFixture",
    "FrameAdmissionReplayError",
    "FrameAdmissionReplayRunner",
    "FrameAdmissionReport",
    "FrameAdmissionRun",
    "FrameAdmissionRunner",
    "canonical_digest",
    "verify_frame_admission_report",
]
