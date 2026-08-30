"""Run the G1-OBS-002B offline observation smoke.

The command is deliberately narrower than a capture or control test.  It
verifies external assets by ``root environment + normalized relative id +
SHA-256``, constructs an injected or ONNX Runtime detector, and runs the same
immutable FramePacket three times.  Reports contain hashes and metadata only:
raw pixels, absolute paths, and model custom metadata stay outside the report.

The public ``run_smoke`` function accepts an ``ObservationSmokeFixture`` and a
backend injection so unit tests can exercise report semantics without a model
or a device.  With no injection the default path constructs the production
``OnnxDetectorBackend`` from the external asset roots.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Any, cast

import numpy as np
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCHEMA_VERSION = "1.0.0"
REPORT_TYPE = "g1_obs_002b_observation_smoke"
DEFAULT_SCHEMA = ROOT / "schemas" / "observation-runtime-report.schema.json"
DEFAULT_LOCK = ROOT / "configs" / "g1-observation-requirements.lock"
MODEL_SHA256 = "b279fc566c3d6f1411adedafcadb33fa48d7f2ef1a5289452bf9d5c9607004b4"
CLASSES_SHA256 = "07d524938046cff5c328f2b1b4c5b67847aae461172a954f6da19d6bf8954884"
CONFIG_SHA256 = "5d57809b0ec4f81d3a0551bcdbebd6ce296179ebdd38f87191113c0f81a017b6"
PILOT_BINDING_SHA256 = "5d19b9d3c28eab8840ee182672d8f3c1e608af56781a3a95b4d74164daa73060"
MODEL_SIZE_BYTES = 44_746_005
CLASSES_SIZE_BYTES = 13
CONFIG_SIZE_BYTES = 1_518
EXPECTED_ORT_VERSION = "1.23.2"
ORT_WHEEL_SHA256 = "25de5214923ce941a3523739d34a520aac30f21e631de53bba9174dc9c004435"
ORT_WHEEL_SIZE_BYTES = 13_470_528
ZERO_SHA256 = "0" * 64
ZERO_COMMIT = "0" * 40
RUN_COUNT = 3
MAX_LATENCY_MS = 600_000.0

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_PORTABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ORT_LOCK_RE = re.compile(
    r"(?m)^onnxruntime==(?P<version>[^\s\\]+)\s*\\\r?\n"
    r"\s*--hash=sha256:(?P<digest>[a-f0-9]{64})\s*$"
)
_ABSOLUTE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\|/(?!/))[^\r\n,;\"']*")


class ObservationSmokeError(RuntimeError):
    """Raised when a strict report or evidence boundary is violated."""


def _canonical_json(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ObservationSmokeError("report is not strict JSON") from exc


def _digest_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _digest_file(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, UnicodeError) as exc:
        raise ObservationSmokeError("evidence artifact is unavailable") from exc
    return digest.hexdigest()


def _normalise_sha256(value: object, field_name: str = "sha256") -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ObservationSmokeError(f"{field_name} must be a SHA-256 digest")
    return value.lower()


def _normalise_commit(value: object) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ObservationSmokeError("source commit must be a 40-character digest")
    return value.lower()


def _portable_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _PORTABLE_ID_RE.fullmatch(value) is None:
        raise ObservationSmokeError(f"{field_name} is not a portable identifier")
    if value.casefold() in {"current", "sealed"}:
        raise ObservationSmokeError(f"{field_name} is reserved")
    return value


def _relative_id(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or ":" in value:
        raise ObservationSmokeError("asset relative id is invalid")
    normalized = value.replace("\\", "/")
    windows = PureWindowsPath(value)
    if normalized.startswith("/") or windows.drive or windows.is_absolute():
        raise ObservationSmokeError("asset relative id must be relative")
    pieces = normalized.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces):
        raise ObservationSmokeError("asset relative id is not normalized")
    if any(ord(character) < 32 for character in normalized):
        raise ObservationSmokeError("asset relative id contains a control character")
    return normalized


def _safe_text(value: object) -> str:
    """Return path-free diagnostic text suitable for a public report."""

    text = str(value).replace("\\", "/")
    text = _ABSOLUTE_PATH_RE.sub("<path>", text)
    return text[:240] or "unknown error"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _default_evidence_id() -> str:
    return f"g1-obs-002b-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def _git_commit(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
        return _normalise_commit(completed.stdout.strip())
    except (OSError, subprocess.SubprocessError, ObservationSmokeError):
        return ZERO_COMMIT


def _verify_runtime_lock(path: Path) -> str:
    """Verify the exact ORT wheel pin and return the lock file digest."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ObservationSmokeError("observation runtime lock is unavailable") from exc
    matches = list(_ORT_LOCK_RE.finditer(text))
    if len(matches) != 1 or text.count("onnxruntime==") != 1:
        raise ObservationSmokeError("observation runtime lock is malformed")
    match = matches[0]
    if (
        match.group("version") != EXPECTED_ORT_VERSION
        or match.group("digest") != ORT_WHEEL_SHA256
        or f"# Artifact size: {ORT_WHEEL_SIZE_BYTES} bytes" not in text
    ):
        raise ObservationSmokeError("observation runtime lock does not match the frozen wheel")
    return _digest_file(path)


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """An external asset bound by environment name, relative id, and hash."""

    asset_id: str
    root_env: str
    relative_id: str
    expected_sha256: str
    expected_size_bytes: int | None = None

    def __post_init__(self) -> None:
        _portable_id(self.asset_id, "asset_id")
        if (
            not isinstance(self.root_env, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", self.root_env) is None
        ):
            raise ObservationSmokeError("asset root environment is invalid")
        object.__setattr__(self, "relative_id", _relative_id(self.relative_id))
        object.__setattr__(
            self, "expected_sha256", _normalise_sha256(self.expected_sha256, "asset hash")
        )
        if self.expected_size_bytes is not None and (
            type(self.expected_size_bytes) is not int or self.expected_size_bytes < 0
        ):
            raise ObservationSmokeError("asset size is invalid")


@dataclass(frozen=True, slots=True)
class ObservationSmokeFixture:
    """Immutable inputs for a three-run offline observation smoke."""

    frame: Any
    pixel_store: Any
    binding: Any
    preprocess_config: Any
    clock: Callable[[], int] = lambda: 1
    assets: tuple[AssetSpec, ...] = ()
    backend: Any | None = None
    _resource: Any | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        from maple_automation_core.capture.pixel_store import PixelStore
        from maple_automation_core.domain.frame import FramePacket
        from maple_automation_core.domain.observation import ModelBinding
        from maple_automation_core.vision.preprocess import PreprocessConfig

        if not isinstance(self.frame, FramePacket):
            raise TypeError("fixture.frame must be a FramePacket")
        if not isinstance(self.pixel_store, PixelStore):
            raise TypeError("fixture.pixel_store must be a PixelStore")
        if not isinstance(self.binding, ModelBinding):
            raise TypeError("fixture.binding must be a ModelBinding")
        if not isinstance(self.preprocess_config, PreprocessConfig):
            raise TypeError("fixture.preprocess_config must be a PreprocessConfig")
        if not callable(self.clock):
            raise TypeError("fixture.clock must be callable")
        if not isinstance(self.assets, tuple) or any(
            not isinstance(item, AssetSpec) for item in self.assets
        ):
            raise TypeError("fixture.assets must contain AssetSpec values")


class DeterministicFixtureBackend:
    """Small injected backend used by unit tests and the default offline fixture."""

    def __init__(
        self,
        *,
        provider: str,
        input_shape: tuple[int, ...],
        output_shape: tuple[int, ...],
        output: np.ndarray | None = None,
    ) -> None:
        self.providers = (provider,)
        self.input_name = "images"
        self.output_name = "output0"
        self.input_dtype = "tensor(float)"
        self.output_dtype = "tensor(float)"
        self.input_shape = input_shape
        self.output_shape = output_shape
        self._output = np.zeros(output_shape, dtype=np.float32) if output is None else output
        self.calls = 0

    def infer(self, tensor: np.ndarray, **kwargs: object) -> Any:
        from maple_automation_core.vision.observation_adapter import DetectorOutput

        self.calls += 1
        if tuple(tensor.shape) != self.input_shape:
            raise ValueError("fixture tensor shape mismatch")
        return DetectorOutput(
            output=np.array(self._output, dtype=np.float32, copy=True),
            provider=cast(str, kwargs["provider"]),
            input_name=cast(str, kwargs["input_name"]),
            output_name=cast(str, kwargs["output_name"]),
            input_shape=self.input_shape,
            output_shape=self.output_shape,
        )


def _default_asset_specs() -> tuple[AssetSpec, ...]:
    return (
        AssetSpec(
            "model",
            "MAPLE_MODEL_ROOT",
            "weights/best_forest_v3.onnx",
            MODEL_SHA256,
            MODEL_SIZE_BYTES,
        ),
        AssetSpec(
            "classes",
            "MAPLE_LEGACY_ROOT",
            "profiles/maple_legacy_cn/models/classes_v14_mob_only.yaml",
            CLASSES_SHA256,
            CLASSES_SIZE_BYTES,
        ),
        AssetSpec(
            "effective-config",
            "MAPLE_CORE_ROOT",
            "configs/pilot-subject-01.resolved.json",
            CONFIG_SHA256,
            CONFIG_SIZE_BYTES,
        ),
    )


def _pilot_fixture() -> tuple[ObservationSmokeFixture, tempfile.TemporaryDirectory[str]]:
    """Create a private, synthetic pilot fixture for injected/default smoke use."""

    from maple_automation_core.capture.frame_source import canonical_calibration_sha256
    from maple_automation_core.capture.pixel_store import PixelSpec, PixelStore
    from maple_automation_core.domain.frame import (
        CaptureHealth,
        FramePacket,
        FrameSize,
        SourceGeometry,
        SourceRect,
    )
    from maple_automation_core.domain.observation import ModelBinding
    from maple_automation_core.vision.preprocess import NormalizedRoi, PreprocessConfig

    resource = tempfile.TemporaryDirectory(prefix="maple-obs-002b-")
    geometry = SourceGeometry(
        source_size=FrameSize(width=1920, height=1080),
        content_rect=SourceRect(x=277, y=167, width=1366, height=768),
        working_size=FrameSize(width=1296, height=700),
    )
    preprocess = PreprocessConfig(
        geometry=geometry,
        roi=NormalizedRoi(left=0.04, top=0.0, right=0.98, bottom=0.84),
        model_size=FrameSize(width=640, height=640),
    )
    binding = ModelBinding(
        release_id="candidate-core-v2-20260829-shadow",
        model_id="best_forest_v3-candidate",
        model_sha256=MODEL_SHA256,
        classes=("mob",),
        classes_sha256=CLASSES_SHA256,
        config_sha256=CONFIG_SHA256,
        preprocess_version=preprocess.version,
        preprocess_sha256=preprocess.digest,
        input_name="images",
        input_size=preprocess.model_size,
        output_name="output0",
        output_shape=(1, 5, 8400),
        requested_providers=("CPUExecutionProvider",),
        detection_confidence=0.25,
        iou_threshold=0.45,
        roi=(0.04, 0.0, 0.98, 0.84),
    )
    spec = PixelSpec(width=1920, height=1080)
    # A deterministic private byte pattern avoids a device and avoids placing
    # raw pixels in the report.  The CAS object is removed with the fixture.
    pixels = bytes(index % 251 for index in range(spec.length))
    store = PixelStore(Path(resource.name) / "cas")
    digest = store.put(spec, pixels)
    calibration = canonical_calibration_sha256(geometry, "calibration-v1")
    frame = FramePacket(
        source_id="capture-card-primary",
        session_id="obs-002b-fixture",
        frame_id=1,
        captured_at_ns=0,
        received_at_ns=1,
        transform_version="calibration-v1",
        clock_domain="monotonic",
        content_hash=digest,
        source_geometry=geometry,
        image_ref=f"cas://sha256/{digest}",
        capture_health=CaptureHealth(
            session_id="obs-002b-fixture",
            frame_id=1,
            source_id="capture-card-primary",
            content_hash=digest,
            clock_domain="monotonic",
            captured_at_ns=0,
            received_at_ns=1,
            transform_version="calibration-v1",
            max_age_ns=100,
        ),
        image_metadata={
            "pixel_spec": spec.to_dict(),
            "calibration_sha256": calibration,
            "model_sha256": binding.model_sha256,
            "classes_sha256": binding.classes_sha256,
            "config_sha256": binding.config_sha256,
            "binding_sha256": binding.digest,
            "preprocess_sha256": binding.preprocess_sha256,
            "preprocess_version": binding.preprocess_version,
        },
    )
    fixture = ObservationSmokeFixture(
        frame=frame,
        pixel_store=store,
        binding=binding,
        preprocess_config=preprocess,
        assets=_default_asset_specs(),
        # Production ``run_smoke`` constructs the ONNX backend after the
        # external model/classes/config assets have passed their hash gate.
        backend=None,
        _resource=resource,
    )
    return fixture, resource


def make_fixture(root: Path) -> ObservationSmokeFixture:
    """Make a tiny fully verified fixture with external test assets.

    This helper is intentionally public for focused tests.  It uses a 2x2
    pixel object and a two-anchor output while retaining the same Observation
    boundary, report, hash, and privacy checks as the pilot path.
    """

    from maple_automation_core.capture.frame_source import canonical_calibration_sha256
    from maple_automation_core.capture.pixel_store import PixelSpec, PixelStore
    from maple_automation_core.domain.frame import (
        CaptureHealth,
        FramePacket,
        FrameSize,
        SourceGeometry,
        SourceRect,
    )
    from maple_automation_core.domain.observation import ModelBinding
    from maple_automation_core.vision.preprocess import NormalizedRoi, PreprocessConfig

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    model = root / "model.onnx"
    classes = root / "classes.yaml"
    config_file = root / "config.json"
    model.write_bytes(b"fixture-onnx-model-v1")
    classes.write_text("names:\n- mob\n", encoding="utf-8", newline="\n")
    config_file.write_text('{"fixture":true}\n', encoding="utf-8", newline="\n")
    model_hash = _digest_file(model)
    classes_hash = _digest_file(classes)
    config_hash = _digest_file(config_file)
    geometry = SourceGeometry(
        source_size=FrameSize(width=2, height=2),
        content_rect=SourceRect(x=0, y=0, width=2, height=2),
        working_size=FrameSize(width=4, height=4),
    )
    preprocess = PreprocessConfig(
        geometry=geometry,
        roi=NormalizedRoi(left=0.0, top=0.0, right=1.0, bottom=1.0),
        model_size=FrameSize(width=4, height=4),
    )
    binding = ModelBinding(
        release_id="g1-obs-002b-fixture",
        model_id="fixture-model",
        model_sha256=model_hash,
        classes=("mob",),
        classes_sha256=classes_hash,
        config_sha256=config_hash,
        preprocess_version=preprocess.version,
        preprocess_sha256=preprocess.digest,
        input_name="images",
        input_size=preprocess.model_size,
        output_name="output0",
        output_shape=(1, 5, 2),
        requested_providers=("CPUExecutionProvider",),
        detection_confidence=0.25,
        iou_threshold=0.45,
        roi=(0.0, 0.0, 1.0, 1.0),
    )
    spec = PixelSpec(width=2, height=2)
    store = PixelStore(root / "cas")
    digest = store.put(spec, bytes(range(spec.length)))
    calibration = canonical_calibration_sha256(geometry, "calibration-v1")
    frame = FramePacket(
        source_id="fixture-source",
        session_id="fixture-session",
        frame_id=1,
        captured_at_ns=0,
        received_at_ns=1,
        transform_version="calibration-v1",
        clock_domain="monotonic",
        content_hash=digest,
        source_geometry=geometry,
        image_ref=f"cas://sha256/{digest}",
        capture_health=CaptureHealth(
            session_id="fixture-session",
            frame_id=1,
            source_id="fixture-source",
            content_hash=digest,
            clock_domain="monotonic",
            captured_at_ns=0,
            received_at_ns=1,
            transform_version="calibration-v1",
            max_age_ns=100,
        ),
        image_metadata={
            "pixel_spec": spec.to_dict(),
            "calibration_sha256": calibration,
            "model_sha256": model_hash,
            "classes_sha256": classes_hash,
            "config_sha256": config_hash,
            "binding_sha256": binding.digest,
            "preprocess_sha256": binding.preprocess_sha256,
            "preprocess_version": binding.preprocess_version,
        },
    )
    specs = (
        AssetSpec("model", "TEST_MODEL_ROOT", "model.onnx", model_hash, model.stat().st_size),
        AssetSpec(
            "classes", "TEST_LEGACY_ROOT", "classes.yaml", classes_hash, classes.stat().st_size
        ),
        AssetSpec(
            "effective-config",
            "TEST_CORE_ROOT",
            "config.json",
            config_hash,
            config_file.stat().st_size,
        ),
    )
    backend = DeterministicFixtureBackend(
        provider="CPUExecutionProvider",
        input_shape=(1, 3, 4, 4),
        output_shape=(1, 5, 2),
    )
    return ObservationSmokeFixture(
        frame=frame,
        pixel_store=store,
        binding=binding,
        preprocess_config=preprocess,
        assets=specs,
        backend=backend,
    )


def _resolve_asset_path(spec: AssetSpec, roots: Mapping[str, Path]) -> Path:
    root = roots.get(spec.root_env)
    if root is None:
        raw_root = os.environ.get(spec.root_env)
        root = Path(raw_root) if raw_root else None
    if root is None:
        raise ObservationSmokeError("external asset root is unavailable")
    try:
        resolved_root = root.expanduser().resolve(strict=True)
        if not resolved_root.is_dir():
            raise ObservationSmokeError("external asset root is unavailable")
        candidate = (resolved_root / Path(spec.relative_id)).resolve(strict=True)
        candidate.relative_to(resolved_root)
        if not candidate.is_file():
            raise ObservationSmokeError("external asset is unavailable")
        # A resolved path can still originate from a symlink/reparse point;
        # inspect the lexical chain before using the bytes.
        for component in (*reversed(candidate.parents), candidate):
            if component.is_symlink():
                raise ObservationSmokeError("external asset path is linked")
        return candidate
    except ObservationSmokeError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ObservationSmokeError("external asset is unavailable") from exc


def _parse_classes(path: Path) -> tuple[str, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ObservationSmokeError("classes asset cannot be decoded") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "names:":
        raise ObservationSmokeError("classes asset has an unsupported shape")
    values: list[str] = []
    for line in lines[1:]:
        if not line.startswith("- "):
            raise ObservationSmokeError("classes asset has an unsupported shape")
        value = line[2:].strip()
        if not value or _PORTABLE_ID_RE.fullmatch(value) is None:
            raise ObservationSmokeError("classes asset contains an invalid class")
        values.append(value)
    if not values or len(set(values)) != len(values):
        raise ObservationSmokeError("classes asset contains no unique classes")
    return tuple(values)


def _validate_effective_config(path: Path, binding: Any) -> None:
    """Bind the resolved pilot config semantics to the immutable ModelBinding."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        model = payload["model"]
        execution = payload["execution"]
        expected = {
            "model_id": binding.model_id,
            "classes": list(binding.classes),
            "input_size": {
                "height": binding.input_size.height,
                "width": binding.input_size.width,
            },
            "detection_confidence": binding.detection_confidence,
            "iou_threshold": binding.iou_threshold,
            "roi": list(binding.roi),
        }
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        raise ObservationSmokeError("effective config is unavailable or malformed") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(model, dict)
        or not isinstance(execution, dict)
    ):
        raise ObservationSmokeError("effective config is unavailable or malformed")
    if any(model.get(key) != value for key, value in expected.items()):
        raise ObservationSmokeError("effective config does not match the model binding")
    if execution.get("input_owner") != "legacy" or execution.get("real_input_enabled") is not False:
        raise ObservationSmokeError("effective config violates the input boundary")


def _verify_assets(
    specs: Sequence[AssetSpec],
    roots: Mapping[str, Path],
    binding: Any,
) -> tuple[list[dict[str, Any]], dict[str, Path], list[str]]:
    reports: list[dict[str, Any]] = []
    paths: dict[str, Path] = {}
    failures: list[str] = []
    seen: set[str] = set()
    binding_hashes = {
        "model": binding.model_sha256,
        "classes": binding.classes_sha256,
        "effective-config": binding.config_sha256,
    }
    for spec in specs:
        if spec.asset_id in seen:
            raise ObservationSmokeError("asset ids must be unique")
        seen.add(spec.asset_id)
        entry: dict[str, Any] = {
            "asset_id": spec.asset_id,
            "root_env": spec.root_env,
            "relative_id": spec.relative_id,
            "expected_sha256": spec.expected_sha256,
            "observed_sha256": None,
            "size_bytes": None,
            "status": "MISSING",
        }
        bound_hash = binding_hashes.get(spec.asset_id)
        if bound_hash is not None and spec.expected_sha256 != bound_hash:
            entry["status"] = "INVALID"
            failures.append(f"asset:{spec.asset_id}:binding_hash_mismatch")
            reports.append(entry)
            continue
        try:
            path = _resolve_asset_path(spec, roots)
            observed = _digest_file(path)
            size = path.stat().st_size
            entry["observed_sha256"] = observed
            entry["size_bytes"] = size
            if observed != spec.expected_sha256 or (
                spec.expected_size_bytes is not None and size != spec.expected_size_bytes
            ):
                entry["status"] = "HASH_MISMATCH"
                failures.append(f"asset:{spec.asset_id}:hash_mismatch")
            elif spec.asset_id == "classes" and _parse_classes(path) != tuple(binding.classes):
                entry["status"] = "INVALID"
                failures.append(f"asset:{spec.asset_id}:class_binding_mismatch")
            elif (
                spec.asset_id == "effective-config"
                and binding.release_id == "candidate-core-v2-20260829-shadow"
            ):
                try:
                    _validate_effective_config(path, binding)
                except ObservationSmokeError:
                    entry["status"] = "INVALID"
                    failures.append(f"asset:{spec.asset_id}:config_binding_mismatch")
                else:
                    entry["status"] = "VERIFIED"
                    paths[spec.asset_id] = path
            else:
                entry["status"] = "VERIFIED"
                paths[spec.asset_id] = path
        except ObservationSmokeError as exc:
            entry["status"] = "MISSING" if "unavailable" in str(exc) else "INVALID"
            failures.append(f"asset:{spec.asset_id}:{entry['status'].lower()}")
        except (OSError, RuntimeError, ValueError):
            entry["status"] = "INVALID"
            failures.append(f"asset:{spec.asset_id}:invalid")
        reports.append(entry)
    required = {"model", "classes", "effective-config"}
    missing_required = sorted(required - seen)
    for asset_id in missing_required:
        failures.append(f"asset:{asset_id}:declaration_missing")
    return reports, paths, failures


def _metadata_shape(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, tuple | list) or not value:
        return None
    if any(type(item) is not int or item <= 0 for item in value):
        return None
    return tuple(cast(int, item) for item in value)


def _backend_metadata(
    backend: Any | None, binding: Any
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    input_shape = (1, 3, binding.input_size.height, binding.input_size.width)
    output_shape = tuple(binding.output_shape)
    if backend is None:
        return (
            {"name": binding.input_name, "dtype": "tensor(float)", "shape": list(input_shape)},
            {"name": binding.output_name, "dtype": "tensor(float)", "shape": list(output_shape)},
            [],
        )
    failures: list[str] = []
    raw_input_name = getattr(backend, "input_name", None)
    raw_output_name = getattr(backend, "output_name", None)
    raw_input_dtype = getattr(backend, "input_dtype", None)
    raw_output_dtype = getattr(backend, "output_dtype", None)
    actual_input_shape = _metadata_shape(getattr(backend, "input_shape", None))
    actual_output_shape = _metadata_shape(getattr(backend, "output_shape", None))
    input_name = (
        raw_input_name
        if isinstance(raw_input_name, str)
        and raw_input_name
        and not any(token in raw_input_name for token in "\\/:")
        else "unavailable"
    )
    output_name = (
        raw_output_name
        if isinstance(raw_output_name, str)
        and raw_output_name
        and not any(token in raw_output_name for token in "\\/:")
        else "unavailable"
    )
    if input_name == "unavailable":
        failures.append("runtime:input_name_invalid")
    if output_name == "unavailable":
        failures.append("runtime:output_name_invalid")
    if raw_input_dtype != "tensor(float)":
        failures.append("runtime:input_dtype_invalid")
    if raw_output_dtype != "tensor(float)":
        failures.append("runtime:output_dtype_invalid")
    if actual_input_shape is None:
        failures.append("runtime:input_shape_invalid")
        actual_input_shape = (1,)
    if actual_output_shape is None:
        failures.append("runtime:output_shape_invalid")
        actual_output_shape = (1,)
    input_metadata = {
        "name": input_name,
        "dtype": "tensor(float)",
        "shape": list(actual_input_shape),
    }
    output_metadata = {
        "name": output_name,
        "dtype": "tensor(float)",
        "shape": list(actual_output_shape),
    }
    return input_metadata, output_metadata, failures


def _provider_values(backend: Any | None) -> tuple[str, ...]:
    if backend is None:
        return ()
    try:
        values = tuple(backend.providers)
    except Exception:
        return ()
    return tuple(
        value for value in values if isinstance(value, str) and _PROVIDER_RE.fullmatch(value)
    )


def _runtime_version(backend: Any | None, supplied: str | None) -> str:
    declared = getattr(backend, "runtime_version", None) if backend is not None else None
    if isinstance(declared, str) and declared:
        return declared
    if backend is None or "onnx" not in type(backend).__module__.casefold():
        return supplied or "fixture"
    try:
        runtime = importlib.import_module("onnxruntime")
        version = getattr(runtime, "__version__", None)
        if isinstance(version, str) and version:
            return version
    except Exception:
        pass
    return "unavailable"


def _input_audit() -> dict[str, Any]:
    return {
        "input_owner": "legacy",
        "real_input_enabled": False,
        "real_input_call_count": 0,
        "core_v2_real_input_call_count": 0,
        "double_write_event_count": 0,
    }


def _validate_input_audit(backend: Any | None) -> None:
    expected = _input_audit()
    supplied = getattr(backend, "input_audit", None) if backend is not None else None
    if supplied is None:
        return
    if not isinstance(supplied, Mapping) or dict(supplied) != expected:
        raise ObservationSmokeError("input audit drift detected")


def _fresh_pixel_spec(fixture: ObservationSmokeFixture) -> Any:
    from maple_automation_core.capture.pixel_store import PixelSpec

    raw = fixture.frame.image_metadata.get("pixel_spec")
    if isinstance(raw, Mapping):
        return PixelSpec.from_dict(cast(Mapping[str, Any], raw))
    size = fixture.preprocess_config.geometry.source_size
    return PixelSpec(width=size.width, height=size.height)


def _preprocess_digest(fixture: ObservationSmokeFixture) -> tuple[str, str]:
    from maple_automation_core.vision.preprocess import preprocess_pixels

    spec = _fresh_pixel_spec(fixture)
    pixels = fixture.pixel_store.read(fixture.frame.content_hash, spec)
    prepared = preprocess_pixels(
        pixels,
        spec,
        config=fixture.preprocess_config,
        expected_pixel_digest=fixture.frame.content_hash,
    )
    return fixture.preprocess_config.digest, prepared.tensor_sha256


def _run_one(
    fixture: ObservationSmokeFixture,
    backend: Any | None,
    index: int,
) -> tuple[dict[str, Any], str | None]:
    from maple_automation_core.capture.frame_source import canonical_calibration_sha256
    from maple_automation_core.vision.observation_adapter import ObservationAdapter

    started = time.perf_counter_ns()
    preprocess_digest = fixture.preprocess_config.digest
    tensor_digest = ZERO_SHA256
    actual_provider = "unavailable"
    result_digest = _digest_bytes(b"observation-run-not-run")
    status = "fault"
    fault_code: str | None = None
    try:
        preprocess_digest, tensor_digest = _preprocess_digest(fixture)
        if backend is None:
            raise ObservationSmokeError("detector backend is unavailable")
        providers = _provider_values(backend)
        if providers:
            actual_provider = providers[0]
        calibration = canonical_calibration_sha256(
            fixture.preprocess_config.geometry, fixture.frame.transform_version
        )
        adapter = ObservationAdapter(
            fixture.binding,
            fixture.pixel_store,
            backend,
            preprocess_config=fixture.preprocess_config,
            calibration_sha256=calibration,
            model_sha256=fixture.binding.model_sha256,
            classes_sha256=fixture.binding.classes_sha256,
            config_sha256=fixture.binding.config_sha256,
            clock=fixture.clock,
        )
        result = adapter.observe(fixture.frame)
        result_digest = result.digest
        if result.succeeded and result.observation is not None:
            status = "success"
            actual_provider = result.observation.execution_provider
        elif result.fault is not None:
            fault_code = result.fault.code.value
    except Exception as exc:
        fault_code = type(exc).__name__.casefold()
    elapsed_ns = time.perf_counter_ns() - started
    elapsed_ms = elapsed_ns / 1_000_000.0
    if not np.isfinite(elapsed_ms) or elapsed_ms < 0 or elapsed_ms > MAX_LATENCY_MS:
        raise ObservationSmokeError("latency diagnostic is not finite")
    run: dict[str, Any] = {
        "run_index": index,
        "status": status,
        "preprocess_digest": preprocess_digest,
        "tensor_digest": tensor_digest,
        "result_digest": result_digest,
        "actual_provider": (
            actual_provider if _PROVIDER_RE.fullmatch(actual_provider) else "unavailable"
        ),
        "latency_ms": elapsed_ms,
    }
    return run, fault_code


def _consistency(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    preprocess = [run["preprocess_digest"] for run in runs]
    tensor = [run["tensor_digest"] for run in runs]
    result = [run["result_digest"] for run in runs]
    values = {
        "run_count": len(runs),
        "preprocess_equal": len(set(preprocess)) == 1,
        "tensor_equal": len(set(tensor)) == 1,
        "result_equal": len(set(result)) == 1,
    }
    values["all_equal"] = all(
        values[key] for key in ("preprocess_equal", "tensor_equal", "result_equal")
    )
    return values


def _latency(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [float(run["latency_ms"]) for run in runs]
    if not values or not all(np.isfinite(value) and value >= 0 for value in values):
        raise ObservationSmokeError("latency diagnostic is not finite")
    return {
        "sample_count": len(values),
        "min_ms": min(values),
        "max_ms": max(values),
        "mean_ms": sum(values) / len(values),
        "all_finite": True,
    }


def _privacy_findings(payload: object) -> int:
    encoded = _canonical_json(payload).decode("utf-8")
    return len(_ABSOLUTE_PATH_RE.findall(encoded))


def _schema_validate(payload: Mapping[str, Any], schema_path: Path) -> None:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
            key=lambda error: list(error.path),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        raise ObservationSmokeError("observation report schema is unavailable") from exc
    if errors:
        detail = "; ".join(error.message for error in errors[:5])
        raise ObservationSmokeError(f"observation report schema rejected payload: {detail}")


def validate_report(
    payload: Mapping[str, Any],
    *,
    schema_path: Path = DEFAULT_SCHEMA,
    lock_path: Path = DEFAULT_LOCK,
    tool_path: Path = Path(__file__),
) -> None:
    """Validate schema, digest, privacy, and fail-closed semantic invariants."""

    if not isinstance(payload, Mapping):
        raise ObservationSmokeError("observation report must be an object")
    _schema_validate(payload, schema_path)
    report = dict(payload)
    declared = report.get("report_digest")
    unsigned = dict(report)
    unsigned.pop("report_digest", None)
    if declared != _digest_bytes(_canonical_json(unsigned)):
        raise ObservationSmokeError("report digest mismatch")
    if _privacy_findings(report) != 0:
        raise ObservationSmokeError("report contains an absolute path")
    if report["privacy_audit"] != {
        "absolute_paths_present": False,
        "model_custom_metadata_present": False,
        "raw_artifacts_public": False,
        "finding_count": 0,
    }:
        raise ObservationSmokeError("report privacy audit is not clean")
    if report["input_audit"] != _input_audit():
        raise ObservationSmokeError("report input audit drifted")
    if report["sealed_state"] != {
        "evidence_id_is_new": True,
        "current_bundle_touched": False,
        "sealed_bundle_touched": False,
    }:
        raise ObservationSmokeError("sealed state is not preserved")
    if report["tool_artifact_sha256"] != _digest_file(tool_path):
        raise ObservationSmokeError("report tool artifact hash mismatch")
    if report["schema_sha256"] != _digest_file(schema_path):
        raise ObservationSmokeError("report schema artifact hash mismatch")
    if report["dependency_lock_sha256"] != _verify_runtime_lock(lock_path):
        raise ObservationSmokeError("report dependency lock hash mismatch")
    try:
        from maple_automation_core.domain.observation import ModelBinding

        binding = ModelBinding.from_dict(cast(Mapping[str, Any], report["model_binding"]))
    except (TypeError, ValueError, KeyError) as exc:
        raise ObservationSmokeError("report model binding is invalid") from exc
    if binding.digest != report["model_binding_sha256"]:
        raise ObservationSmokeError("report model binding digest mismatch")
    if binding.release_id != report["release_id"]:
        raise ObservationSmokeError("report release id does not match model binding")
    assets = cast(list[Mapping[str, Any]], report["assets"])
    asset_ids = {item["asset_id"] for item in assets}
    if len(asset_ids) != len(assets):
        raise ObservationSmokeError("asset ids are not unique")
    if not {"model", "classes", "effective-config"}.issubset(asset_ids):
        raise ObservationSmokeError("required assets are missing")
    binding_hashes = {
        "model": binding.model_sha256,
        "classes": binding.classes_sha256,
        "effective-config": binding.config_sha256,
    }
    for asset in assets:
        expected = asset["expected_sha256"]
        observed = asset["observed_sha256"]
        bound_hash = binding_hashes.get(cast(str, asset["asset_id"]))
        if report["status"] == "PASS" and bound_hash is not None and expected != bound_hash:
            raise ObservationSmokeError("asset hash does not match model binding")
        if asset["status"] == "VERIFIED":
            if observed != expected:
                raise ObservationSmokeError("verified asset hash mismatch")
            if type(asset["size_bytes"]) is not int or asset["size_bytes"] <= 0:
                raise ObservationSmokeError("verified asset size is unavailable")
    runtime = cast(Mapping[str, Any], report["runtime"])
    if runtime["wheel_sha256"] != ORT_WHEEL_SHA256:
        raise ObservationSmokeError("runtime wheel hash is not locked")
    if runtime["wheel_size_bytes"] != ORT_WHEEL_SIZE_BYTES:
        raise ObservationSmokeError("runtime wheel size is not locked")
    expected_input = {
        "name": binding.input_name,
        "dtype": "tensor(float)",
        "shape": [1, 3, binding.input_size.height, binding.input_size.width],
    }
    expected_output = {
        "name": binding.output_name,
        "dtype": "tensor(float)",
        "shape": list(binding.output_shape),
    }
    if (
        not binding.requested_providers
        or runtime["requested_provider"] != binding.requested_providers[0]
    ):
        raise ObservationSmokeError("runtime provider does not match model binding")
    runs = cast(list[Mapping[str, Any]], report["runs"])
    if [run["run_index"] for run in runs] != [1, 2, 3]:
        raise ObservationSmokeError("run indices are not canonical")
    consistency = cast(Mapping[str, Any], report["consistency"])
    expected_consistency = _consistency(runs)
    if dict(consistency) != expected_consistency:
        raise ObservationSmokeError("consistency summary mismatch")
    latency = cast(Mapping[str, Any], report["latency"])
    if dict(latency) != _latency(runs):
        raise ObservationSmokeError("latency summary mismatch")
    if report["release_id"] == "candidate-core-v2-20260829-shadow":
        if binding.digest != PILOT_BINDING_SHA256:
            raise ObservationSmokeError("pilot model binding is not frozen")
        frozen_assets = {item.asset_id: item for item in _default_asset_specs()}
        if asset_ids != set(frozen_assets):
            raise ObservationSmokeError("pilot asset set is not frozen")
        for asset in assets:
            frozen = frozen_assets[cast(str, asset["asset_id"])]
            if (
                asset["root_env"] != frozen.root_env
                or asset["relative_id"] != frozen.relative_id
                or asset["expected_sha256"] != frozen.expected_sha256
                or asset["size_bytes"] != frozen.expected_size_bytes
            ):
                raise ObservationSmokeError("pilot asset binding mismatch")
        frozen_input = {
            "name": "images",
            "dtype": "tensor(float)",
            "shape": [1, 3, 640, 640],
        }
        frozen_output = {
            "name": "output0",
            "dtype": "tensor(float)",
            "shape": [1, 5, 8400],
        }
        if runtime["requested_provider"] != "CPUExecutionProvider":
            raise ObservationSmokeError("pilot runtime provider is not frozen")
    if report["status"] == "PASS":
        if runtime["requested_provider"] != runtime["actual_provider"]:
            raise ObservationSmokeError("runtime provider identity mismatch")
        if runtime["input"] != expected_input or runtime["output"] != expected_output:
            raise ObservationSmokeError("runtime tensor contract does not match model binding")
        if any(run["actual_provider"] != runtime["actual_provider"] for run in runs):
            raise ObservationSmokeError("run provider identity mismatch")
        if report["release_id"] == "candidate-core-v2-20260829-shadow" and (
            runtime["input"] != frozen_input or runtime["output"] != frozen_output
        ):
            raise ObservationSmokeError("pilot runtime tensor contract mismatch")
        if report["runtime"]["ort_version"] not in {EXPECTED_ORT_VERSION, "fixture"}:
            raise ObservationSmokeError("PASS report runtime version is not locked")
        if any(asset["status"] != "VERIFIED" for asset in assets):
            raise ObservationSmokeError("PASS report contains an unverified asset")
        if any(run["status"] != "success" for run in runs):
            raise ObservationSmokeError("PASS report contains a fault run")
        if not consistency["all_equal"]:
            raise ObservationSmokeError("PASS report is not deterministic")
        if report["failures"]:
            raise ObservationSmokeError("PASS report contains failures")


def _make_report(
    *,
    fixture: ObservationSmokeFixture,
    backend: Any | None,
    asset_reports: list[dict[str, Any]],
    asset_failures: list[str],
    source_commit: str,
    evidence_id: str,
    generated_at: str,
    schema_path: Path,
    lock_path: Path,
    ort_version: str | None,
    extra_failures: Sequence[str] = (),
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    failures = list(asset_failures) + list(extra_failures)
    for index in range(1, RUN_COUNT + 1):
        run, fault_code = _run_one(fixture, backend, index)
        runs.append(run)
        if run["status"] != "success":
            suffix = fault_code or "observation_fault"
            failures.append(f"run:{index}:{_safe_text(suffix)}")
    consistency = _consistency(runs)
    latency = _latency(runs)
    input_metadata, output_metadata, metadata_failures = _backend_metadata(backend, fixture.binding)
    failures.extend(metadata_failures)
    requested_provider = fixture.binding.requested_providers[0]
    provider_values = _provider_values(backend)
    actual_provider = provider_values[0] if provider_values else "unavailable"
    if not _PROVIDER_RE.fullmatch(actual_provider):
        actual_provider = "unavailable"
    if actual_provider != requested_provider:
        failures.append("provider:actual_does_not_match_requested")
    if len({run["actual_provider"] for run in runs}) != 1:
        failures.append("provider:run_drift")
    expected_input = [1, 3, fixture.binding.input_size.height, fixture.binding.input_size.width]
    if (
        input_metadata["name"] != fixture.binding.input_name
        or input_metadata["shape"] != expected_input
    ):
        failures.append("runtime:input_metadata_mismatch")
    if output_metadata["name"] != fixture.binding.output_name or tuple(
        output_metadata["shape"]
    ) != tuple(fixture.binding.output_shape):
        failures.append("runtime:output_metadata_mismatch")
    runtime_version = _runtime_version(backend, ort_version)
    if not isinstance(runtime_version, str) or not runtime_version:
        runtime_version = "unavailable"
    if runtime_version not in {EXPECTED_ORT_VERSION, "fixture"}:
        failures.append("runtime:ort_version_mismatch")
    try:
        tool_hash = _digest_file(Path(__file__))
    except ObservationSmokeError:
        tool_hash = ZERO_SHA256
        failures.append("artifact:tool_hash_unavailable")
    try:
        lock_hash = _verify_runtime_lock(lock_path)
    except ObservationSmokeError:
        lock_hash = ZERO_SHA256
        failures.append("artifact:dependency_lock_unavailable")
    try:
        schema_hash = _digest_file(schema_path)
    except ObservationSmokeError:
        schema_hash = ZERO_SHA256
        failures.append("artifact:schema_hash_unavailable")
    status = "PASS" if not failures and consistency["all_equal"] else "FAIL"
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "evidence_id": evidence_id,
        "generated_at": generated_at,
        "status": status,
        "scope": "offline_observation",
        "source_commit": source_commit,
        "release_id": fixture.binding.release_id,
        "model_binding": fixture.binding.to_dict(),
        "model_binding_sha256": fixture.binding.digest,
        "tool_artifact_sha256": tool_hash,
        "dependency_lock_sha256": lock_hash,
        "schema_sha256": schema_hash,
        "assets": asset_reports,
        "runtime": {
            "ort_version": runtime_version,
            "wheel_sha256": ORT_WHEEL_SHA256,
            "wheel_size_bytes": ORT_WHEEL_SIZE_BYTES,
            "requested_provider": requested_provider,
            "actual_provider": actual_provider,
            "input": input_metadata,
            "output": output_metadata,
        },
        "runs": runs,
        "consistency": consistency,
        "latency": latency,
        "input_audit": _input_audit(),
        "privacy_audit": {
            "absolute_paths_present": False,
            "model_custom_metadata_present": False,
            "raw_artifacts_public": False,
            "finding_count": 0,
        },
        "sealed_state": {
            "evidence_id_is_new": True,
            "current_bundle_touched": False,
            "sealed_bundle_touched": False,
        },
        "failures": sorted({_safe_text(item) for item in failures if item}),
        "limitations": [
            "Offline FramePacket/CAS fixture or injected backend only; no device is opened.",
            "The report establishes integration determinism, not detector accuracy or P/R.",
            "The input audit remains Legacy-owned with every real-input counter at zero.",
        ],
    }
    unsigned = dict(report)
    report["report_digest"] = _digest_bytes(_canonical_json(unsigned))
    return report


def run_smoke(
    *,
    repo_root: Path = ROOT,
    fixture: ObservationSmokeFixture | Callable[[], ObservationSmokeFixture] | None = None,
    backend: Any | None = None,
    asset_specs: Sequence[AssetSpec] | None = None,
    asset_roots: Mapping[str, Path | str] | None = None,
    provider: str | None = None,
    model_root: Path | str | None = None,
    classes_root: Path | str | None = None,
    core_root: Path | str | None = None,
    source_commit: str | None = None,
    evidence_id: str | None = None,
    generated_at: str | None = None,
    ort_version: str | None = None,
    schema_path: Path = DEFAULT_SCHEMA,
    lock_path: Path = DEFAULT_LOCK,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Run three deterministic observations and return a strict report."""

    resource: Any | None = None
    if fixture is None:
        fixture_value, resource = _pilot_fixture()
    elif callable(fixture) and not isinstance(fixture, ObservationSmokeFixture):
        fixture_value = fixture()
    else:
        fixture_value = fixture
    if not isinstance(fixture_value, ObservationSmokeFixture):
        raise TypeError("fixture must be an ObservationSmokeFixture or factory")
    try:
        selected_backend = backend if backend is not None else fixture_value.backend
        roots: dict[str, Path] = {key: Path(value) for key, value in (asset_roots or {}).items()}
        if model_root is not None:
            roots["MAPLE_MODEL_ROOT"] = Path(model_root)
        if classes_root is not None:
            roots["MAPLE_LEGACY_ROOT"] = Path(classes_root)
        if core_root is not None:
            roots["MAPLE_CORE_ROOT"] = Path(core_root)
        specs = tuple(asset_specs or fixture_value.assets or _default_asset_specs())
        asset_reports, paths, asset_failures = _verify_assets(specs, roots, fixture_value.binding)
        failures = list(asset_failures)
        # Asset evidence is a hard gate.  An injected backend still exercises
        # report construction in unit tests, while inference remains skipped
        # whenever a declared model/classes/config asset is unavailable or
        # mismatched.
        if asset_failures:
            selected_backend = None
        selected_provider = (
            fixture_value.binding.requested_providers[0] if provider is None else provider
        )
        if selected_provider != fixture_value.binding.requested_providers[0]:
            failures.append("provider:fixture_binding_mismatch")
        if selected_backend is None and not asset_failures:
            try:
                from maple_automation_core.vision.onnx_backend import (
                    OnnxBackendConfig,
                    OnnxDetectorBackend,
                )

                model_spec = next(item for item in specs if item.asset_id == "model")
                model_root_path = roots.get(model_spec.root_env)
                if model_root_path is None:
                    raw_model_root = os.environ.get(model_spec.root_env)
                    model_root_path = Path(raw_model_root) if raw_model_root else None
                if model_root_path is None:
                    raise ObservationSmokeError("external model root is unavailable")
                config = OnnxBackendConfig(
                    input_name=fixture_value.binding.input_name,
                    input_shape=(
                        1,
                        3,
                        fixture_value.binding.input_size.height,
                        fixture_value.binding.input_size.width,
                    ),
                    output_name=fixture_value.binding.output_name,
                    output_shape=tuple(fixture_value.binding.output_shape),
                )
                selected_backend = OnnxDetectorBackend(
                    external_root=model_root_path,
                    model_relative_path=model_spec.relative_id,
                    model_sha256=model_spec.expected_sha256,
                    requested_provider=selected_provider,
                    expected_config=config,
                )
            except Exception as exc:
                failures.append(f"backend:{type(exc).__name__.casefold()}:{_safe_text(exc)}")
        _validate_input_audit(selected_backend)
        try:
            commit = (
                _normalise_commit(source_commit)
                if source_commit is not None
                else _git_commit(repo_root)
            )
        except ObservationSmokeError:
            commit = ZERO_COMMIT
            failures.append("source:commit_unavailable")
        current_evidence_id = _portable_id(evidence_id or _default_evidence_id(), "evidence_id")
        report = _make_report(
            fixture=fixture_value,
            backend=selected_backend,
            asset_reports=asset_reports,
            asset_failures=failures,
            source_commit=commit,
            evidence_id=current_evidence_id,
            generated_at=generated_at or _timestamp(),
            schema_path=schema_path,
            lock_path=lock_path,
            ort_version=ort_version,
        )
        # Recompute status after artifact/source checks that occur around the
        # report construction and then bind the canonical report digest.
        if commit == ZERO_COMMIT:
            report["failures"] = sorted(set(report["failures"]) | {"source:commit_unavailable"})
            report["status"] = "FAIL"
        unsigned = dict(report)
        unsigned.pop("report_digest", None)
        report["report_digest"] = _digest_bytes(_canonical_json(unsigned))
        validate_report(report, schema_path=schema_path, lock_path=lock_path)
        if report_path is not None:
            write_report(report, report_path, schema_path=schema_path, lock_path=lock_path)
        return report
    finally:
        if resource is not None:
            resource.cleanup()


def build_report(**kwargs: Any) -> dict[str, Any]:
    """Compatibility name for callers that treat the smoke as a report builder."""

    return run_smoke(**kwargs)


run_observation_smoke = run_smoke


def _protected_destination(path: Path) -> bool:
    name = path.name.casefold()
    if name in {"current.json", "sealed.json"}:
        return True
    return any(part.casefold() in {"current", "sealed"} for part in path.parts)


def _linked_destination_parent(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    for parent in absolute.parents:
        if not parent.exists():
            continue
        try:
            is_junction = getattr(parent, "is_junction", lambda: False)
            if parent.is_symlink() or bool(is_junction()):
                return True
        except OSError:
            return True
    return False


def write_report(
    payload: Mapping[str, Any],
    path: Path,
    *,
    schema_path: Path = DEFAULT_SCHEMA,
    lock_path: Path = DEFAULT_LOCK,
) -> None:
    """Exclusively create a new report without following linked evidence parents."""

    validate_report(payload, schema_path=schema_path, lock_path=lock_path)
    destination = Path(path)
    if (
        _protected_destination(destination)
        or _protected_destination(destination.resolve(strict=False))
        or _linked_destination_parent(destination)
    ):
        raise ObservationSmokeError("current/sealed destinations are protected")
    created = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if _linked_destination_parent(destination):
            raise ObservationSmokeError("linked evidence destinations are protected")
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(destination, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise ObservationSmokeError("evidence destination already exists") from exc
    except ObservationSmokeError:
        raise
    except (OSError, UnicodeError) as exc:
        if created:
            with suppress(OSError):
                destination.unlink(missing_ok=True)
        raise ObservationSmokeError("evidence report could not be written") from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--evidence-id")
    parser.add_argument("--provider", default="CPUExecutionProvider")
    parser.add_argument("--model-root", type=Path)
    parser.add_argument("--classes-root", type=Path)
    parser.add_argument("--core-root", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--generated-at")
    parser.add_argument("--ort-version")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report_path = args.report
    if report_path is None:
        evidence_id = args.evidence_id or _default_evidence_id()
        report_path = args.repo_root / "evidence" / "g1-obs-002b" / f"{evidence_id}.json"
    try:
        report = run_smoke(
            repo_root=args.repo_root,
            provider=args.provider,
            model_root=args.model_root,
            classes_root=args.classes_root,
            core_root=args.core_root,
            source_commit=args.source_commit,
            evidence_id=args.evidence_id,
            generated_at=args.generated_at,
            ort_version=args.ort_version,
            schema_path=args.schema,
            lock_path=args.lock,
            report_path=report_path,
        )
    except (ObservationSmokeError, OSError, ValueError, TypeError) as exc:
        print(f"Observation smoke failed: {_safe_text(exc)}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLASSES_SHA256",
    "CONFIG_SHA256",
    "EXPECTED_ORT_VERSION",
    "MODEL_SHA256",
    "AssetSpec",
    "DeterministicFixtureBackend",
    "ObservationSmokeError",
    "ObservationSmokeFixture",
    "build_report",
    "make_fixture",
    "run_observation_smoke",
    "run_smoke",
    "validate_report",
    "write_report",
]
