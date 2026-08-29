"""Run the offline G1 installed-wheel smoke.

The smoke intentionally exercises only synthetic bytes and a deterministic
fake backend.  It imports NumPy/OpenCV to prove the locked runtime is present,
but never opens a device, connects an input receiver, or writes to a window.
``--require-installed`` rejects a checkout import and is the mode used after
installing the wheel into a clean Python 3.12 environment.
``--runtime-only`` keeps that clean environment limited to the G1 runtime
lock; the development-environment corpus/provenance audit remains separate.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_NUMPY = "2.1.3"
EXPECTED_OPENCV_DISTRIBUTION = "4.10.0.84"
EXPECTED_OPENCV_MODULE = "4.10.0"
EXPECTED_G1_LOCK = "g1-frame-requirements.lock"
EXPECTED_ARTIFACT_HASHES = {
    "numpy": "0d30c543f02e84e92c4b1f415b7c6b5326cbe45ee7882b6b77db7195fb971e3a",
    "opencv-python-headless": "afcf28bd1209dd58810d33defb622b325d3cbe49dcd7a43a902982c33e5fad05",
}
FULL_SIZE_ZERO_DIGEST = "c23a85d7fe7002f426293d40fb9a02a8795c41f7ef7ea801b082a969793ab4bc"
SMOKE_SCHEMA_VERSION = "1.0.0"
_LOCK_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)==(?P<version>[^\s\\]+)\s*\\?$"
)
_LOCK_HASH = re.compile(r"^--hash=sha256:(?P<digest>[a-f0-9]{64})$")
_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s\"']+[\\/]|/(?:[^/\s\"']+/)+)[^\s\"']*"
)


class SmokeFailure(RuntimeError):
    """Raised for a deterministic smoke contract failure."""


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _path_basename(value: str) -> str:
    """Return a path basename without preserving drive, share, or directory."""

    stripped = value.rstrip(".,:;)]}>\"'")
    normalized = stripped.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    return basename or "<path>"


def _sanitize_text(value: str) -> str:
    """Remove absolute paths from tool output while retaining basenames."""

    return _ABSOLUTE_PATH.sub(lambda match: _path_basename(match.group(0)), value)


def _sanitize_command(command: Sequence[str]) -> list[str]:
    """Keep command identity/options and reduce every path argument to a basename."""

    result: list[str] = []
    for token in command:
        if Path(token).name.casefold() in {"python", "python.exe"}:
            result.append("python")
        elif "/" in token or "\\" in token or re.match(r"^[A-Za-z]:", token):
            result.append(_path_basename(token))
        else:
            result.append(token)
    return result


def _load_runtime(require_installed: bool, *, checkout_root: Path = ROOT) -> tuple[Any, Path]:
    """Import the runtime and return its resolved package file.

    Import is deliberately delayed until this check so a wheel smoke can
    distinguish an installed distribution from the checkout's ``src`` tree.
    """

    try:
        package = importlib.import_module("maple_automation_core")
    except ModuleNotFoundError:
        if require_installed:
            raise SmokeFailure("runtime package is not installed") from None
        sys.path.insert(0, str(checkout_root.resolve() / "src"))
        package = importlib.import_module("maple_automation_core")

    package_file = getattr(package, "__file__", None)
    if not isinstance(package_file, str) or not package_file:
        raise SmokeFailure("runtime package has no importable __file__")
    package_path = Path(package_file).resolve()
    if require_installed and package_path.is_relative_to(checkout_root.resolve()):
        raise SmokeFailure(
            f"runtime package resolved from checkout: {_path_basename(str(package_path))}"
        )
    return package, package_path


def _distribution_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _check_capture_dependencies(*, require_exact: bool) -> dict[str, Any]:
    """Import the locked numerical/image dependencies without device access."""

    try:
        numpy = importlib.import_module("numpy")
        cv2 = importlib.import_module("cv2")
    except ImportError as exc:
        raise SmokeFailure(f"capture dependency import failed: {exc}") from exc

    numpy_module = str(getattr(numpy, "__version__", ""))
    cv2_module = str(getattr(cv2, "__version__", ""))
    numpy_distribution = _distribution_version("numpy")
    opencv_distribution = _distribution_version("opencv-python-headless")
    observed: dict[str, Any] = {
        "numpy": {
            "distribution_version": numpy_distribution,
            "module_version": numpy_module,
        },
        "opencv_python_headless": {
            "distribution_version": opencv_distribution,
            "module_version": cv2_module,
        },
    }
    if require_exact:
        mismatches: list[str] = []
        if numpy_distribution != EXPECTED_NUMPY or numpy_module != EXPECTED_NUMPY:
            mismatches.append(
                f"NumPy expected {EXPECTED_NUMPY}, distribution={numpy_distribution!r}, "
                f"module={numpy_module!r}"
            )
        if (
            opencv_distribution != EXPECTED_OPENCV_DISTRIBUTION
            or cv2_module != EXPECTED_OPENCV_MODULE
        ):
            mismatches.append(
                f"OpenCV expected {EXPECTED_OPENCV_DISTRIBUTION}/{EXPECTED_OPENCV_MODULE}, "
                f"distribution={opencv_distribution!r}, module={cv2_module!r}"
            )
        if mismatches:
            raise SmokeFailure("; ".join(mismatches))
    observed["exact_pin_required"] = require_exact
    return observed


def _parse_g1_lock(path: Path) -> dict[str, dict[str, Any]]:
    """Parse the small hashed G1 runtime lock without invoking pip/network."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SmokeFailure("G1 runtime lock cannot be read") from exc
    entries: dict[str, dict[str, Any]] = {}
    current: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lock_hash = _LOCK_HASH.fullmatch(line)
        if lock_hash is not None and current is not None:
            cast_hashes = entries[current]["hashes"]
            if not isinstance(cast_hashes, list):
                raise SmokeFailure(f"malformed G1 lock hashes: {current}")
            cast_hashes.append(lock_hash.group("digest"))
            continue
        if line.startswith("--"):
            continue
        requirement = _LOCK_REQUIREMENT.fullmatch(line)
        if requirement is not None:
            name = requirement.group("name").casefold().replace("_", "-")
            if name in entries:
                raise SmokeFailure(f"duplicate G1 lock requirement: {name}")
            entries[name] = {
                "version": requirement.group("version"),
                "hashes": [],
            }
            current = name
            continue
        raise SmokeFailure("G1 runtime lock contains an unsupported line")
    if set(entries) != set(EXPECTED_ARTIFACT_HASHES):
        raise SmokeFailure("G1 runtime lock must contain exactly NumPy and OpenCV headless")
    for name, expected_hash in EXPECTED_ARTIFACT_HASHES.items():
        entry = entries[name]
        if (
            entry["version"]
            != {
                "numpy": EXPECTED_NUMPY,
                "opencv-python-headless": EXPECTED_OPENCV_DISTRIBUTION,
            }[name]
        ):
            raise SmokeFailure(f"G1 lock version mismatch: {name}")
        if entry["hashes"] != [expected_hash]:
            raise SmokeFailure(f"G1 lock artifact hash mismatch: {name}")
    return entries


def _pixel_cas_smoke(pixel_store_module: Any, cas_root: Path) -> dict[str, Any]:
    spec = pixel_store_module.PixelSpec()
    numpy = importlib.import_module("numpy")
    array = numpy.zeros(spec.shape, dtype=numpy.uint8)
    if not bool(array.flags.c_contiguous):
        raise SmokeFailure("NumPy zero fixture is not C-contiguous")
    digest = pixel_store_module.pixel_digest(spec, array)
    if digest != FULL_SIZE_ZERO_DIGEST:
        raise SmokeFailure(f"full-size Pixel V1 KAT mismatch: {digest}")

    pixels = array.tobytes(order="C")
    store = pixel_store_module.PixelStore(cas_root)
    artifact = store.put_artifact(
        spec,
        pixels,
        privacy_class="restricted",
        retention_class="candidate",
        source_provenance_id="g1-installed-smoke",
        session_id="synthetic-session",
        source_sequence=1,
    )
    resolved = store.read(artifact.pixel_digest, spec)
    if resolved != pixels or artifact.pixel_digest != digest:
        raise SmokeFailure("Pixel CAS round-trip mismatch")
    encoded_hash = sha256(pixels).hexdigest()
    if artifact.encoded_sha256 != encoded_hash:
        raise SmokeFailure("Pixel CAS encoded hash mismatch")
    return {
        "fixture": "full_size_zero_bgr8_v1",
        "spec": spec.to_dict(),
        "byte_length": len(pixels),
        "pixel_digest": digest,
        "cas_ref": artifact.ref,
        "copy_verified": True,
        "hash_verified": True,
        "cas_put_get_verified": True,
        "encoded_sha256": encoded_hash,
    }


class _FakeBackend:
    """Finite deterministic backend used by the lifecycle smoke."""

    def __init__(self, frames: Sequence[bytes]) -> None:
        self._frames = list(frames)
        self._stop_event = threading.Event()
        self.start_calls = 0
        self.read_calls = 0
        self.stop_calls = 0
        self.negotiated_facts: Any = None
        self.device_fingerprint_sha256: Any = None

    @property
    def device_name(self) -> str:
        return "VC-003 Video"

    def start(self) -> None:
        self.start_calls += 1

    def read(self) -> bytes | None:
        self.read_calls += 1
        if self._frames:
            return self._frames.pop(0)
        self._stop_event.wait(0.002)
        return None

    def stop(self) -> None:
        self.stop_calls += 1
        self._stop_event.set()


def _wait_for_pending(source: Any, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        status = source.status()
        if status.pending == 1:
            return
        if status.lifecycle == "error":
            raise SmokeFailure(f"fake VC-003 source entered error: {status.error}")
        if time.monotonic() >= deadline:
            raise SmokeFailure("fake VC-003 source did not publish a pending frame")
        time.sleep(0.001)


def _vc_lifecycle_smoke(vc_module: Any) -> dict[str, Any]:
    config = vc_module.VC003SourceConfig(
        source_id="capture-card-primary",
        session_id="synthetic-vc003-session",
        device_name="VC-003 Video",
        backend="dshow",
        width=2,
        height=1,
        fps=30.0,
        poll_interval_s=0.0001,
    )
    backend = _FakeBackend([bytes((1, 2, 3, 4, 5, 6))])
    pixel_store_module = importlib.import_module("maple_automation_core.capture.pixel_store")
    backend.device_fingerprint_sha256 = pixel_store_module.UNKNOWN_DEVICE_FINGERPRINT_SHA256
    backend.negotiated_facts = vc_module.NegotiatedCaptureFacts(
        width=2,
        height=1,
        fps=30.0,
        fourcc="MJPG",
        backend="dshow",
        backend_api="dshow",
        backend_version="fake-v1",
    )
    source = vc_module.VC003Source(config, backend=backend)
    started = False
    try:
        source.start()
        started = True
        _wait_for_pending(source)
        sample = source.read(timeout_s=1.0)
        if sample is None:
            raise SmokeFailure("fake VC-003 source returned no sample")
        if source.pixel_store.read(sample.content_hash, sample.spec) != sample.raw_bytes:
            raise SmokeFailure("fake VC-003 source CAS verification failed")
        source.stop()
        started = False
        status = source.status()
    finally:
        if started:
            source.stop()

    if backend.start_calls != 1 or backend.stop_calls != 1:
        raise SmokeFailure(
            f"fake backend lifecycle mismatch: start={backend.start_calls}, "
            f"stop={backend.stop_calls}"
        )
    if status.lifecycle != "stopped" or status.thread_alive:
        raise SmokeFailure("fake VC-003 source did not stop cleanly")
    if not status.accounting_holds or status.max_depth != 1 or status.pending != 0:
        raise SmokeFailure("raw latest accounting invariant failed")
    return {
        "lifecycle": status.lifecycle,
        "produced": status.produced,
        "delivered": status.delivered,
        "superseded": status.superseded,
        "pending": status.pending,
        "discarded_on_reset": status.discarded_on_reset,
        "max_depth": status.max_depth,
        "accounting_holds": status.accounting_holds,
        "last_sequence": status.last_sequence,
        "last_delivered_sequence": status.last_delivered_sequence,
        "fake_backend": {
            "start_calls": backend.start_calls,
            "read_calls": backend.read_calls,
            "stop_calls": backend.stop_calls,
        },
        "real_device_opened": False,
    }


def _clean_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    env.update({"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
    return env


def _run_command(command: list[str], *, cwd: Path, timeout_s: float = 120.0) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=_clean_subprocess_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure(
            f"command exceeded {timeout_s:.0f}s: {_sanitize_text(command[0])}"
        ) from exc
    return {
        "command": _sanitize_command(command),
        "exit_code": completed.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "stdout_tail": _sanitize_text(completed.stdout[-2000:]),
        "stderr_tail": _sanitize_text(completed.stderr[-2000:]),
    }


def _corpus_audit_smoke(
    repo_root: Path, work_root: Path, *, require_installed: bool
) -> dict[str, Any]:
    fixture_root = repo_root / "fixtures" / "g1" / "frame_corpus_synthetic_v1"
    tools_root = repo_root / "tools"
    plan = fixture_root / "import-plan.json"
    if not plan.is_file():
        raise SmokeFailure(f"synthetic corpus plan is missing: {_path_basename(str(plan))}")
    output_root = work_root / "corpus"
    cas_root = work_root / "private-cas"
    manifest = output_root / "frame-corpus-manifest.json"
    report = work_root / "provenance-report.json"
    installed_flag = ["--require-installed"] if require_installed else []

    import_command = [
        sys.executable,
        str(tools_root / "import_frame_corpus.py"),
        "--plan",
        str(plan),
        "--output-root",
        str(output_root),
        "--cas-root",
        str(cas_root),
        *installed_flag,
    ]
    import_result = _run_command(import_command, cwd=repo_root)
    if import_result["exit_code"] != 0 or not manifest.is_file():
        raise SmokeFailure(f"synthetic corpus import failed: {import_result}")

    try:
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure(
            f"imported corpus manifest is not strict JSON: {_path_basename(str(manifest))}"
        ) from exc
    event_tape_module = importlib.import_module("maple_automation_core.replay.event_tape")
    event_tape_paths: list[Path] = []
    for index, sample in enumerate(manifest_payload.get("samples", [])):
        tape_path = work_root / f"event-tape-{index}.jsonl"
        tape = event_tape_module.EventTape(tape_path)
        truth_path = output_root / sample["truth_path"]
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        expected_admission = truth["expected_admission"]
        expected_status = truth["expected_status"]
        expected_reason_code = truth["expected_reason_code"]
        if expected_admission == "fatal":
            event_type = "frame.fatal"
            admission_status = expected_status
            plan_suppressed = True
            fault_latched = True
            pixel_digest = None
            image_ref = None
            reason = "synthetic fatal fixture"
        else:
            event_type = "frame.accepted"
            admission_status = expected_status
            plan_suppressed = False
            fault_latched = False
            pixel_digest = sample["pixel_digest"]
            image_ref = sample["cas_ref"]
            reason = "synthetic accepted fixture"
        tape.append(
            event_type=event_type,
            payload={
                "truth_scope": "frame_ingestion_only",
                "truth_id": sample["truth_id"],
                "truth_pixel_digest": sample["pixel_digest"],
                "admission_status": admission_status,
                "plan_suppressed": plan_suppressed,
                "fault_latched": fault_latched,
                "pixel_digest": pixel_digest,
                "image_ref": image_ref,
                "reason": reason,
                "reason_code": expected_reason_code,
            },
            session_id=sample["session_id"],
            frame_id=sample["sequence"],
            world_state_version=0,
            recorded_at_ns=100 + index,
        )
        event_tape_paths.append(tape_path)
    event_tape_args = [
        item for tape_path in event_tape_paths for item in ("--event-tape", str(tape_path))
    ]

    audit_command = [
        sys.executable,
        str(tools_root / "audit_frame_provenance.py"),
        "--manifest",
        str(manifest),
        "--truth-root",
        str(output_root),
        "--cas-root",
        str(cas_root),
        "--report",
        str(report),
        "--generated-at",
        "2026-08-29T00:00:00Z",
        *event_tape_args,
        *installed_flag,
    ]
    audit_result = _run_command(audit_command, cwd=repo_root)
    if audit_result["exit_code"] != 0 or not report.is_file():
        raise SmokeFailure(f"synthetic provenance audit failed: {audit_result}")
    try:
        audit_payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure(
            f"synthetic provenance report is not strict JSON: {_path_basename(str(report))}"
        ) from exc
    input_audit = audit_payload.get("input_audit")
    expected_input = {
        "input_owner": "legacy",
        "connected": False,
        "real_input_call_count": 0,
        "receiver_connect_count": 0,
        "window_write_count": 0,
        "double_write_event_count": 0,
    }
    if audit_payload.get("status") != "PASS" or input_audit != expected_input:
        raise SmokeFailure("synthetic provenance audit status/input audit mismatch")
    return {
        "manifest": manifest.name,
        "manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
        "provenance_report": report.name,
        "provenance_report_sha256": sha256(report.read_bytes()).hexdigest(),
        "event_tapes": [path.name for path in event_tape_paths],
        "import": import_result,
        "audit": audit_result,
        "audit_status": audit_payload["status"],
        "sample_count": audit_payload["corpus"]["sample_count"],
        "event_count": audit_payload["event_tape"]["event_count"],
        "input_audit": input_audit,
    }


def _lock_smoke(repo_root: Path, *, require_installed: bool) -> dict[str, Any]:
    lock_path = repo_root / "configs" / EXPECTED_G1_LOCK
    entries = _parse_g1_lock(lock_path)
    if require_installed:
        missing_or_mismatched: list[str] = []
        for name, expected_hash in EXPECTED_ARTIFACT_HASHES.items():
            expected_version = {
                "numpy": EXPECTED_NUMPY,
                "opencv-python-headless": EXPECTED_OPENCV_DISTRIBUTION,
            }[name]
            actual = _distribution_version(name)
            if actual != expected_version:
                missing_or_mismatched.append(f"{name}={actual!r} (expected {expected_version})")
            if expected_hash not in entries[name]["hashes"]:
                raise SmokeFailure(f"G1 lock artifact hash mismatch: {name}")
        if missing_or_mismatched:
            raise SmokeFailure("G1 runtime package mismatch: " + ", ".join(missing_or_mismatched))
    return {
        "lock_id": EXPECTED_G1_LOCK,
        "lock_sha256": sha256(lock_path.read_bytes()).hexdigest(),
        "requirements": {
            name: {
                "version": entry["version"],
                "artifact_sha256": entry["hashes"][0],
            }
            for name, entry in sorted(entries.items())
        },
        "installed_versions_checked": require_installed,
    }


def _privacy_scan(payload: dict[str, Any]) -> dict[str, Any]:
    corpus_module = importlib.import_module("maple_automation_core.replay.frame_corpus")
    summary = corpus_module.public_privacy_summary(payload, [])
    if summary["pii_findings"] != 0:
        raise SmokeFailure("installed smoke report contains a privacy finding")
    return {
        "status": "PASS",
        "pii_findings": summary["pii_findings"],
        "scan_contract_sha256": summary["scan_contract_sha256"],
        "scan_digest": summary["scan_digest"],
    }


def _input_audit() -> dict[str, Any]:
    return {
        "input_owner": "legacy",
        "real_input_enabled": False,
        "real_input_call_count": 0,
        "receiver_connect_count": 0,
        "window_write_count": 0,
        "keyboard_event_count": 0,
        "mouse_event_count": 0,
        "double_write_event_count": 0,
    }


def run_smoke(
    *,
    repo_root: Path = ROOT,
    require_installed: bool = False,
    runtime_only: bool = False,
) -> dict[str, Any]:
    """Run all offline checks and return a JSON-serializable evidence report."""

    root = repo_root.resolve()
    report: dict[str, Any] = {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "smoke_id": "g1-installed-wheel-smoke-v1",
        "generated_at": _timestamp(),
        "require_installed": require_installed,
        "runtime_only": runtime_only,
        "scope": "runtime-only" if runtime_only else "full-offline",
        "status": "FAIL",
        "failures": [],
        "checks": [],
        "limitations": [
            "Synthetic bytes and deterministic fake backend only; no VC-003 device is opened.",
            "This smoke does not establish hardware throughput or freshness thresholds.",
        ],
        "input_audit": _input_audit(),
    }
    if runtime_only:
        report["limitations"].append(
            "Runtime-only profile skips corpus/provenance tool subprocesses; "
            "those run in the development environment."
        )
    failures = report["failures"]
    checks = report["checks"]

    try:
        _, package_path = _load_runtime(require_installed, checkout_root=root)
        report["runtime"] = {
            "package_file": package_path.name,
            "package_origin": "checkout" if package_path.is_relative_to(root) else "installed",
        }
        checks.append({"name": "runtime-import", "status": "PASS"})
    except Exception as exc:
        failures.append(f"runtime-import: {type(exc).__name__}: {_sanitize_text(str(exc))}")
        checks.append({"name": "runtime-import", "status": "FAIL"})
        report["canonical_report_sha256"] = sha256(
            _canonical_json({key: value for key, value in report.items()})
        ).hexdigest()
        return report

    try:
        report["dependencies"] = _check_capture_dependencies(require_exact=require_installed)
        checks.append({"name": "capture-dependencies", "status": "PASS"})
    except Exception as exc:
        failures.append(f"capture-dependencies: {type(exc).__name__}: {_sanitize_text(str(exc))}")
        checks.append({"name": "capture-dependencies", "status": "FAIL"})

    with tempfile.TemporaryDirectory(prefix="maple-g1-installed-smoke-") as temporary:
        work_root = Path(temporary)
        pixel_module = importlib.import_module("maple_automation_core.capture.pixel_store")
        vc_module = importlib.import_module("maple_automation_core.capture.vc003_source")
        try:
            report["pixel_cas"] = _pixel_cas_smoke(pixel_module, work_root / "pixel-cas")
            checks.append({"name": "pixel-kat-cas", "status": "PASS"})
        except Exception as exc:
            failures.append(f"pixel-kat-cas: {type(exc).__name__}: {_sanitize_text(str(exc))}")
            checks.append({"name": "pixel-kat-cas", "status": "FAIL"})
        try:
            report["vc003_fake_lifecycle"] = _vc_lifecycle_smoke(vc_module)
            checks.append({"name": "vc003-fake-start-read-stop", "status": "PASS"})
        except Exception as exc:
            failures.append(
                f"vc003-fake-start-read-stop: {type(exc).__name__}: {_sanitize_text(str(exc))}"
            )
            checks.append({"name": "vc003-fake-start-read-stop", "status": "FAIL"})
        try:
            report["dependency_lock"] = _lock_smoke(root, require_installed=require_installed)
            checks.append({"name": "dependency-lock", "status": "PASS"})
        except Exception as exc:
            failures.append(f"dependency-lock: {type(exc).__name__}: {_sanitize_text(str(exc))}")
            checks.append({"name": "dependency-lock", "status": "FAIL"})
        if runtime_only:
            checks.append(
                {
                    "name": "corpus-audit-installed-import",
                    "status": "SKIPPED",
                    "reason": "runtime-only profile; development audit runs separately",
                }
            )
        else:
            try:
                report["corpus_audit"] = _corpus_audit_smoke(
                    root,
                    work_root,
                    require_installed=require_installed,
                )
                checks.append({"name": "corpus-audit-installed-import", "status": "PASS"})
            except Exception as exc:
                failures.append(
                    "corpus-audit-installed-import: "
                    f"{type(exc).__name__}: {_sanitize_text(str(exc))}"
                )
                checks.append({"name": "corpus-audit-installed-import", "status": "FAIL"})

    try:
        report["privacy_scan"] = _privacy_scan(report)
        checks.append({"name": "privacy-scan", "status": "PASS"})
    except Exception as exc:
        failures.append(f"privacy-scan: {type(exc).__name__}: {_sanitize_text(str(exc))}")
        checks.append({"name": "privacy-scan", "status": "FAIL"})
    report["status"] = "PASS" if not failures else "FAIL"
    report["canonical_report_sha256"] = sha256(
        _canonical_json({key: value for key, value in report.items()})
    ).hexdigest()
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--require-installed",
        action="store_true",
        help="Require maple_automation_core to resolve outside this checkout and enforce pins.",
    )
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="Skip corpus/provenance tool subprocesses; use the G1 runtime lock only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_smoke(
        repo_root=args.repo_root,
        require_installed=args.require_installed,
        runtime_only=args.runtime_only,
    )
    encoded = _canonical_json(report) + b"\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_bytes(encoded)
    print(encoded.decode("utf-8"), end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SmokeFailure, ValueError) as exc:
        print(f"Error: {_sanitize_text(str(exc))}", file=sys.stderr)
        raise SystemExit(1) from exc
