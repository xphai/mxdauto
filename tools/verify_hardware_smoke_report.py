"""Verify VC-003 hardware smoke evidence without trusting report assertions.

The report is deliberately a data-only boundary.  ``status`` and the two
human-facing FPS values are treated as untrusted observations: this module
recomputes the monotonic window, exact integer rate comparisons, raw-slot
accounting, freshness and all zero-failure gates before accepting a report.
No capture device, network endpoint, keyboard or mouse is opened by this
tool.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

try:
    from .bundle_common import read_json, safe_relative_path, sha256_file
except ImportError:  # pragma: no cover - direct script execution
    from bundle_common import (  # type: ignore[import-not-found,no-redef]
        read_json,
        safe_relative_path,
        sha256_file,
    )


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "vc003-hardware-smoke-report.schema.json"
SCHEMA_VERSION = "1.0.0"
REPORT_TYPE = "vc003_hardware_smoke"
MAX_AGE_NS = 250_000_000
MIN_WINDOW_NS = 300_000_000_000
MIN_CAPTURE_FRAMES = 9_000
MIN_ADMITTED_FRAMES = 4_500
MIN_CAPTURE_RATE_NUM = 30
MIN_ADMISSION_RATE_NUM = 15
STOP_DEADLINE_SECONDS = Decimal("2.0")
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[A-Fa-f0-9]{40}$")

# All counters in this table represent a hard zero-failure gate.  Keeping the
# list explicit prevents a benign diagnostic counter from accidentally being
# interpreted as a source failure.
ZERO_FAILURE_FIELDS = (
    "source_read_failures",
    "decode_failures",
    "copy_failures",
    "hash_failures",
    "fatal_errors",
    "reconnects",
    "backend_fallbacks",
    "stale_frames",
    "duplicate_sequences",
    "out_of_order_sequences",
    "timestamp_regressions",
    "source_mismatches",
    "session_mismatches",
    "clock_mismatches",
    "size_mismatches",
    "geometry_mismatches",
    "admission_failures",
    "cas_mismatches",
    "event_tape_orphans",
    "event_tape_mismatches",
    "frame_ledger_orphans",
    "pixel_orphans",
    "provenance_failures",
    "privacy_failures",
    "cleanup_failures",
)


class HardwareSmokeVerificationError(ValueError):
    """Raised by :func:`assert_valid` for a malformed or failed report."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_report_digest(payload: Mapping[str, Any]) -> str:
    """Return the SHA-256 of canonical report JSON excluding ``report_digest``."""

    body = dict(payload)
    body.pop("report_digest", None)
    # The provenance/audit schemas use this descriptive spelling.  Excluding
    # both known self fields keeps re-signing deterministic across report
    # producers while still rejecting unknown fields at the strict schema.
    body.pop("canonical_report_sha256", None)
    return sha256(_canonical_json(body)).hexdigest()


# Names used by callers that share the report-binding helper with G0 tools.
_canonical_digest = canonical_report_digest
compute_report_digest = canonical_report_digest
canonical_digest = canonical_report_digest


def _mapping(value: object, field: str) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return None


def _value(payload: Mapping[str, Any], *path: str) -> object:
    current: object = payload
    for name in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(name)
    return current


def _strict_int(value: object) -> bool:
    return type(value) is int and cast(int, value) >= 0


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return result


def _schema_errors(payload: Mapping[str, Any], schema_path: Path) -> list[str]:
    try:
        schema = read_json(schema_path)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    except (OSError, ValueError, TypeError) as exc:
        return [f"schema could not be loaded: {exc}"]
    return [f"schema: {error.json_path}: {error.message}" for error in errors]


def _unsafe_relative(root: Path, relative: object, field: str) -> tuple[Path | None, str | None]:
    """Resolve a repository-relative path while rejecting traversal/symlinks."""

    if not isinstance(relative, str):
        return None, f"{field} must be a relative path"
    try:
        normalized = safe_relative_path(relative)
    except ValueError as exc:
        return None, f"{field} unsafe path: {exc}"
    root_input = root.expanduser()
    if root_input.is_symlink():
        return None, f"{field} root must not be a symlink"
    root_resolved = root_input.resolve()
    candidate = root_resolved.joinpath(*normalized.split("/"))
    # Check every component, including an existing directory between root and
    # the leaf.  resolve() alone would follow a symlink before we could audit
    # it.
    try:
        relative_parts = candidate.relative_to(root_resolved).parts
    except ValueError:
        return None, f"{field} escaped root"
    current = root_resolved
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            return None, f"{field} must not traverse a symlink: {normalized}"
    return candidate, None


def _check_local_artifact(
    artifact: Mapping[str, Any],
    *,
    root: Path | None,
    external_roots: Mapping[str, Path],
    metadata_only: bool,
    errors: list[str],
    prefix: str,
) -> None:
    path_value = artifact.get("path")
    if not isinstance(path_value, str):
        errors.append(f"{prefix} path must be a string")
        return
    if path_value.startswith("external://"):
        remainder = path_value[len("external://") :]
        root_name, separator, relative = remainder.partition("/")
        if not separator or not root_name:
            errors.append(f"{prefix} has malformed external locator")
            return
        try:
            safe_relative_path(relative)
        except ValueError as exc:
            errors.append(f"{prefix} unsafe external locator: {exc}")
            return
        if metadata_only:
            return
        if not separator or root_name not in external_roots:
            errors.append(f"{prefix} has no controlled external root")
            return
        external_root = external_roots[root_name]
        external_path, path_error = _unsafe_relative(external_root, relative, prefix)
        if path_error:
            errors.append(path_error)
            return
        assert external_path is not None
        if not external_path.is_file():
            errors.append(f"{prefix} is missing: {path_value}")
            return
        try:
            actual_size = external_path.stat().st_size
            actual_hash = sha256_file(external_path)
        except OSError as exc:
            errors.append(f"{prefix} cannot be read: {exc}")
            return
        if artifact.get("size_bytes") != actual_size:
            errors.append(
                f"{prefix} size mismatch: expected {artifact.get('size_bytes')}, got {actual_size}"
            )
        if str(artifact.get("sha256", "")).lower() != actual_hash:
            errors.append(f"{prefix} hash mismatch")
        return
    if root is None:
        errors.append(f"{prefix} local artifact requires --repo-root")
        return
    path, path_error = _unsafe_relative(root, path_value, prefix)
    if path_error:
        errors.append(path_error)
        return
    assert path is not None
    if not path.is_file():
        errors.append(f"{prefix} is missing: {path_value}")
        return
    try:
        actual_size = path.stat().st_size
        actual_hash = sha256_file(path)
    except OSError as exc:
        errors.append(f"{prefix} cannot be read: {exc}")
        return
    if artifact.get("size_bytes") != actual_size:
        errors.append(
            f"{prefix} size mismatch: expected {artifact.get('size_bytes')}, got {actual_size}"
        )
    if str(artifact.get("sha256", "")).lower() != actual_hash:
        errors.append(f"{prefix} hash mismatch")


def _require_zero_counts(report: Mapping[str, Any], errors: list[str]) -> None:
    counts = _mapping(report.get("failure_counts"), "failure_counts")
    if counts is None:
        errors.append("failure_counts must be an object")
        return
    for field in ZERO_FAILURE_FIELDS:
        value = counts.get(field)
        if not _strict_int(value):
            errors.append(f"failure_counts.{field} must be a non-negative integer")
        elif cast(int, value) != 0:
            errors.append(f"failure_counts.{field} must be zero (got {value})")


def _validate_binding_fields(report: Mapping[str, Any], errors: list[str]) -> None:
    for field in ("source_commit",):
        value = report.get(field)
        if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
            errors.append(f"{field} must be a 40-character git commit")
    for field in (
        "tool_artifact_sha256",
        "wheel_sha256",
        "dependency_lock_sha256",
        "config_sha256",
        "calibration_sha256",
    ):
        value = report.get(field)
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            errors.append(f"{field} must be a SHA-256 digest")
    ci_run = _mapping(report.get("ci_run"), "ci_run")
    if ci_run is None or ci_run.get("status") != "success":
        errors.append("ci_run.status must be success for a bound B1 artifact")


def _format_errors(report: Mapping[str, Any], errors: list[str]) -> None:
    source = _mapping(report.get("source"), "source")
    if source is None:
        errors.append("source must be an object")
        return
    queue_values = [source.get("upstream_queue_depth")]
    if "upstream_queue" in source:
        queue_values.append(source.get("upstream_queue"))
    if any(value != "unknown" for value in queue_values):
        errors.append("upstream_queue_depth must be exactly 'unknown'")
    if source.get("timestamp_origin") != "host_monotonic_post_retrieve":
        errors.append("timestamp_origin must be host_monotonic_post_retrieve")
    for field, expected in (
        ("logical_source_id", "capture-card-primary"),
        ("selector", "VC-003 Video"),
    ):
        if source.get(field) != expected:
            errors.append(f"source.{field} must be {expected!r}")
    fingerprint = source.get("device_fingerprint_sha256")
    unknown_fingerprint = sha256(b"unknown").hexdigest()
    if (
        not isinstance(fingerprint, str)
        or SHA256_RE.fullmatch(fingerprint) is None
        or fingerprint == unknown_fingerprint
        or fingerprint == "0" * 64
    ):
        errors.append("source.device_fingerprint_sha256 must be a measured anonymous identity")

    requested = _mapping(source.get("requested"), "source.requested")
    negotiated = _mapping(source.get("negotiated"), "source.negotiated")
    for name, value in (("requested", requested), ("negotiated", negotiated)):
        if value is None:
            errors.append(f"source.{name} must be an object")
            continue
        if value.get("width") != 1920 or value.get("height") != 1080:
            errors.append(f"source.{name} must be 1920x1080")
        try:
            fps = _decimal(value.get("fps"), f"source.{name}.fps")
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if fps != Decimal("30"):
                if fps == Decimal("29.97"):
                    errors.append("29.97 FPS is not acceptable; the report must be HOLD/FAIL")
                else:
                    errors.append(f"source.{name}.fps must be exactly 30.0 (got {fps})")

    if negotiated is not None:
        canonical = {
            "channels": negotiated.get("channels"),
            "pixel_format": negotiated.get("pixel_format"),
            "dtype": negotiated.get("dtype"),
            "stride": negotiated.get("stride"),
            "length": negotiated.get("length"),
        }
        expected = {
            "channels": 3,
            "pixel_format": "BGR8",
            "dtype": "uint8",
            "stride": 5760,
            "length": 6_220_800,
        }
        if canonical != expected:
            errors.append(
                "negotiated pixels must be canonical 1920x1080 BGR8/HWC/stride=5760/length=6220800"
            )


def _window_errors(report: Mapping[str, Any], errors: list[str]) -> int | None:
    window = _mapping(report.get("measurement_window"), "measurement_window")
    if window is None:
        errors.append("measurement_window must be an object")
        return None
    start = window.get("measurement_start_ns")
    end = window.get("measurement_end_ns")
    if not _strict_int(start) or not _strict_int(end):
        errors.append("measurement window markers must be non-negative integers")
        return None
    duration = cast(int, end) - cast(int, start)
    if duration <= 0:
        errors.append("measurement window must have a positive duration")
    if duration < MIN_WINDOW_NS:
        errors.append(
            "measured duration is below 300.000 seconds "
            f"(recomputed {duration / 1_000_000_000:.9f})"
        )
    if window.get("continuous") is not True:
        errors.append("measurement window must be continuous")
    try:
        warmup = _decimal(window.get("warmup_seconds"), "warmup_seconds")
        if warmup < Decimal("30"):
            errors.append("warmup_seconds must be at least 30")
    except ValueError as exc:
        errors.append(str(exc))
    try:
        displayed = _decimal(window.get("measured_seconds"), "measured_seconds")
        recomputed = Decimal(duration) / Decimal(1_000_000_000)
        if displayed != recomputed:
            errors.append(
                f"measured_seconds does not match monotonic markers (expected {recomputed})"
            )
    except ValueError as exc:
        errors.append(str(exc))
    return duration


def _metric_errors(report: Mapping[str, Any], duration_ns: int | None, errors: list[str]) -> None:
    metrics = _mapping(report.get("metrics"), "metrics")
    if metrics is None:
        errors.append("metrics must be an object")
        return
    successful = metrics.get("successful_frames")
    admitted = metrics.get("admitted_frames")
    for name, value in (("successful_frames", successful), ("admitted_frames", admitted)):
        if not _strict_int(value):
            errors.append(f"metrics.{name} must be a non-negative integer")
    if _strict_int(successful) and cast(int, successful) < MIN_CAPTURE_FRAMES:
        errors.append(f"successful_frames must be >= {MIN_CAPTURE_FRAMES}")
    if _strict_int(admitted) and cast(int, admitted) < MIN_ADMITTED_FRAMES:
        errors.append(f"admitted_frames must be >= {MIN_ADMITTED_FRAMES}")
    if (
        duration_ns is not None
        and duration_ns > 0
        and _strict_int(successful)
        and _strict_int(admitted)
    ):
        successful_value = cast(int, successful)
        admitted_value = cast(int, admitted)
        if successful_value * 1_000_000_000 < MIN_CAPTURE_RATE_NUM * duration_ns:
            errors.append(
                "capture rate below exact 30.0 FPS (recomputed from integer count/window)"
            )
        if admitted_value * 1_000_000_000 < MIN_ADMISSION_RATE_NUM * duration_ns:
            errors.append(
                "admission rate below exact 15.0 FPS (recomputed from integer count/window)"
            )
        for field, count in (
            ("capture_rate_fps", successful_value),
            ("admission_rate_fps", admitted_value),
        ):
            try:
                displayed = _decimal(metrics.get(field), f"metrics.{field}")
                recomputed = Decimal(count) * Decimal(1_000_000_000) / Decimal(duration_ns)
                # A displayed value is informational, but it must not be a
                # rounded-up claim.  Permit normal decimal formatting while
                # rejecting a value above the exact ratio.
                if displayed > recomputed:
                    errors.append(f"metrics.{field} overstates the recomputed rate")
            except ValueError as exc:
                errors.append(str(exc))

    max_gap = metrics.get("max_inter_frame_gap_ns")
    if not _strict_int(max_gap):
        errors.append("metrics.max_inter_frame_gap_ns must be a non-negative integer")
    elif cast(int, max_gap) > MAX_AGE_NS:
        errors.append("max inter-frame gap exceeds 250ms")
    gaps = metrics.get("inter_frame_gaps_ns")
    if isinstance(gaps, list):
        for index, gap in enumerate(gaps):
            if not _strict_int(gap):
                errors.append(
                    f"metrics.inter_frame_gaps_ns[{index}] must be a non-negative integer"
                )
            elif cast(int, gap) > MAX_AGE_NS:
                errors.append("an inter-frame gap exceeds 250ms")
        if (
            gaps
            and _strict_int(max_gap)
            and max(cast(int, gap) for gap in gaps) != cast(int, max_gap)
        ):
            errors.append("max_inter_frame_gap_ns does not match inter_frame_gaps_ns")

    max_age = metrics.get("max_accepted_age_ns")
    min_age = metrics.get("min_accepted_age_ns")
    if not _strict_int(max_age) or cast(int, max_age) > MAX_AGE_NS:
        errors.append("max accepted frame age must be <= 250ms")
    if not _strict_int(min_age):
        errors.append("min_accepted_age_ns must be a non-negative integer")
    elif cast(int, min_age) > MAX_AGE_NS:
        errors.append("min accepted frame age must be <= 250ms")
    if _strict_int(max_age) and _strict_int(min_age) and cast(int, min_age) > cast(int, max_age):
        errors.append("min_accepted_age_ns must not exceed max_accepted_age_ns")
    ages = metrics.get("accepted_age_ns")
    if isinstance(ages, list):
        for index, age in enumerate(ages):
            if not _strict_int(age):
                errors.append(f"metrics.accepted_age_ns[{index}] must be a non-negative integer")
            elif cast(int, age) > MAX_AGE_NS:
                errors.append("an accepted frame age exceeds 250ms")
        if (
            ages
            and _strict_int(max_age)
            and max(cast(int, age) for age in ages) != cast(int, max_age)
        ):
            errors.append("max_accepted_age_ns does not match accepted_age_ns")
        if (
            ages
            and _strict_int(min_age)
            and min(cast(int, age) for age in ages) != cast(int, min_age)
        ):
            errors.append("min_accepted_age_ns does not match accepted_age_ns")
    min_gap = metrics.get("min_inter_frame_gap_ns")
    if min_gap is not None and not _strict_int(min_gap):
        errors.append("metrics.min_inter_frame_gap_ns must be a non-negative integer")
    elif (
        min_gap is not None
        and _strict_int(min_gap)
        and _strict_int(max_gap)
        and cast(int, min_gap) > cast(int, max_gap)
    ):
        errors.append("min_inter_frame_gap_ns must not exceed max_inter_frame_gap_ns")


def _raw_slot_errors(report: Mapping[str, Any], errors: list[str]) -> None:
    raw = _mapping(report.get("raw_slot"), "raw_slot")
    if raw is None:
        errors.append("raw_slot must be an object")
        return
    fields = ("produced", "delivered", "superseded", "pending", "discarded_on_reset")
    if not all(_strict_int(raw.get(field)) for field in fields):
        errors.append("raw slot counters must be non-negative integers")
        return
    produced = cast(int, raw["produced"])
    delivered = cast(int, raw["delivered"])
    superseded = cast(int, raw["superseded"])
    pending = cast(int, raw["pending"])
    discarded = cast(int, raw["discarded_on_reset"])
    if pending not in (0, 1):
        errors.append("raw pending must be 0 or 1")
    if produced != delivered + superseded + pending + discarded:
        errors.append(
            "raw counter equation mismatch: produced != delivered + superseded + "
            "pending + discarded_on_reset"
        )
    max_depth = raw.get("max_depth")
    if not _strict_int(max_depth) or (produced > 0 and cast(int, max_depth) != 1):
        errors.append("raw max_depth must be exactly 1 after a publish")
    if raw.get("reset_count") != 0 or discarded != 0:
        errors.append("raw reset_count and discarded_on_reset must be zero during the smoke window")
    if produced <= 0:
        errors.append("raw slot must produce at least one sample")
    if raw.get("final_drain_performed") is not True:
        errors.append("raw final drain was not performed")
    if pending != 0:
        errors.append("raw pending must be zero after final drain")
    last = raw.get("last_produced_sequence")
    final = raw.get("final_delivered_sequence")
    if produced > 0 and (not _strict_int(last) or not _strict_int(final) or last != final):
        errors.append("final delivered sequence must equal last produced sequence")
    if raw.get("final_drain_matches_last_produced") is not True:
        errors.append("final drain does not match the last produced sequence")


def _process_errors(report: Mapping[str, Any], errors: list[str]) -> None:
    process = _mapping(report.get("process"), "process")
    if process is None:
        errors.append("process must be an object")
        return
    if process.get("capture_thread_alive") is not False:
        errors.append("capture thread remains alive after cleanup")
    if process.get("backend_child_alive") is not False:
        errors.append("backend child remains alive after cleanup")
    for field in ("residual_thread_count", "residual_child_count"):
        if process.get(field) != 0:
            errors.append(f"process.{field} must be zero")
    try:
        stop_elapsed = _decimal(process.get("stop_elapsed_seconds"), "stop_elapsed_seconds")
        if stop_elapsed > STOP_DEADLINE_SECONDS:
            errors.append("stop_elapsed_seconds exceeds 2.0 seconds")
    except ValueError as exc:
        errors.append(str(exc))


def _input_errors(report: Mapping[str, Any], errors: list[str]) -> None:
    audit = _mapping(report.get("input_audit"), "input_audit")
    if audit is None:
        errors.append("input_audit must be an object")
        return
    expected: dict[str, object] = {
        "input_owner": "legacy",
        "real_input_enabled": False,
        "real_input_call_count": 0,
        "core_v2_real_input_call_count": 0,
        "receiver_connect_count": 0,
        "window_write_count": 0,
        "keyboard_call_count": 0,
        "mouse_call_count": 0,
        "double_write_event_count": 0,
    }
    for field, value in expected.items():
        actual = audit.get(field)
        matches = type(actual) is int and actual == value if type(value) is int else actual == value
        if not matches:
            errors.append(
                f"input_audit.{field} contradicts the zero-input policy (expected {value!r})"
            )


def _privacy_errors(report: Mapping[str, Any], errors: list[str]) -> None:
    audit = _mapping(report.get("privacy_audit"), "privacy_audit")
    if audit is None:
        errors.append("privacy_audit must be an object")
        return
    if audit.get("status") != "PASS":
        errors.append("privacy_audit.status must be PASS")
    if audit.get("raw_artifacts_public") is not False:
        errors.append("raw artifacts must not be public")
    if type(audit.get("pii_findings")) is not int or audit.get("pii_findings") != 0:
        errors.append("privacy_audit.pii_findings must be zero")
    if type(audit.get("failure_count")) is not int or audit.get("failure_count") != 0:
        errors.append("privacy_audit.failure_count must be zero")


def _failure_array_errors(report: Mapping[str, Any], errors: list[str]) -> None:
    failures = report.get("failures")
    if not isinstance(failures, list):
        errors.append("failures must be an array")
        return
    preflight = _mapping(report.get("preflight"), "preflight")
    not_run = preflight is not None and preflight.get("run_started") is False
    if failures and not not_run:
        errors.append("failures must be empty for a measured smoke PASS")


def _expected_status(report: Mapping[str, Any], hard_errors: Sequence[str]) -> str:
    preflight = _mapping(report.get("preflight"), "preflight")
    if preflight is not None and (
        preflight.get("run_started") is False or preflight.get("device_available") is False
    ):
        return "HOLD/NOT-RUN"
    return "FAIL" if hard_errors else "PASS"


def derive_status(report: Mapping[str, Any], *, schema_path: Path = SCHEMA_PATH) -> str:
    """Derive the report state from evidence, independently of ``status``."""

    errors: list[str] = []
    # Schema errors are semantic failures for the purpose of a state decision,
    # but malformed status itself is intentionally not consulted.
    errors.extend(_schema_errors(report, schema_path))
    digest_values = [
        report.get(name) for name in ("report_digest", "canonical_report_sha256") if name in report
    ]
    try:
        expected_digest = canonical_report_digest(report)
    except (TypeError, ValueError) as exc:
        expected_digest = None
        errors.append(f"report canonical digest could not be computed: {exc}")
    if not digest_values or any(
        expected_digest is None or not isinstance(value, str) or value.lower() != expected_digest
        for value in digest_values
    ):
        errors.append("report canonical digest does not match content")
    _validate_binding_fields(report, errors)
    _format_errors(report, errors)
    duration = _window_errors(report, errors)
    _metric_errors(report, duration, errors)
    _require_zero_counts(report, errors)
    _raw_slot_errors(report, errors)
    _process_errors(report, errors)
    _input_errors(report, errors)
    _privacy_errors(report, errors)
    _failure_array_errors(report, errors)
    return _expected_status(report, errors)


def verify_hardware_smoke_report(
    report: Mapping[str, Any] | Path | str,
    *,
    repo_root: Path | None = None,
    external_roots: Mapping[str, Path | str] | None = None,
    metadata_only: bool = True,
    schema_path: Path = SCHEMA_PATH,
) -> list[str]:
    """Return all verification errors for one report.

    ``metadata_only`` still verifies every local artifact when ``repo_root``
    is supplied.  It only permits restricted/external locators to remain
    unresolved; full mode requires those locators to be mapped by a caller.
    """

    if isinstance(report, Path | str):
        try:
            report = read_json(Path(report).resolve(strict=True))
        except (OSError, ValueError) as exc:
            return [f"report could not be read: {exc}"]
    if not isinstance(report, Mapping):
        return ["report must be a JSON object"]
    payload = cast(Mapping[str, Any], report)
    root = None if repo_root is None else Path(repo_root).expanduser()
    schema_path = Path(schema_path)
    errors: list[str] = []
    errors.extend(_schema_errors(payload, schema_path))
    try:
        expected_digest = canonical_report_digest(payload)
    except (TypeError, ValueError) as exc:
        expected_digest = None
        errors.append(f"report canonical digest could not be computed: {exc}")
    digest_values = [
        payload.get(name)
        for name in ("report_digest", "canonical_report_sha256")
        if name in payload
    ]
    if not digest_values or any(
        expected_digest is None or not isinstance(value, str) or value.lower() != expected_digest
        for value in digest_values
    ):
        errors.append("report canonical digest does not match content")
    _validate_binding_fields(payload, errors)
    _format_errors(payload, errors)
    duration = _window_errors(payload, errors)
    _metric_errors(payload, duration, errors)
    _require_zero_counts(payload, errors)
    _raw_slot_errors(payload, errors)
    _process_errors(payload, errors)
    _input_errors(payload, errors)
    _privacy_errors(payload, errors)
    _failure_array_errors(payload, errors)

    artifacts = payload.get("artifacts")
    roots = {name: Path(value).expanduser() for name, value in (external_roots or {}).items()}
    if isinstance(artifacts, list):
        seen: set[str] = set()
        for index, raw in enumerate(artifacts):
            artifact = _mapping(raw, f"artifacts[{index}]")
            if artifact is None:
                errors.append(f"artifacts[{index}] must be an object")
                continue
            artifact_id = artifact.get("artifact_id")
            if isinstance(artifact_id, str):
                if artifact_id in seen:
                    errors.append(f"duplicate artifact_id: {artifact_id}")
                seen.add(artifact_id)
            _check_local_artifact(
                artifact,
                root=root,
                external_roots=roots,
                metadata_only=metadata_only,
                errors=errors,
                prefix=f"artifacts[{index}]",
            )

    hard_errors = list(errors)
    expected_status = _expected_status(payload, hard_errors)
    if payload.get("status") != expected_status:
        errors.append(
            "status is untrusted/mismatched: "
            f"evidence derives {expected_status}, report declares {payload.get('status')!r}"
        )
    return errors


# Friendly aliases used by integration callers and hidden CI smoke tests.
verify_report = verify_hardware_smoke_report
verify = verify_hardware_smoke_report


def assert_valid(
    report: Mapping[str, Any],
    *,
    repo_root: Path | None = None,
    external_roots: Mapping[str, Path | str] | None = None,
    metadata_only: bool = True,
    schema_path: Path = SCHEMA_PATH,
) -> None:
    errors = verify_hardware_smoke_report(
        report,
        repo_root=repo_root,
        external_roots=external_roots,
        metadata_only=metadata_only,
        schema_path=schema_path,
    )
    if errors:
        raise HardwareSmokeVerificationError("; ".join(errors))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="VC-003 report JSON")
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH, help="Strict report schema")
    parser.add_argument(
        "--repo-root", type=Path, default=ROOT, help="Repository root for local artifacts"
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Allow restricted/external artifact locators to remain unresolved",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = read_json(args.report.resolve(strict=True))
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    errors = verify_hardware_smoke_report(
        report,
        repo_root=args.repo_root.resolve(),
        metadata_only=args.metadata_only,
        schema_path=args.schema.resolve(),
    )
    if errors:
        print(
            f"VC-003 hardware smoke verification failed ({len(errors)} error(s)):", file=sys.stderr
        )
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("VC-003 hardware smoke report verified (rates/counters/status recomputed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
