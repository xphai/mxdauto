"""Fail-closed ONNX detector backend.

The observation foundation deliberately keeps ``onnxruntime`` out of its
import graph.  This module follows the same boundary: the runtime is loaded
only when a backend is constructed without an injected runtime or session
factory.  Model bytes are resolved below an explicitly supplied external root,
hashed as a stream, and only then handed to a session factory.
"""

from __future__ import annotations

import importlib
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from os import PathLike, fspath
from pathlib import Path, PureWindowsPath
from typing import Any, Final, cast

import numpy as np
from numpy.typing import NDArray

from .observation_adapter import DetectorOutput

Tensor = NDArray[np.float32]
SessionFactory = Callable[..., Any]


class OnnxBackendError(RuntimeError):
    """Stable, path-free failure raised by :class:`OnnxDetectorBackend`."""


@dataclass(frozen=True, slots=True)
class OnnxBackendConfig:
    """Immutable ONNX input/output contract.

    ONNX Runtime exposes floating-point tensors as ``tensor(float)``.  The
    shape is intentionally a concrete tuple: symbolic or partially dynamic
    session metadata is rejected at the backend boundary.
    """

    input_name: str = "images"
    input_dtype: str = "tensor(float)"
    input_shape: tuple[int, ...] = (1, 3, 640, 640)
    output_name: str = "output0"
    output_dtype: str = "tensor(float)"
    output_shape: tuple[int, ...] = (1, 5, 8400)

    def __post_init__(self) -> None:
        if not isinstance(self.input_name, str) or not self.input_name:
            raise ValueError("input_name must be a non-empty string.")
        if not isinstance(self.output_name, str) or not self.output_name:
            raise ValueError("output_name must be a non-empty string.")
        if _float32_type(self.input_dtype) is None:
            raise ValueError("input_dtype must describe float32.")
        if _float32_type(self.output_dtype) is None:
            raise ValueError("output_dtype must describe float32.")
        _validate_shape_config(self.input_shape, "input_shape")
        _validate_shape_config(self.output_shape, "output_shape")

    @property
    def input_type(self) -> str:
        """Alias matching ONNX Runtime's ``NodeArg.type`` terminology."""

        return self.input_dtype

    @property
    def output_type(self) -> str:
        """Alias matching ONNX Runtime's ``NodeArg.type`` terminology."""

        return self.output_dtype


@dataclass(frozen=True, slots=True)
class _TensorMetadata:
    name: str
    dtype: str
    shape: tuple[int, ...]


def _float32_type(value: object) -> str | None:
    """Return the canonical ONNX float32 type for known spellings."""

    if isinstance(value, np.dtype):
        return "tensor(float)" if value == np.dtype(np.float32) else None
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    if token in {"tensor(float)", "float32", "numpy.float32", "<class 'numpy.float32'>"}:
        return "tensor(float)"
    return None


def _validate_shape_config(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, tuple) or not value:
        raise ValueError(f"{name} must be a non-empty tuple.")
    shape: list[int] = []
    for dimension in value:
        if isinstance(dimension, bool) or not isinstance(dimension, int | np.integer):
            raise ValueError(f"{name} must contain concrete positive integers.")
        integer = int(dimension)
        if integer <= 0:
            raise ValueError(f"{name} must contain concrete positive integers.")
        shape.append(integer)
    return tuple(shape)


DEFAULT_ONNX_BACKEND_CONFIG: Final[OnnxBackendConfig] = OnnxBackendConfig()


def _normalise_sha256(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise OnnxBackendError("model SHA-256 is invalid.")
    return value.lower()


def _normalise_relative_path(value: object) -> str:
    """Validate a relative model identifier without platform ambiguities."""

    if isinstance(value, PathLike):
        try:
            raw = fspath(value)
        except Exception:
            raise OnnxBackendError("model path is invalid.") from None
    else:
        raw = value
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise OnnxBackendError("model path is invalid.")

    # Check both separators.  ``PureWindowsPath`` catches drive/UNC paths even
    # when tests run on a POSIX host, while Path catches the native spelling.
    windows = PureWindowsPath(raw)
    native = Path(raw)
    if native.is_absolute() or windows.is_absolute() or windows.drive:
        raise OnnxBackendError("model path must be relative to the external root.")
    pieces = raw.replace("\\", "/").split("/")
    if not pieces or any(piece in {"", ".", ".."} for piece in pieces):
        raise OnnxBackendError("model path must be a normalized relative path.")
    return "/".join(pieces)


def _resolve_model(root_value: object, relative_value: object) -> tuple[Path, str]:
    if isinstance(root_value, PathLike):
        try:
            root_raw = fspath(root_value)
        except Exception:
            raise OnnxBackendError("external model root is invalid.") from None
    else:
        root_raw = root_value
    if not isinstance(root_raw, str) or not root_raw or "\x00" in root_raw:
        raise OnnxBackendError("external model root is invalid.")
    relative = _normalise_relative_path(relative_value)
    try:
        root = Path(root_raw).resolve(strict=True)
        if not root.is_dir():
            raise OnnxBackendError("external model root is unavailable.")
        candidate = (root / Path(relative)).resolve(strict=True)
        candidate.relative_to(root)
        if not candidate.is_file():
            raise OnnxBackendError("model file is unavailable.")
    except OnnxBackendError:
        raise
    except Exception:
        # Do not expose the root or candidate in filesystem diagnostics.
        raise OnnxBackendError("model file is unavailable.") from None
    return candidate, relative


def _read_verified_model(path: Path, expected_sha256: str) -> bytes:
    """Read and hash one immutable session payload without a path TOCTOU gap."""

    digest = sha256()
    payload = bytearray()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                payload.extend(chunk)
    except Exception:
        raise OnnxBackendError("model file is unavailable.") from None
    if digest.hexdigest() != expected_sha256:
        raise OnnxBackendError("model SHA-256 does not match expected digest.")
    return bytes(payload)


def _metadata_value(metadata: object, key: str) -> object:
    if isinstance(metadata, Mapping):
        return metadata.get(key)
    return getattr(metadata, key, None)


def _shape_from_metadata(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, list | tuple) or not value:
        return None
    result: list[int] = []
    for dimension in value:
        if isinstance(dimension, bool) or not isinstance(dimension, int | np.integer):
            return None
        integer = int(dimension)
        if integer <= 0:
            return None
        result.append(integer)
    return tuple(result)


def _read_tensor_metadata(metadata: object) -> _TensorMetadata | None:
    name = _metadata_value(metadata, "name")
    dtype = _metadata_value(metadata, "type")
    if dtype is None:
        dtype = _metadata_value(metadata, "dtype")
    shape = _shape_from_metadata(_metadata_value(metadata, "shape"))
    if not isinstance(name, str) or not name or shape is None:
        return None
    canonical_dtype = _float32_type(dtype)
    if canonical_dtype is None:
        if not isinstance(dtype, str):
            return None
        return _TensorMetadata(name=name, dtype=dtype, shape=shape)
    return _TensorMetadata(name=name, dtype=canonical_dtype, shape=shape)


def _one_metadata(value: object) -> _TensorMetadata | None:
    if isinstance(value, str | bytes | Mapping) or value is None:
        return None
    try:
        entries = tuple(cast(Sequence[object], value))
    except (TypeError, ValueError):
        return None
    if len(entries) != 1:
        return None
    return _read_tensor_metadata(entries[0])


class OnnxDetectorBackend:
    """An injectable, single-provider ONNX Runtime detector backend.

    Construction is the session boundary: path policy, streaming digest,
    provider availability, session provider identity, and tensor metadata are
    all verified before the object becomes usable.  ``infer`` serializes the
    session interaction and repeats provider/metadata checks to detect runtime
    drift instead of silently falling back.
    """

    def __init__(
        self,
        external_root: str | Path,
        model_relative_path: str | Path,
        model_sha256: str,
        requested_provider: str,
        *,
        runtime: Any | None = None,
        runtime_factory: Callable[[], Any] | None = None,
        session_factory: SessionFactory | None = None,
        expected_config: OnnxBackendConfig | None = None,
    ) -> None:
        self._lock = threading.RLock()
        if (
            not isinstance(requested_provider, str)
            or not requested_provider
            or requested_provider != requested_provider.strip()
        ):
            raise OnnxBackendError("requested provider is invalid.")
        if runtime is not None and runtime_factory is not None:
            raise OnnxBackendError("runtime and runtime factory are mutually exclusive.")
        if session_factory is not None and runtime_factory is not None:
            raise OnnxBackendError("session factory and runtime factory are mutually exclusive.")
        self._requested_provider = requested_provider
        if expected_config is None:
            expected = DEFAULT_ONNX_BACKEND_CONFIG
        elif not isinstance(expected_config, OnnxBackendConfig):
            raise OnnxBackendError("expected ONNX contract is invalid.")
        else:
            expected = expected_config
        self._expected = expected
        self._model_path, self._relative_model_path = _resolve_model(
            external_root, model_relative_path
        )
        self._model_sha256 = _normalise_sha256(model_sha256)
        model_payload = _read_verified_model(self._model_path, self._model_sha256)

        runtime_object = runtime
        factory = session_factory
        if runtime_object is None and factory is None:
            if runtime_factory is not None:
                if not callable(runtime_factory):
                    raise OnnxBackendError("runtime factory is invalid.")
                try:
                    runtime_object = runtime_factory()
                except Exception:
                    raise OnnxBackendError("onnxruntime is unavailable.") from None
            else:
                runtime_object = self._load_runtime()
        if runtime_object is not None:
            self._check_provider_available(runtime_object)
        runtime_version = getattr(runtime_object, "__version__", None)
        self._runtime_version = (
            runtime_version if isinstance(runtime_version, str) and runtime_version else None
        )
        if factory is None:
            if runtime_object is None:
                raise OnnxBackendError("ONNX session factory is unavailable.")
            candidate_factory = getattr(runtime_object, "InferenceSession", None)
            if not callable(candidate_factory):
                raise OnnxBackendError("ONNX session factory is unavailable.")
            factory = cast(SessionFactory, candidate_factory)
        elif not callable(factory):
            raise OnnxBackendError("ONNX session factory is invalid.")

        session = self._create_session(factory, model_payload)
        self._session = session
        self._input_metadata, self._output_metadata = self._validate_session_contract(session)
        self._assert_provider_identity(session)

    @staticmethod
    def _load_runtime() -> Any:
        try:
            return importlib.import_module("onnxruntime")
        except Exception:
            raise OnnxBackendError("onnxruntime is unavailable.") from None

    def _check_provider_available(self, runtime: Any) -> None:
        getter = getattr(runtime, "get_available_providers", None)
        if not callable(getter):
            raise OnnxBackendError("runtime provider inventory is unavailable.")
        try:
            available_value = getter()
            available = tuple(available_value)
            valid = (
                bool(available)
                and all(isinstance(item, str) and bool(item) for item in available)
                and self._requested_provider in available
            )
        except Exception:
            raise OnnxBackendError("runtime provider inventory is unavailable.") from None
        if not valid:
            raise OnnxBackendError("requested provider is unavailable.")

    def _create_session(self, factory: SessionFactory, model_payload: bytes) -> Any:
        try:
            # Exactly one requested provider is supplied.  No fallback list is
            # passed, even when the runtime has a CPU provider available.
            return factory(model_payload, providers=[self._requested_provider])
        except Exception:
            raise OnnxBackendError("ONNX session initialization failed.") from None

    def _session_providers(self, session: Any) -> tuple[str, ...]:
        getter = getattr(session, "get_providers", None)
        if not callable(getter):
            raise OnnxBackendError("session provider metadata is unavailable.")
        try:
            values = tuple(getter())
        except Exception:
            raise OnnxBackendError("session provider metadata is unavailable.") from None
        if any(not isinstance(item, str) or not item for item in values):
            raise OnnxBackendError("session provider metadata is invalid.")
        return values

    def _assert_provider_identity(self, session: Any) -> None:
        actual = self._session_providers(session)
        if actual != (self._requested_provider,):
            raise OnnxBackendError("session execution provider does not match requested provider.")

    def _read_session_contract(
        self, session: Any
    ) -> tuple[_TensorMetadata | None, _TensorMetadata | None]:
        inputs_getter = getattr(session, "get_inputs", None)
        outputs_getter = getattr(session, "get_outputs", None)
        if not callable(inputs_getter) or not callable(outputs_getter):
            raise OnnxBackendError("session tensor metadata is unavailable.")
        try:
            actual_input = _one_metadata(inputs_getter())
            actual_output = _one_metadata(outputs_getter())
        except Exception:
            raise OnnxBackendError("session tensor metadata is unavailable.") from None
        return actual_input, actual_output

    def _validate_session_contract(self, session: Any) -> tuple[_TensorMetadata, _TensorMetadata]:
        actual_input, actual_output = self._read_session_contract(session)
        expected = self._expected
        expected_input_dtype = _float32_type(expected.input_dtype)
        expected_output_dtype = _float32_type(expected.output_dtype)
        if (
            actual_input is None
            or actual_input.name != expected.input_name
            or actual_input.dtype != expected_input_dtype
            or actual_input.shape != expected.input_shape
        ):
            raise OnnxBackendError("ONNX input metadata does not match expected contract.")
        if (
            actual_output is None
            or actual_output.name != expected.output_name
            or actual_output.dtype != expected_output_dtype
            or actual_output.shape != expected.output_shape
        ):
            raise OnnxBackendError("ONNX output metadata does not match expected contract.")
        return actual_input, actual_output

    def _assert_session_contract_unchanged(self) -> None:
        actual_input, actual_output = self._read_session_contract(self._session)
        if actual_input != self._input_metadata:
            raise OnnxBackendError("ONNX input metadata drift detected.")
        if actual_output != self._output_metadata:
            raise OnnxBackendError("ONNX output metadata drift detected.")

    @property
    def providers(self) -> tuple[str, ...]:
        """Return the one verified provider, rejecting provider drift."""

        with self._lock:
            self._assert_provider_identity(self._session)
            return (self._requested_provider,)

    @property
    def requested_provider(self) -> str:
        return self._requested_provider

    @property
    def model_sha256(self) -> str:
        return self._model_sha256

    @property
    def model_relative_path(self) -> str:
        """The normalized relative model identifier (never an absolute path)."""

        return self._relative_model_path

    @property
    def runtime_version(self) -> str | None:
        """Return the attested runtime version when the runtime exposes one."""

        return self._runtime_version

    @property
    def input_name(self) -> str:
        return self._input_metadata.name

    @property
    def output_name(self) -> str:
        return self._output_metadata.name

    @property
    def input_shape(self) -> tuple[int, ...]:
        return self._input_metadata.shape

    @property
    def input_dtype(self) -> str:
        return self._input_metadata.dtype

    @property
    def output_shape(self) -> tuple[int, ...]:
        return self._output_metadata.shape

    @property
    def output_dtype(self) -> str:
        return self._output_metadata.dtype

    def infer(
        self,
        tensor: Tensor,
        *,
        provider: str,
        input_name: str,
        output_name: str,
    ) -> DetectorOutput:
        """Run one verified inference and return actual session metadata."""

        with self._lock:
            if provider != self._requested_provider:
                raise OnnxBackendError("inference provider does not match requested provider.")
            if input_name != self._input_metadata.name or output_name != self._output_metadata.name:
                raise OnnxBackendError("inference tensor names do not match session metadata.")
            self._assert_provider_identity(self._session)
            self._assert_session_contract_unchanged()
            if (
                not isinstance(tensor, np.ndarray)
                or tensor.dtype != np.dtype(np.float32)
                or tensor.shape != self._input_metadata.shape
                or not np.isfinite(tensor).all()
            ):
                raise OnnxBackendError(
                    "inference input must be a finite float32 tensor with the expected shape."
                )
            feed = np.array(tensor, dtype=np.float32, copy=True, order="C")
            try:
                result = self._session.run(
                    [self._output_metadata.name], {self._input_metadata.name: feed}
                )
            except Exception:
                raise OnnxBackendError("ONNX inference failed.") from None
            # A provider can be changed by a hostile/mock session while run
            # is in progress; verify again before publishing its output.
            self._assert_provider_identity(self._session)
            self._assert_session_contract_unchanged()
            outputs = (result,) if isinstance(result, np.ndarray) else result
            if isinstance(outputs, str | bytes) or outputs is None:
                raise OnnxBackendError("ONNX inference returned an invalid output.")
            try:
                output_values = tuple(cast(Sequence[object], outputs))
            except (TypeError, ValueError):
                raise OnnxBackendError("ONNX inference returned an invalid output.") from None
            if len(output_values) != 1 or not isinstance(output_values[0], np.ndarray):
                raise OnnxBackendError("ONNX inference returned an invalid output.")
            output = output_values[0]
            if (
                output.dtype != np.dtype(np.float32)
                or output.shape != self._output_metadata.shape
                or not np.isfinite(output).all()
            ):
                raise OnnxBackendError("ONNX inference returned an invalid output tensor.")
            stable_output = np.array(output, dtype=np.float32, copy=True, order="C")
            stable_output.setflags(write=False)
            return DetectorOutput(
                output=stable_output,
                provider=self._requested_provider,
                input_name=self._input_metadata.name,
                output_name=self._output_metadata.name,
                input_shape=self._input_metadata.shape,
                output_shape=self._output_metadata.shape,
            )


__all__ = [
    "DEFAULT_ONNX_BACKEND_CONFIG",
    "OnnxBackendConfig",
    "OnnxBackendError",
    "OnnxDetectorBackend",
]
