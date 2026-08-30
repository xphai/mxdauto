"""Run the G1-LOC-003C VC-003 read-only live-marker integration.

The command is intentionally a thin orchestration layer.  The capture and
admission state machines live in :mod:`maple_automation_core.capture`; the
only detector called here is ``MinimapMarkerExtractor``.  Public output is a
hash-only report and hash-only bucket ledger.  Pixels, frame metadata and
coordinates are retained only in a caller supplied private CAS/JSONL file.

The production command uses the frozen 30 second warm-up, 300 second
measurement and 100 three-second half-open buckets from the VC003 config.
``run_measurement`` also accepts an injected source/clock and a short window
for deterministic unit tests; the production CLI does not expose those test
overrides.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _datetime
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from maple_automation_core.capture.frame_source import (  # noqa: E402
    FrameAdmissionResult,
    FrameAdmissionStatus,
    FrameSourceConfig,
    canonical_calibration_sha256,
)
from maple_automation_core.capture.pixel_store import (  # noqa: E402
    CaptureSourceProvenance,
    PixelStore,
    canonical_json,
    hash_physical_device_fingerprint,
)
from maple_automation_core.capture.vc003_source import (  # noqa: E402
    NegotiatedCaptureFacts,
    OpenCVCaptureBackend,
    VC003Source,
    VC003SourceConfig,
)
from maple_automation_core.localization.minimap_marker import (  # noqa: E402
    MinimapMarkerConfig,
    MinimapMarkerExtractor,
)
from maple_automation_core.replay.vc003_live_marker import (  # noqa: E402
    BUCKET_COUNT,
    BUCKET_DURATION_NS,
    BUCKET_SECONDS,
    FULL_FRAME_CALIBRATION_SHA256,
    FULL_FRAME_GEOMETRY,
    FULL_FRAME_PIXEL_SPEC,
    GENERATION,
    MAX_AGE_NS,
    CapacityOneMemoryCAS,
    ReadOnlyPixelStore,
    VC003LiveMarkerRunner,
    VC003LiveMarkerThresholds,
    default_minimap_marker_config,
)

CONFIG_PATH = ROOT / "configs" / "g1-loc-003c-vc003-readonly-live.json"
REPORT_SCHEMA_PATH = ROOT / "schemas" / "vc003-readonly-localization-report.schema.json"
LEDGER_SCHEMA_PATH = ROOT / "schemas" / "vc003-readonly-localization-ledger-row.schema.json"
DEFAULT_MARKER_CONFIG_PATH = ROOT / "configs" / "g1-loc-003b-minimap-marker.json"
DEFAULT_LOCK_PATH = ROOT / "configs" / "g1-frame-requirements.lock"
DEFAULT_B2_PACKET_PATH = (
    ROOT / "evidence" / "g1-frame-candidate-20260829" / "g1-frame-candidate-packet.json"
)
DEFAULT_B2_PROVENANCE_PATH = (
    ROOT / "evidence" / "g1-frame-candidate-20260829" / "capture-source-provenance.json"
)
DEFAULT_B2_ZERO_INPUT_PATH = (
    ROOT / "evidence" / "g1-frame-candidate-20260829" / "zero-input-audit-report.json"
)
DEFAULT_LOC003B_REPORT_PATH = (
    ROOT / "evidence" / "g1-loc-003b" / "g1-loc-003b-marker-replay-20260830.json"
)
DEFAULT_WHEEL_PATH = (
    ROOT
    / "evidence"
    / "g1-frame-candidate-20260829"
    / "maple_automation_core-0.1.0-py3-none-any.whl"
)
DEFAULT_PRIVATE_ROWS_REF = "external://g1-loc-003c/restricted-verifier-rows.jsonl"
DEFAULT_LEDGER_REF = "external://g1-loc-003c/public-selected-rows.jsonl"

SCOPE = "G1-LOC-003C"
TRUTH_SCOPE = "live_marker_integration_only"
REPORT_TYPE = "vc003_readonly_localization"
SCHEMA_VERSION = "1.0.0"
TIMESTAMP_ORIGIN = "host_monotonic_post_retrieve"
CLOCK_DOMAIN = "monotonic"
PIXEL_DIGEST_DOMAIN = "MAPLE_PIXEL_V1"
CHAIN = "VC003Source->accepted FramePacket/CAS->MinimapMarkerExtractor->working-space candidate"
DEVICE_INSTANCE_ENV = "VC003_DEVICE_INSTANCE_ID"
TARGET_ADMISSION_HZ = 15.0
POLL_TIMEOUT_SECONDS = 0.05
SCOPE_EXCLUDED = [
    "ObservationResult",
    "affine",
    "world",
    "map",
    "platform",
    "planner",
    "action",
    "input",
    "receiver",
    "window",
    "resolver",
    "accuracy",
]

B2_PACKET_SHA256 = "4e21973f66fd5c4480c1417d1509a0e21069551d728bf02607319008cbf74f73"
LOC003B_REPORT_SHA256 = "37076a1937fa10ce317c4899a43470dfcce9dd7c155f6a0efa8ef089f0efc4d5"
LOC003B_REPORT_DIGEST = "9528f117200bfcb24d3723a081e83e4889f273322c798fef6fd62cfc14a361ff"
MARKER_CONFIG_RAW_SHA256 = "2d77fae38f22386a2ab1465a1c837d2b935f26c020c3a10ffd17f086ae8306b5"
MARKER_CONFIG_SEMANTIC_SHA256 = "47936cf77e46ebc62fd3d6dae241237307ebb370fd81a197745486812c58f22a"
EXTRACTOR_SHA256 = "508b309fce0988a2b0c1e7f4b2ab13a4702a969be5f0175950cb9f779c18a651"
EXPECTED_B2_WHEEL_SHA256 = "62b3b2f362a60087dffadb1d5529c4d7a27440adf61a28d30b685c7cda3b273f"
EXPECTED_B2_LOCK_SHA256 = "1aa30d122b50bb938545bcfc2f50e4d3ba789c473c30e3b6806a73cad38957a9"
EXPECTED_B2_DEVICE_ENV_SHA256 = "b21f9f0bdb9e15ba389aa7de7152a9434e4ebbe7ac7d77ad67cc4e75a8a40898"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")


class VC003RunError(RuntimeError):
    """Raised for a malformed run input or a failed run contract."""


class Clock(Protocol):
    def __call__(self) -> int: ...


class ControlledOpenCVBackend:
    """OpenCV backend whose physical-device pin is supplied by preflight."""

    def __init__(
        self,
        config: VC003SourceConfig,
        *,
        fingerprint_sha256: str,
        probe_every_property_reads: int = 300,
    ) -> None:
        self.inner = OpenCVCaptureBackend(config)
        self._fingerprint = _require_sha(fingerprint_sha256, "device fingerprint")
        self._facts: NegotiatedCaptureFacts | None = None
        self._property_reads = 0
        self._probe_every = probe_every_property_reads

    @property
    def device_name(self) -> str:
        return self.inner.device_name

    @property
    def device_fingerprint_sha256(self) -> str:
        return self._fingerprint

    @property
    def backend_name(self) -> str:
        return self.inner.backend_name

    @property
    def negotiated_facts(self) -> NegotiatedCaptureFacts | None:
        self._property_reads += 1
        if self._facts is None or self._property_reads % self._probe_every == 0:
            self._facts = self.inner.negotiated_facts
        return self._facts

    def start(self) -> None:
        self.inner.start()
        self._facts = self.inner.negotiated_facts

    def read(self) -> Any | None:
        return self.inner.read()

    def stop(self) -> None:
        self.inner.stop()


def _normalize_device_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _enumerate_dshow_video_devices() -> list[tuple[str, str]]:
    """Return DirectShow video names and alternative IDs without persisting them."""

    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=15,
    )
    lines = (completed.stderr + "\n" + completed.stdout).splitlines()
    devices: list[tuple[str, str]] = []
    pending_name: str | None = None
    for line in lines:
        match = re.search(r'"([^"]+)" \(video\)\s*$', line)
        if match is not None:
            pending_name = match.group(1)
            continue
        if pending_name is None:
            continue
        alternative = re.search(r'Alternative name "([^"]+)"', line)
        if alternative is not None:
            devices.append((pending_name, alternative.group(1)))
            pending_name = None
    if not devices:
        raise VC003RunError("DirectShow device enumeration returned no video devices")
    return devices


def _preflight_device(device_name: str, raw_instance_id: str) -> tuple[int, str]:
    """Resolve one named DirectShow device and bind its de-identified fingerprint."""

    if not raw_instance_id:
        raise VC003RunError(f"{DEVICE_INSTANCE_ENV} is required and is never persisted")
    matches = [
        (index, alternative)
        for index, (name, alternative) in enumerate(_enumerate_dshow_video_devices())
        if name == device_name
    ]
    if len(matches) != 1:
        raise VC003RunError("DirectShow selector must resolve exactly one VC-003 video device")
    index, alternative = matches[0]
    normalized_raw = _normalize_device_identity(raw_instance_id)
    if not normalized_raw or normalized_raw not in _normalize_device_identity(alternative):
        raise VC003RunError("DirectShow device identity does not match the pinned instance ID")
    return index, hash_physical_device_fingerprint(raw_instance_id)


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _lexical_safe_path(value: Path | str) -> Path:
    candidate = Path(os.path.abspath(Path(value).expanduser()))
    for component in (*reversed(candidate.parents), candidate):
        try:
            if component.is_symlink() or bool(
                getattr(component.lstat(), "st_file_attributes", 0) & 0x400
            ):
                raise VC003RunError("artifact path must not contain symlinks/reparse points")
        except FileNotFoundError:
            continue
    return candidate


def load_strict_json(path: Path | str) -> dict[str, Any]:
    """Load one UTF-8 JSON object, rejecting duplicates and non-finite values."""

    target = _lexical_safe_path(path)
    if not target.is_file():
        raise VC003RunError(f"JSON artifact must be an existing regular file: {target}")
    try:
        text = target.read_text(encoding="utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise VC003RunError(f"invalid strict JSON artifact: {target}") from exc
    if not isinstance(value, dict):
        raise VC003RunError(f"JSON artifact must be an object: {target}")
    return value


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: Path | str) -> str:
    target = _lexical_safe_path(path)
    if not target.is_file():
        raise VC003RunError(f"artifact must be an existing regular file: {target}")
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise VC003RunError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_commit(value: object, name: str = "source_commit") -> str:
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        raise VC003RunError(f"{name} must be a lowercase 40-character commit")
    return value


def _git_head(repo_root: Path = ROOT) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VC003RunError("could not resolve git HEAD") from exc
    return _require_commit(completed.stdout.strip(), "git HEAD")


def _validate_checkout(
    replay_source_commit: str,
    *,
    repo_root: Path = ROOT,
    allow_dirty: bool = False,
) -> str:
    """Require the requested commit and a clean checkout.

    ``allow_dirty`` exists only for injected unit-test fixtures.  Production
    CLI invocations leave it false and include untracked files in the check.
    """

    expected = _require_commit(replay_source_commit, "--source-commit")
    head = _git_head(repo_root)
    if expected != head:
        raise VC003RunError("--source-commit must equal the current git HEAD")
    if allow_dirty:
        return head
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VC003RunError("could not inspect checkout cleanliness") from exc
    if completed.stdout.strip():
        raise VC003RunError("checkout must be clean")
    return head


def _require_file(value: Path | str, name: str) -> Path:
    target = _lexical_safe_path(value)
    if not target.is_file():
        raise VC003RunError(f"{name} must be an existing regular file: {target}")
    return target


def _require_dir(value: Path | str, name: str) -> Path:
    target = _lexical_safe_path(value)
    if not target.is_dir():
        raise VC003RunError(f"{name} must be an existing directory: {target}")
    return target


def _utc_timestamp() -> str:
    return (
        _datetime.datetime.now(_datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _write_atomic_bytes(path: Path | str, payload: bytes) -> None:
    target = _lexical_safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _write_atomic_json(path: Path | str, payload: Mapping[str, Any]) -> None:
    """Write canonical UTF-8 JSON with exactly one LF using atomic replace."""

    _write_atomic_bytes(path, canonical_json(payload) + b"\n")


def _write_jsonl(path: Path | str, rows: Iterable[Mapping[str, Any]]) -> None:
    encoded = b"".join(canonical_json(row) + b"\n" for row in rows)
    _write_atomic_bytes(path, encoded)


def _external_ref(path: Path | str, role: str) -> str:
    # Never copy a local path into public evidence.
    suffix = Path(path).name or role
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", suffix)
    return f"external://g1-loc-003c/{role}/{safe}"


def _format_from_facts(
    facts: NegotiatedCaptureFacts | None,
    config: VC003SourceConfig,
) -> dict[str, Any]:
    if facts is not None:
        value = facts.to_format_dict()
        # The report schema freezes the requested 30 FPS contract.  DirectShow
        # may expose a tiny floating-point reporting residual, which is
        # already admitted by VC003Source's sub-millihertz tolerance.
        if abs(float(value.get("fps", 0.0)) - 30.0) <= 0.001:
            value["fps"] = 30.0
        return value
    return {
        "width": config.width,
        "height": config.height,
        "fps": config.fps,
        "fourcc": "MJPG",
        "backend": config.backend,
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": config.width * 3,
        "length": config.width * config.height * 3,
    }


def _build_live_provenance(
    source_config: VC003SourceConfig,
    device_environment: Mapping[str, Any],
    *,
    device_fingerprint_sha256: str,
    source_commit: str,
    dependency_lock_sha256: str,
    source_artifact_sha256: str,
) -> CaptureSourceProvenance:
    """Rebind measured B2 facts to this exact live session and tool revision."""

    expected_fingerprint = device_environment.get("physical_device_fingerprint_sha256")
    if expected_fingerprint != device_fingerprint_sha256:
        raise VC003RunError("current device fingerprint does not match the pinned environment")
    negotiated = device_environment.get("negotiated")
    backend_version = device_environment.get("backend_version")
    if not isinstance(negotiated, Mapping) or not isinstance(backend_version, str):
        raise VC003RunError("device environment lacks measured negotiated facts")
    source_config_sha256 = hashlib.sha256(canonical_json(source_config.to_dict())).hexdigest()
    calibration_sha256 = canonical_calibration_sha256(
        source_config.geometry,
        source_config.transform_version,
    )
    if calibration_sha256 != FULL_FRAME_CALIBRATION_SHA256:
        raise VC003RunError("source calibration is not the frozen full-frame calibration")
    return CaptureSourceProvenance(
        requested=_format_from_facts(None, source_config),
        negotiated=dict(negotiated),
        backend=source_config.backend,
        timestamp_origin=TIMESTAMP_ORIGIN,
        upstream_queue=str(device_environment.get("upstream_queue", "unknown")),
        physical_device_fingerprint_sha256=device_fingerprint_sha256,
        input_owner="legacy",
        real_input_enabled=False,
        real_input_call_count=0,
        source_id=source_config.source_id,
        session_id=source_config.session_id,
        backend_version=backend_version,
        tool_artifact_sha256=sha256_file(Path(__file__)),
        dependency_lock_sha256=dependency_lock_sha256,
        source_artifact_sha256=source_artifact_sha256,
        source_commit=source_commit,
        config_sha256=source_config_sha256,
        calibration_sha256=calibration_sha256,
    )


def _require_fresh_private_cas(root: Path) -> None:
    """Require an empty, run-specific selected-occurrence CAS directory."""

    if root.is_symlink() or not root.is_dir():
        raise VC003RunError("private CAS root must be a real directory")
    if any(root.iterdir()):
        raise VC003RunError("private CAS root must be empty for a new LOC-003C run")


def _config_bindings(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    raw = config.get("expected_bindings")
    if not isinstance(raw, Mapping):
        raise VC003RunError("config.expected_bindings must be an object")
    result: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            raise VC003RunError("config.expected_bindings contains an invalid binding")
        kind = value.get("kind")
        digest = value.get("expected_sha256")
        external = value.get("external_ref")
        if not isinstance(kind, str) or not kind:
            raise VC003RunError(f"binding {key} has no kind")
        _require_sha(digest, f"binding {key}.expected_sha256")
        if not isinstance(external, str) or not external.startswith("external://"):
            raise VC003RunError(f"binding {key}.external_ref must be external://")
        result[key] = {"kind": kind, "expected_sha256": cast(str, digest), "external_ref": external}
    return result


def _binding_paths_from_args(args: argparse.Namespace) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    names = {
        "upstream_b2_packet": getattr(args, "upstream_b2_packet", None),
        "loc003b_report_raw": getattr(args, "loc003b_report", None),
        "base_marker_config_raw": getattr(args, "base_marker_config", None),
        "calibration": getattr(args, "calibration", None),
        "extractor": getattr(args, "extractor_source", None),
        "wheel": getattr(args, "wheel", None),
        "dependency_lock": getattr(args, "lock", None),
        "device_environment": getattr(args, "device_environment", None),
    }
    for key, value in names.items():
        if value is not None:
            paths[key] = _require_file(value, f"binding {key}")
    for raw in getattr(args, "binding", []) or []:
        if "=" not in raw:
            raise VC003RunError("--binding expects NAME=PATH")
        name, value = raw.split("=", 1)
        if not name or name in paths:
            raise VC003RunError("--binding names must be unique")
        paths[name] = _require_file(value, f"binding {name}")
    return paths


def _semantic_binding_digest(key: str, path: Path) -> str:
    if key == "upstream_b2_packet":
        payload = load_strict_json(path)
        declared = payload.get("packet_digest")
        if isinstance(declared, str) and SHA256_RE.fullmatch(declared):
            return declared
        raise VC003RunError("upstream B2 packet has no valid packet_digest")
    if key == "loc003b_report_semantic":
        payload = load_strict_json(path)
        declared = payload.get("report_digest")
        if isinstance(declared, str) and SHA256_RE.fullmatch(declared):
            return declared
        return _canonical_digest({k: v for k, v in payload.items() if k != "report_digest"})
    if key in {"base_marker_config_semantic", "marker_config_semantic"}:
        marker = MinimapMarkerConfig.from_dict(load_strict_json(path))
        return marker.digest
    if key in {"calibration", "marker_calibration"}:
        # Calibration JSON files carry the digest as a field; hashing the
        # envelope would bind incidental metadata instead of the calibration
        # contract.  A bare calibration payload still gets a content digest.
        payload = load_strict_json(path)
        fields = ("calibration_sha256", "calibration_digest", "digest")
        for field in fields:
            value = payload.get(field)
            if isinstance(value, str) and SHA256_RE.fullmatch(value):
                return value.lower()
    return sha256_file(path)


def verify_external_bindings(
    config: Mapping[str, Any],
    paths: Mapping[str, Path | str],
    *,
    expected: Mapping[str, str] | None = None,
) -> list[str]:
    """Return binding errors without dereferencing or exposing local paths."""

    errors: list[str] = []
    bindings = _config_bindings(config)
    expected_map = dict(expected or {})
    for key, expected_binding in bindings.items():
        path_value = paths.get(key)
        if path_value is None:
            # Semantic aliases are commonly supplied through their raw file.
            if key == "loc003b_report_semantic":
                path_value = paths.get("loc003b_report_raw")
            elif key == "base_marker_config_semantic":
                path_value = paths.get("base_marker_config_raw")
        if path_value is None:
            errors.append(f"binding_missing:{key}")
            continue
        try:
            actual = _semantic_binding_digest(key, _require_file(path_value, f"binding {key}"))
        except (OSError, TypeError, ValueError, VC003RunError) as exc:
            errors.append(f"binding_unreadable:{key}:{type(exc).__name__}")
            continue
        required = expected_map.get(key, expected_binding["expected_sha256"])
        if actual.casefold() != required.casefold():
            errors.append(f"binding_digest_mismatch:{key}")
    for key, required in expected_map.items():
        if key in bindings:
            continue
        path_value = paths.get(key)
        if path_value is None:
            errors.append(f"binding_missing:{key}")
            continue
        try:
            actual = sha256_file(path_value)
        except (OSError, VC003RunError) as exc:
            errors.append(f"binding_unreadable:{key}:{type(exc).__name__}")
            continue
        if actual.casefold() != required.casefold():
            errors.append(f"binding_digest_mismatch:{key}")
    return errors


def _frame_digest(packet: object) -> str:
    item = cast(Any, packet)
    return _canonical_digest(
        {
            "session_id": item.session_id,
            "source_id": item.source_id,
            "frame_id": item.frame_id,
            "captured_at_ns": item.captured_at_ns,
            "admitted_at_ns": item.received_at_ns,
            "pixel_digest": item.content_hash,
        }
    )


def _result_status(result: object) -> str:
    value = getattr(result, "status", None)
    if isinstance(result, Mapping):
        value = result.get("status", value)
    value = getattr(value, "value", value)
    text = str(value).casefold()
    if text in {"candidate", "found", "detected", "accepted"}:
        return "candidate"
    if text in {"no_candidate", "no_marker", "no-marker", "none", "unknown"}:
        return "no_candidate"
    return "fault"


def _result_field(result: object, name: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _digest_result_field(result: object, field: str) -> str | None:
    value = _result_field(result, field)
    if isinstance(value, str) and SHA256_RE.fullmatch(value):
        return value
    obj_name = {"candidate_digest": "candidate", "evidence_digest": "evidence"}.get(field)
    obj = None if obj_name is None else _result_field(result, obj_name)
    if obj is None:
        return None
    declared = getattr(obj, "digest", None)
    if isinstance(declared, str) and SHA256_RE.fullmatch(declared):
        return declared
    body = obj.to_dict() if callable(getattr(obj, "to_dict", None)) else obj
    return _canonical_digest(body)


def _result_digest(result: object) -> str:
    declared = _result_field(result, "result_digest")
    if not isinstance(declared, str):
        declared = _result_field(result, "digest")
    if isinstance(declared, str) and SHA256_RE.fullmatch(declared):
        return declared
    body = result.to_dict() if callable(getattr(result, "to_dict", None)) else result
    return _canonical_digest(body)


def _public_row(selection: object, row: object, ordinal: int) -> dict[str, Any]:
    # ``VC003SelectedRow.to_dict`` is the public API owned by the replay
    # integration.  Reusing it here keeps row/frame/evidence digests exactly
    # aligned with the verifier and prevents a tool-local second contract.
    body = dict(row.to_dict())
    bucket = int(selection.bucket_index)
    start = int(selection.bucket_start_ns)
    received = int(selection.received_at_ns)
    body["bucket_index"] = bucket
    body["sample_ordinal"] = ordinal
    body["bucket_offset_ns"] = received - start
    body["selected"] = True
    body["row_digest"] = _canonical_digest(
        {key: value for key, value in body.items() if key != "row_digest"}
    )
    return body


def _private_row(
    selection: object,
    row: object,
    result: object,
    device_digest: str | None,
    artifact_ref: str = DEFAULT_PRIVATE_ROWS_REF,
) -> dict[str, Any]:
    candidate = _result_field(result, "candidate")
    evidence = _result_field(result, "evidence")
    components = getattr(evidence, "components", ()) if evidence is not None else ()
    component = components[0] if len(components) == 1 else None
    candidate_body = candidate.to_dict() if callable(getattr(candidate, "to_dict", None)) else None
    component_body = component.to_dict() if component is not None else None
    public = _public_row(selection, row, int(row.bucket_index))
    private: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "row_kind": "restricted_verifier",
        "bucket_index": int(row.bucket_index),
        "sample_id": str(row.sample_id),
        "status": str(row.marker_status).casefold(),
        "generation": GENERATION,
        "session_id": str(row.session_id),
        "source_id": str(row.source_id),
        "frame_id": int(row.frame_id),
        "source_sequence": int(row.source_sequence),
        "source_provenance_id": str(row.source_provenance_id),
        "frame_digest": public["frame_digest"],
        "pixel_digest": str(row.pixel_digest),
        "occurrence_artifact_sha256": str(row.artifact_sha256),
        "candidate_digest": public["candidate_digest"],
        "evidence_digest": public["evidence_digest"],
        "result_digest": public["result_digest"],
        "observed_at_ns": int(row.checked_at_ns),
        "captured_at_ns": int(row.captured_at_ns),
        "received_at_ns": int(row.admitted_at_ns),
        "pixel_ref": f"external://g1-loc-003c/cas/sha256/{row.pixel_digest}",
        "verifier_artifact_ref": artifact_ref,
        "artifact_ref": artifact_ref,
        "privacy_class": "restricted",
        "retention_class": "candidate",
        "row_digest": public["row_digest"],
    }
    if device_digest is not None:
        private["device_fingerprint_sha256"] = device_digest
    if candidate_body is not None:
        anchor = candidate_body.get("anchor_working")
        if isinstance(anchor, Mapping):
            private["working_candidate"] = {
                key: anchor[key] for key in ("x", "y", "width", "height") if key in anchor
            }
    if isinstance(component_body, Mapping):
        if "source_bbox" in component_body:
            private["source_bbox"] = component_body["source_bbox"]
        if "source_centroid" in component_body:
            private["source_centroid"] = component_body["source_centroid"]
        if "area" in component_body:
            private["component_area"] = component_body["area"]
        if "bright_core_pixels" in component_body:
            private["bright_core_pixels"] = component_body["bright_core_pixels"]
    return private


def _status_counts(admissions: Sequence[FrameAdmissionResult]) -> dict[str, int]:
    counts = Counter(str(item.status.value) for item in admissions)
    return {status.value: counts.get(status.value, 0) for status in FrameAdmissionStatus}


def _accepted_ledger_digest(admissions: Sequence[FrameAdmissionResult]) -> str:
    return _canonical_digest(
        [
            {key: value for key, value in row.items() if key != "status"}
            for row in _accepted_ledger_rows(admissions)
        ]
    )


def _accepted_ledger_rows(admissions: Sequence[FrameAdmissionResult]) -> list[dict[str, Any]]:
    """Return the restricted, hash-only accepted occurrence ledger."""

    rows: list[dict[str, Any]] = []
    for item in admissions:
        if not item.accepted or item.packet is None:
            continue
        packet = item.packet
        rows.append(
            {
                "status": "accepted",
                "frame_id": packet.frame_id,
                "captured_at_ns": packet.captured_at_ns,
                "received_at_ns": packet.received_at_ns,
                "session_id": packet.session_id,
                "source_id": packet.source_id,
                "pixel_digest": packet.content_hash,
                "frame_digest": _frame_digest(packet),
            }
        )
    return rows


def _row_set_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    return _canonical_digest(list(rows))


def _make_zero_input(run_index: int) -> dict[str, Any]:
    return {
        "audit_id": f"vc003-{run_index:03d}-zero-input",
        "run_index": run_index,
        "scope": SCOPE,
        "reused_from_b2": False,
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


def _make_privacy(run_index: int) -> dict[str, Any]:
    return {
        "audit_id": f"vc003-{run_index:03d}-privacy",
        "run_index": run_index,
        "scope": SCOPE,
        "reused_from_b2": False,
        "raw_artifacts_public": False,
        "coordinates_present": False,
        "absolute_paths_present": False,
        "raw_bytes_present": False,
        "device_original_id_present": False,
        "private_artifacts_external_only": True,
        "finding_count": 0,
    }


def _cleanup_payload(source: object, stop_ok: bool, private_released: bool) -> dict[str, Any]:
    residual = 0
    lifecycle: str | None = None
    source_error: str | None = None
    accounting_holds = True
    status_method = getattr(source, "status", None)
    if callable(status_method):
        try:
            status = status_method()
            residual = int(getattr(status, "residual_worker_count", 0))
            lifecycle_value = getattr(status, "lifecycle", None)
            lifecycle = lifecycle_value if isinstance(lifecycle_value, str) else None
            error_value = getattr(status, "error", None)
            source_error = error_value if isinstance(error_value, str) else None
            accounting_holds = bool(getattr(status, "accounting_holds", False))
        except Exception:
            stop_ok = False
    else:
        thread = getattr(source, "thread", None)
        if thread is not None and callable(getattr(thread, "is_alive", None)):
            residual = int(thread.is_alive())
    lifecycle_ok = lifecycle in {None, "created", "stopped"}
    cleanup_ok = (
        stop_ok and residual == 0 and lifecycle_ok and source_error is None and accounting_holds
    )
    return {
        "status": "PASS" if cleanup_ok else "FAIL",
        "capture_stopped": stop_ok and lifecycle_ok,
        "residual_thread_count": residual,
        "residual_child_count": 0,
        "private_artifacts_released": private_released,
        "cleanup_failure_count": 0 if cleanup_ok else 1,
    }


def _production_config(config: Mapping[str, Any]) -> None:
    if config.get("schema_version") != SCHEMA_VERSION or config.get("scope") != SCOPE:
        raise VC003RunError("VC003 config schema/scope mismatch")
    if config.get("truth_scope") != TRUTH_SCOPE or config.get("generation") != GENERATION:
        raise VC003RunError("VC003 config truth scope/generation mismatch")
    window = config.get("measurement_window")
    expected_window = {
        "warmup_seconds": 30,
        "measurement_seconds": 300,
        "bucket_count": BUCKET_COUNT,
        "bucket_seconds": BUCKET_SECONDS,
        "target_admission_hz": 15.0,
        "poll_timeout_seconds": 0.05,
        "bucket_clock": "FramePacket.received_at_ns",
        "bucket_boundary": "half_open",
        "generation": GENERATION,
    }
    if window != expected_window:
        raise VC003RunError("VC003 measurement window is not the frozen production window")
    capture = config.get("capture")
    if not isinstance(capture, Mapping):
        raise VC003RunError("VC003 capture config is missing")
    expected = {
        "read_only": True,
        "source_id": "capture-card-primary",
        "selector": "VC-003 Video",
        "backend": "dshow",
        "fourcc": "MJPG",
        "fps": 30.0,
        "width": 1920,
        "height": 1080,
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": 5760,
        "length": 6220800,
        "timestamp_origin": TIMESTAMP_ORIGIN,
        "clock_domain": CLOCK_DOMAIN,
        "source_size": {"width": 1920, "height": 1080},
        "content_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
        "working_size": {"width": 1920, "height": 1080},
    }
    for key, value in expected.items():
        if capture.get(key) != value:
            raise VC003RunError(f"VC003 capture config field {key!r} is not frozen")
    if config.get("scope_excluded") != SCOPE_EXCLUDED:
        raise VC003RunError("VC003 scope_excluded is not frozen")
    marker = config.get("marker")
    if not isinstance(marker, Mapping):
        raise VC003RunError("VC003 marker config is missing")
    expected_marker = {
        "extractor": "MinimapMarkerExtractor",
        "extractor_artifact_sha256": EXTRACTOR_SHA256,
        "base_config_raw_sha256": MARKER_CONFIG_RAW_SHA256,
        "base_config_semantic_sha256": MARKER_CONFIG_SEMANTIC_SHA256,
        "calibration_sha256": FULL_FRAME_CALIBRATION_SHA256,
        "roi": {"x": 309, "y": 238, "width": 97, "height": 113},
        "bucket_clock": "FramePacket.received_at_ns",
        "bucket_boundary": "half_open",
        "working_space_candidate": True,
        "resolver_invoked": False,
        "accuracy_evaluated": False,
    }
    for key, value in expected_marker.items():
        if marker.get(key) != value:
            raise VC003RunError(f"VC003 marker config field {key!r} is not frozen")
    base_marker = marker.get("base_config")
    if not isinstance(base_marker, Mapping):
        raise VC003RunError("VC003 marker base_config is missing")
    parsed_marker = MinimapMarkerConfig.from_dict(base_marker)
    if parsed_marker.digest != MARKER_CONFIG_SEMANTIC_SHA256:
        raise VC003RunError("VC003 marker semantic digest is not frozen")
    expected_input = {
        "input_owner": "legacy",
        "real_input_enabled": False,
        "real_input_call_count": 0,
        "core_v2_real_input_call_count": 0,
        "double_write_event_count": 0,
    }
    if config.get("input_policy") != expected_input:
        raise VC003RunError("VC003 zero-input policy is not frozen")
    expected_output = {
        "public_row_kind": "hash_only",
        "public_selected_row_count": BUCKET_COUNT,
        "one_row_per_bucket": True,
        "allow_duplicate_pixel_digest": True,
        "include_coordinates": False,
        "include_raw_bytes": False,
        "include_absolute_paths": False,
        "include_device_original_id": False,
    }
    if (
        config.get("output_policy") != expected_output
        or config.get("run_specific_audits") is not True
    ):
        raise VC003RunError("VC003 output/privacy policy is not frozen")


def _build_report(
    *,
    config: Mapping[str, Any],
    config_sha256: str,
    source_commit: str,
    source_config: VC003SourceConfig,
    source: object,
    admissions: Sequence[FrameAdmissionResult],
    integration: VC003LiveMarkerRunner,
    measurement_start_ns: int,
    measurement_end_ns: int,
    warmup_seconds: int,
    measurement_seconds: int,
    private_rows_ref: str = DEFAULT_PRIVATE_ROWS_REF,
    private_rows_sha256: str | None = None,
    private_rows_count: int = 0,
    device_fingerprint_sha256: str | None = None,
    run_index: int = 1,
    cleanup: Mapping[str, Any] | None = None,
    binding_errors: Sequence[str] = (),
    extra_failures: Sequence[str] = (),
    external_artifacts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    selector = integration.selector
    selections = () if selector is None else tuple(selector.selected)
    rows = tuple(integration.rows)
    public_rows = [
        _public_row(selection, row, index)
        for index, (selection, row) in enumerate(zip(selections, rows, strict=False))
    ]
    # A selector never emits more than one row per bucket and always emits in
    # first-arrival order.  Keep report order canonical by bucket index.
    public_rows.sort(key=lambda item: int(item["bucket_index"]))
    for index, row in enumerate(public_rows):
        row["sample_ordinal"] = index
        row["row_digest"] = _canonical_digest(
            {key: value for key, value in row.items() if key != "row_digest"}
        )
    accepted_count = sum(1 for item in admissions if item.accepted)
    status_counts = _status_counts(admissions)
    marker_counts = Counter(str(row.marker_status).casefold() for row in rows)
    candidate_digests = [
        row["candidate_digest"] for row in public_rows if row["candidate_digest"] is not None
    ]
    result_digests = [row["result_digest"] for row in public_rows]
    coverage = [
        {
            "bucket_index": index,
            "selected": any(row["bucket_index"] == index for row in public_rows),
            "status": next(
                (row["status"] for row in public_rows if row["bucket_index"] == index),
                None,
            ),
        }
        for index in range(BUCKET_COUNT)
    ]
    failures = list(binding_errors) + list(extra_failures)
    if len(public_rows) != BUCKET_COUNT:
        failures.append("selector_incomplete")
    if private_rows_sha256 is None or private_rows_count != len(public_rows):
        failures.append("private_rows_unavailable")
    if any(row["status"] == "fault" for row in public_rows):
        failures.append("marker_fault")
    thresholds = integration.thresholds
    if marker_counts.get("candidate", 0) < thresholds.min_candidate_count:
        failures.append("candidate_threshold")
    if marker_counts.get("fault", 0) > thresholds.max_marker_faults:
        failures.append("marker_fault_threshold")
    if not all(row["selected"] is True for row in public_rows):
        failures.append("public_row_not_selected")
    lineage_valid = bool(getattr(integration.validate(require_complete=False), "valid", False))
    if not lineage_valid:
        failures.append("lineage_invalid")
    cleanup_payload = dict(
        cleanup or _cleanup_payload(source, True, private_rows_sha256 is not None)
    )
    if cleanup_payload.get("status") != "PASS":
        failures.append("cleanup_failed")
    failure_codes = sorted(
        set(
            re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")[:100] or "failure"
            for value in failures
        )
    )
    status = "PASS" if not failure_codes else "FAIL"
    execution_valid = status == "PASS"
    zero_input = _make_zero_input(run_index)
    privacy = _make_privacy(run_index)
    facts = getattr(source, "negotiated_facts", None)
    if callable(facts):
        try:
            facts = facts()
        except Exception:
            facts = None
    negotiated = _format_from_facts(cast(NegotiatedCaptureFacts | None, facts), source_config)
    try:
        source_geometry = source_config.geometry.to_dict()
    except AttributeError:
        source_geometry = FULL_FRAME_GEOMETRY.to_dict()
    accepted_ledger = _accepted_ledger_digest(admissions)
    selector_digest = _row_set_digest(public_rows)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "scope": SCOPE,
        "truth_scope": TRUTH_SCOPE,
        "scope_excluded": list(SCOPE_EXCLUDED),
        "report_id": f"vc003-live-marker-{run_index:03d}",
        "generated_at": _utc_timestamp(),
        "source_commit": source_commit,
        "config_sha256": config_sha256,
        "status": status,
        "execution_valid": execution_valid,
        "expected_bindings": _config_bindings(config),
        "capture": {
            "read_only": True,
            "source_id": source_config.source_id,
            "selector": source_config.device_name,
            "requested": _format_from_facts(None, source_config),
            "negotiated": negotiated,
            "timestamp_origin": TIMESTAMP_ORIGIN,
            "clock_domain": CLOCK_DOMAIN,
            "accepted_cas_required": True,
            "device_fingerprint_sha256": device_fingerprint_sha256,
            "geometry": source_geometry,
        },
        "admission": {
            "accepted_count": accepted_count,
            "rejected_count": len(admissions) - accepted_count,
            "status_counts": status_counts,
            "accepted_frame_ledger_sha256": accepted_ledger,
            "cas_lineage_verified": lineage_valid,
            "accepted_cas_count": len(rows),
            "accepted_packet_count": accepted_count,
            "max_accepted_age_ns": max(
                (
                    item.event.observed_at_ns - item.packet.captured_at_ns
                    for item in admissions
                    if item.accepted and item.packet is not None
                ),
                default=0,
            ),
            "max_inter_frame_gap_ns": max(
                (
                    current.packet.received_at_ns - previous.packet.received_at_ns
                    for previous, current in zip(
                        [item for item in admissions if item.accepted and item.packet is not None],
                        [item for item in admissions if item.accepted and item.packet is not None][
                            1:
                        ],
                        strict=False,
                    )
                ),
                default=0,
            ),
        },
        "selector": {
            "policy": "one_representative_per_3s_bucket",
            "bucket_count": BUCKET_COUNT,
            "selected_count": len(public_rows),
            "selected_rows_digest": selector_digest,
            "allow_duplicate_pixel_digest": True,
            "candidate_count": marker_counts.get("candidate", 0),
            "no_candidate_count": marker_counts.get("no_candidate", 0),
            "rejected_count": marker_counts.get("rejected", 0),
            "fault_count": marker_counts.get("fault", 0),
        },
        "marker": {
            "extractor": "MinimapMarkerExtractor",
            "extractor_artifact_sha256": EXTRACTOR_SHA256,
            "config_semantic_sha256": MARKER_CONFIG_SEMANTIC_SHA256,
            "base_config_raw_sha256": MARKER_CONFIG_RAW_SHA256,
            "calibration_sha256": FULL_FRAME_CALIBRATION_SHA256,
            "geometry": FULL_FRAME_GEOMETRY.to_dict(),
            "candidate_stage": "working_space",
            "resolver_invoked": False,
            "accuracy_evaluated": False,
            "candidate_count": marker_counts.get("candidate", 0),
            "no_candidate_count": marker_counts.get("no_candidate", 0),
            "fault_count": marker_counts.get("fault", 0),
            "candidate_digest": _canonical_digest(candidate_digests),
            "result_digest": _canonical_digest(result_digests),
        },
        "timing": {
            "warmup_seconds": warmup_seconds,
            "measurement_seconds": measurement_seconds,
            "bucket_count": BUCKET_COUNT,
            "bucket_seconds": BUCKET_SECONDS,
            "bucket_clock": "FramePacket.received_at_ns",
            "bucket_boundary": "half_open",
            "generation": GENERATION,
            "timestamp_origin": TIMESTAMP_ORIGIN,
            "clock_domain": CLOCK_DOMAIN,
            "monotonic": True,
            "bucket_coverage_digest": _canonical_digest(coverage),
            "measurement_start_offset_ns": 0,
            "measurement_end_offset_ns": max(0, measurement_end_ns - measurement_start_ns),
            "max_inter_frame_gap_ns": max(
                (
                    current.packet.received_at_ns - previous.packet.received_at_ns
                    for previous, current in zip(
                        [item for item in admissions if item.accepted and item.packet is not None],
                        [item for item in admissions if item.accepted and item.packet is not None][
                            1:
                        ],
                        strict=False,
                    )
                ),
                default=0,
            ),
        },
        "lineage": {
            "chain": CHAIN,
            "upstream_b2_packet_sha256": B2_PACKET_SHA256,
            "accepted_frame_ledger_sha256": accepted_ledger,
            "pixel_digest_domain": PIXEL_DIGEST_DOMAIN,
            "cas_required": True,
            "candidate_output": "working_space_candidate",
            "resolver_invoked": False,
            "accuracy_evaluated": False,
            "world_state_emitted": False,
            "private_rows_external_ref": private_rows_ref,
        },
        "failure": {
            "capture_error_count": sum(status_counts.get(name, 0) for name in ("source_error",)),
            "admission_error_count": sum(
                status_counts.get(name, 0)
                for name in (
                    "stale",
                    "duplicate",
                    "out_of_order",
                    "timestamp_regression",
                    "frame_size_changed",
                    "source_mismatch",
                    "session_mismatch",
                    "clock_domain_mismatch",
                )
            ),
            "selector_error_count": int("selector_incomplete" in failures),
            "marker_error_count": marker_counts.get("fault", 0),
            "timing_error_count": int(measurement_end_ns < measurement_start_ns),
            "lineage_error_count": int(not lineage_valid),
            "privacy_error_count": 0,
            "cleanup_error_count": int(cleanup_payload.get("status") != "PASS"),
            "total_count": len(failure_codes),
            "codes": failure_codes,
        },
        "zero_input": zero_input,
        "privacy": privacy,
        "cleanup": cleanup_payload,
        "runs": [
            {
                "run_index": run_index,
                "selected_row_count": len(public_rows),
                "zero_input": zero_input,
                "privacy": privacy,
                "status": status,
                "execution_valid": execution_valid,
                "selected_rows_digest": selector_digest,
            }
        ],
        "selected_row_count": len(public_rows),
        "public_selected_rows": public_rows,
        "restricted_rows_artifact": {
            "external_ref": private_rows_ref,
            "sha256": private_rows_sha256 or ("0" * 64),
            "row_count": private_rows_count,
            "privacy_class": "restricted",
        },
        "artifacts": [dict(item) for item in external_artifacts],
        "limitations": [
            "truth_scope is live_marker_integration_only; no resolver or marker "
            "accuracy claim is made.",
            "The read-only chain stops at a working-space candidate and emits no "
            "world/platform/input state.",
            "Raw pixels and restricted verifier rows remain external to the public report.",
        ],
    }
    report["report_digest"] = _canonical_digest(
        {key: value for key, value in report.items() if key != "report_digest"}
    )
    return report


def run_measurement(
    *,
    source: object,
    source_config: VC003SourceConfig | FrameSourceConfig | None = None,
    marker_config: MinimapMarkerConfig | None = None,
    retained_store: PixelStore | None = None,
    memory_cas: CapacityOneMemoryCAS | None = None,
    clock: Clock | None = None,
    warmup_seconds: int = 30,
    measurement_seconds: int = 300,
    measurement_start_ns: int | None = None,
    target_admission_hz: float = TARGET_ADMISSION_HZ,
    poll_timeout_seconds: float = POLL_TIMEOUT_SECONDS,
    max_iterations: int = 2_000_000,
    run_index: int = 1,
    config: Mapping[str, Any] | None = None,
    config_sha256: str | None = None,
    source_commit: str | None = None,
    private_rows_path: Path | str | None = None,
    private_rows_ref: str = DEFAULT_PRIVATE_ROWS_REF,
    accepted_ledger_path: Path | str | None = None,
    device_fingerprint_sha256: str | None = None,
    binding_errors: Sequence[str] = (),
    external_artifacts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Run one source through the actual adapter/extractor and return a report.

    Test callers may inject a finite source and a fake clock and use short
    warm-up/measurement values.  Production values are validated by the CLI
    before this function is called.
    """

    if type(warmup_seconds) is not int or warmup_seconds < 0:
        raise VC003RunError("warmup_seconds must be a non-negative integer")
    if type(measurement_seconds) is not int or measurement_seconds < 0:
        raise VC003RunError("measurement_seconds must be a non-negative integer")
    if (
        isinstance(target_admission_hz, bool)
        or not isinstance(target_admission_hz, int | float)
        or target_admission_hz <= 0
    ):
        raise VC003RunError("target_admission_hz must be positive")
    if (
        isinstance(poll_timeout_seconds, bool)
        or not isinstance(poll_timeout_seconds, int | float)
        or poll_timeout_seconds < 0
    ):
        raise VC003RunError("poll_timeout_seconds must be non-negative")
    if max_iterations <= 0:
        raise VC003RunError("max_iterations must be positive")
    now: Clock = time.monotonic_ns if clock is None else clock
    source_cfg: VC003SourceConfig
    if isinstance(source_config, VC003SourceConfig):
        source_cfg = source_config
    elif isinstance(source_config, FrameSourceConfig):
        source_cfg = VC003SourceConfig(
            source_id=source_config.source_id,
            session_id=source_config.session_id,
            clock_domain=source_config.clock_domain,
            transform_version=source_config.transform_version,
            source_geometry=source_config.source_geometry,
        )
    elif isinstance(source, VC003Source):
        source_cfg = source.config
    else:
        source_cfg = VC003SourceConfig()
    if source_cfg.width != 1920 or source_cfg.height != 1080:
        raise VC003RunError("VC003 run requires a negotiated full-frame 1920x1080 source")
    marker = (
        default_minimap_marker_config(
            session_id=source_cfg.session_id,
            source_id=source_cfg.source_id,
            clock_domain=source_cfg.clock_domain,
        )
        if marker_config is None
        else marker_config
    )
    if marker.geometry != FULL_FRAME_GEOMETRY or marker.pixel_spec != FULL_FRAME_PIXEL_SPEC:
        raise VC003RunError("marker config must be full-frame 1920x1080")
    if marker.calibration_sha256 != FULL_FRAME_CALIBRATION_SHA256:
        raise VC003RunError("marker calibration does not match VC003 full-frame calibration")
    if device_fingerprint_sha256 is None:
        candidate_device = getattr(source, "device_fingerprint_sha256", None)
        if isinstance(candidate_device, str) and SHA256_RE.fullmatch(candidate_device):
            device_fingerprint_sha256 = candidate_device
    thresholds = VC003LiveMarkerThresholds(
        bucket_count=BUCKET_COUNT,
        bucket_duration_ns=BUCKET_DURATION_NS,
        max_age_ns=marker.max_age_ns if marker.max_age_ns is not None else MAX_AGE_NS,
        generation=GENERATION,
    )
    from maple_automation_core.replay.vc003_live_marker import (
        FixedBucketSelector,  # local to keep API bridge optional
    )

    started_here = False
    is_running = getattr(source, "is_running", False)
    if callable(is_running):
        is_running = is_running()
    if callable(getattr(source, "start", None)) and not bool(is_running):
        try:
            source.start()
        except Exception:
            # A backend may have allocated a handle or worker before its
            # start routine reports failure.  Give it one fail-closed cleanup
            # opportunity without masking the original startup exception.
            with contextlib.suppress(Exception):
                source.stop()
            raise
        started_here = True
    try:
        # Production anchors warm-up only after the capture source is fully
        # open.  Keep this inside the lifecycle guard because an injected
        # clock can still fail after the backend has started.
        anchor = int(now() if measurement_start_ns is None else measurement_start_ns)
        if anchor < 0:
            raise VC003RunError("measurement_start_ns must be non-negative")
        start = anchor + warmup_seconds * 1_000_000_000
        end = start + measurement_seconds * 1_000_000_000
        selector = FixedBucketSelector(start_at_ns=start)
        integration = VC003LiveMarkerRunner(
            source,
            source_config=source_config,
            marker_config=marker,
            pixel_store=memory_cas,
            retained_store=retained_store,
            memory_cas=memory_cas,
            selector=selector,
            thresholds=thresholds,
            clock=now,
        )
    except Exception:
        if started_here and callable(getattr(source, "stop", None)):
            with contextlib.suppress(Exception):
                source.stop()
        raise
    admissions: list[FrameAdmissionResult] = []
    stop_ok = True
    try:
        # Admit warm-up frames through the same source/adapter path.  The
        # fixed selector origin keeps them out of the selected 100 buckets.
        iterations = 0
        poll_period_s = 1.0 / float(target_admission_hz)
        next_poll_wall = time.monotonic()
        while iterations < max_iterations:
            current = int(now())
            if current >= end:
                break
            if clock is None:
                delay = next_poll_wall - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                if int(now()) >= end:
                    break
                # VC003LiveMarkerRunner samples the admission clock after the
                # blocking read, eliminating captured>received races.
                admission, _ = integration.poll(timeout_s=float(poll_timeout_seconds))
                next_poll_wall += poll_period_s
                if next_poll_wall < time.monotonic() - poll_period_s:
                    next_poll_wall = time.monotonic()
            else:
                admission, _ = integration.poll(now_ns=current, timeout_s=0.0)
            observed_at_ns = admission.event.observed_at_ns
            if start <= observed_at_ns < end:
                admissions.append(admission)
            iterations += 1
            # A finite injected source may return no frames forever.  Let the
            # fake clock decide completion; real time is bounded by end.
        if iterations >= max_iterations:
            raise VC003RunError("measurement loop exceeded max_iterations")
    except Exception as exc:
        stop_ok = False
        extra = [f"measurement_{type(exc).__name__}"]
    else:
        extra = []
    finally:
        if started_here and callable(getattr(source, "stop", None)):
            try:
                source.stop()
            except Exception:
                stop_ok = False
    selections = () if integration.selector is None else tuple(integration.selector.selected)
    private_rows = [
        _private_row(
            selection,
            row,
            result,
            device_fingerprint_sha256,
            private_rows_ref,
        )
        for selection, row, result in zip(
            selections,
            integration.rows,
            integration.results,
            strict=False,
        )
    ]
    private_sha: str | None = None
    private_count = 0
    if private_rows_path is not None:
        target = _lexical_safe_path(private_rows_path)
        _write_jsonl(target, private_rows)
        private_sha = sha256_file(target)
        private_count = len(private_rows)
    elif private_rows:
        # Do not expose the restricted rows in the public return object.  A
        # run without an explicit destination remains execution-invalid.
        private_count = 0
    if accepted_ledger_path is not None:
        _write_jsonl(accepted_ledger_path, _accepted_ledger_rows(admissions))
    config_payload: Mapping[str, Any]
    if config is None:
        config_path = CONFIG_PATH
        if config_path.is_file():
            config_payload = load_strict_json(config_path)
        else:
            config_payload = {
                "schema_version": SCHEMA_VERSION,
                "scope": SCOPE,
                "truth_scope": TRUTH_SCOPE,
                "generation": GENERATION,
                "expected_bindings": {},
            }
    else:
        config_payload = config
    if source_commit is None:
        source_commit = _git_head(ROOT)
    if config_sha256 is None:
        config_sha256 = (
            sha256_file(CONFIG_PATH)
            if config is None and CONFIG_PATH.is_file()
            else _canonical_digest(config_payload)
        )
    report = _build_report(
        config=config_payload,
        config_sha256=_require_sha(config_sha256, "config_sha256"),
        source_commit=_require_commit(source_commit),
        source_config=source_cfg,
        source=source,
        admissions=admissions,
        integration=integration,
        measurement_start_ns=start,
        measurement_end_ns=end,
        warmup_seconds=warmup_seconds,
        measurement_seconds=measurement_seconds,
        private_rows_ref=private_rows_ref,
        private_rows_sha256=private_sha,
        private_rows_count=private_count,
        run_index=run_index,
        device_fingerprint_sha256=device_fingerprint_sha256,
        cleanup=_cleanup_payload(source, stop_ok, private_sha is not None),
        binding_errors=binding_errors,
        extra_failures=extra,
        external_artifacts=external_artifacts,
    )
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ledger", "--public-ledger", dest="ledger", type=Path)
    parser.add_argument(
        "--accepted-ledger",
        "--accepted-frame-ledger",
        dest="accepted_ledger",
        type=Path,
        required=True,
        help="Restricted accepted-frame ledger JSONL used by the independent verifier.",
    )
    parser.add_argument(
        "--private-cas-root",
        "--cas-root",
        dest="private_cas_root",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--private-rows",
        "--restricted-rows",
        dest="private_rows",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--source-commit",
        "--replay-source-commit",
        dest="source_commit",
        required=True,
    )
    parser.add_argument("--wheel", type=Path, default=DEFAULT_WHEEL_PATH)
    parser.add_argument(
        "--lock",
        "--dependency-lock",
        dest="lock",
        type=Path,
        default=DEFAULT_LOCK_PATH,
    )
    parser.add_argument(
        "--device-environment",
        "--device-env",
        dest="device_environment",
        type=Path,
    )
    parser.add_argument("--expected-wheel-sha256", type=str, default=EXPECTED_B2_WHEEL_SHA256)
    parser.add_argument("--expected-lock-sha256", type=str, default=EXPECTED_B2_LOCK_SHA256)
    parser.add_argument("--expected-device-env-sha256", type=str)
    parser.add_argument("--upstream-b2-packet", type=Path)
    parser.add_argument("--loc003b-report", type=Path)
    parser.add_argument("--base-marker-config", type=Path)
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--extractor-source", "--marker-source", dest="extractor_source", type=Path)
    parser.add_argument("--binding", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        source_commit = _validate_checkout(args.source_commit, allow_dirty=args.allow_dirty)
        config_path = _require_file(args.config, "config")
        config = load_strict_json(config_path)
        _production_config(config)
        report_path = _lexical_safe_path(args.report)
        private_cas_root = _require_dir(args.private_cas_root, "private CAS root")
        _require_fresh_private_cas(private_cas_root)
        private_rows_path = _lexical_safe_path(args.private_rows)
        ledger_path = None if args.ledger is None else _lexical_safe_path(args.ledger)
        accepted_ledger_path = (
            None if args.accepted_ledger is None else _lexical_safe_path(args.accepted_ledger)
        )
        binding_paths = _binding_paths_from_args(args)
        binding_paths.setdefault("upstream_b2_packet", DEFAULT_B2_PACKET_PATH)
        binding_paths.setdefault("loc003b_report_raw", DEFAULT_LOC003B_REPORT_PATH)
        binding_paths.setdefault("base_marker_config_raw", DEFAULT_MARKER_CONFIG_PATH)
        binding_paths.setdefault("calibration", DEFAULT_B2_PROVENANCE_PATH)
        extractor_source = inspect.getsourcefile(MinimapMarkerExtractor)
        if extractor_source is not None:
            binding_paths.setdefault("extractor", Path(extractor_source).resolve())
        # The source provenance artifact is the de-identified device
        # environment binding when no separate environment file is supplied.
        if args.device_environment is None and DEFAULT_B2_PROVENANCE_PATH.is_file():
            binding_paths["device_environment"] = DEFAULT_B2_PROVENANCE_PATH
        expected_external: dict[str, str] = {
            "wheel": _require_sha(args.expected_wheel_sha256, "--expected-wheel-sha256"),
            "dependency_lock": _require_sha(args.expected_lock_sha256, "--expected-lock-sha256"),
        }
        if args.expected_device_env_sha256 is not None:
            expected_external["device_environment"] = _require_sha(
                args.expected_device_env_sha256, "--expected-device-env-sha256"
            )
        elif args.device_environment is None:
            expected_external["device_environment"] = EXPECTED_B2_DEVICE_ENV_SHA256
        else:
            raise VC003RunError(
                "a custom --device-environment requires --expected-device-env-sha256"
            )
        binding_errors = verify_external_bindings(config, binding_paths, expected=expected_external)
        if binding_errors:
            raise VC003RunError("external binding preflight failed: " + ",".join(binding_errors))
        raw_device_id = os.environ.get(DEVICE_INSTANCE_ENV, "")
        device_index, device_digest = _preflight_device("VC-003 Video", raw_device_id)
        source_config = VC003SourceConfig(
            source_id="capture-card-primary",
            session_id=f"vc003-live-{int(time.time())}",
            clock_domain="monotonic",
            transform_version="capture-v1",
            device_name="VC-003 Video",
            device_index=device_index,
            backend="dshow",
            width=1920,
            height=1080,
            fps=30.0,
            pixel_format="mjpg",
        )
        device_environment_path = binding_paths.get("device_environment")
        if device_environment_path is None:
            raise VC003RunError("device environment binding is required")
        device_environment = load_strict_json(device_environment_path)
        provenance = _build_live_provenance(
            source_config,
            device_environment,
            device_fingerprint_sha256=device_digest,
            source_commit=source_commit,
            dependency_lock_sha256=expected_external["dependency_lock"],
            source_artifact_sha256=expected_external["wheel"],
        )
        live_cas = CapacityOneMemoryCAS()

        def backend_factory(bound_config: VC003SourceConfig) -> ControlledOpenCVBackend:
            return ControlledOpenCVBackend(
                bound_config,
                fingerprint_sha256=device_digest,
            )

        source = VC003Source(
            source_config,
            backend_factory=backend_factory,
            pixel_store=live_cas,
            provenance=provenance,
        )
        retained_store = PixelStore(private_cas_root)
        # The production window is fixed by _production_config; this call is
        # deliberately not parameterised by CLI timing values.
        external_artifacts: list[dict[str, Any]] = []
        for role, key, privacy, retention in (
            ("wheel", "wheel", "public_hash_only", "persistent"),
            ("dependency_lock", "dependency_lock", "public_hash_only", "persistent"),
            ("device_environment", "device_environment", "restricted", "persistent"),
        ):
            value = binding_paths.get(key)
            if value is None:
                continue
            expected_value = expected_external.get(key)
            if expected_value is None:
                try:
                    expected_value = sha256_file(value)
                except VC003RunError:
                    continue
            external_artifacts.append(
                {
                    "artifact_id": f"vc003-{role}",
                    "role": role,
                    "external_ref": _external_ref(value, role),
                    "sha256": expected_value,
                    "size_bytes": value.stat().st_size,
                    "privacy_class": privacy,
                    "retention_class": retention,
                }
            )
        report = run_measurement(
            source=source,
            source_config=source_config,
            retained_store=retained_store,
            memory_cas=live_cas,
            warmup_seconds=30,
            measurement_seconds=300,
            target_admission_hz=TARGET_ADMISSION_HZ,
            poll_timeout_seconds=POLL_TIMEOUT_SECONDS,
            config=config,
            config_sha256=sha256_file(config_path),
            source_commit=source_commit,
            private_rows_path=private_rows_path,
            private_rows_ref=_external_ref(private_rows_path, "restricted-verifier-rows"),
            accepted_ledger_path=accepted_ledger_path,
            device_fingerprint_sha256=device_digest,
            binding_errors=binding_errors,
            external_artifacts=external_artifacts,
        )
        if accepted_ledger_path is None:
            raise VC003RunError("accepted ledger path is required")
        report["artifacts"].append(
            {
                "artifact_id": "vc003-accepted-frame-ledger",
                "role": "accepted_frame_ledger",
                "external_ref": _external_ref(
                    accepted_ledger_path,
                    "accepted-frame-ledger",
                ),
                "sha256": sha256_file(accepted_ledger_path),
                "size_bytes": accepted_ledger_path.stat().st_size,
                "privacy_class": "public_hash_only",
                "retention_class": "candidate",
            }
        )
        if ledger_path is not None:
            _write_jsonl(
                ledger_path,
                cast(Sequence[Mapping[str, Any]], report["public_selected_rows"]),
            )
        report["report_digest"] = _canonical_digest(
            {key: value for key, value in report.items() if key != "report_digest"}
        )
        _write_atomic_json(report_path, report)
        print(canonical_json(report).decode("utf-8"))
        return 0 if report.get("status") == "PASS" and not binding_errors else 1
    except (OSError, TypeError, ValueError, VC003RunError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BUCKET_COUNT",
    "BUCKET_DURATION_NS",
    "ReadOnlyPixelStore",
    "VC003RunError",
    "_canonical_digest",
    "_parse_args",
    "_validate_checkout",
    "_write_atomic_json",
    "load_strict_json",
    "main",
    "run_measurement",
    "sha256_file",
    "verify_external_bindings",
]
