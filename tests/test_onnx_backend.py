from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from maple_automation_core.vision import (
    OnnxBackendConfig,
    OnnxBackendError,
    OnnxDetectorBackend,
)


def _write_model(root: Path, relative: str = "models/detector.onnx") -> tuple[str, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"synthetic onnx model bytes\x00\x01"
    path.write_bytes(payload)
    return relative, hashlib.sha256(payload).hexdigest()


def _node(name: str = "images", shape: tuple[int, ...] = (1, 3, 640, 640)) -> Any:
    return SimpleNamespace(name=name, type="tensor(float)", shape=list(shape))


def _output_node(name: str = "output0", shape: tuple[int, ...] = (1, 5, 8400)) -> Any:
    return SimpleNamespace(name=name, type="tensor(float)", shape=list(shape))


class _Session:
    def __init__(
        self,
        *,
        provider: str = "CPUExecutionProvider",
        input_node: Any | None = None,
        output_node: Any | None = None,
    ) -> None:
        self.provider = provider
        self.input_node = input_node or _node()
        self.output_node = output_node or _output_node()
        self.calls: list[tuple[list[str], dict[str, np.ndarray]]] = []

    def get_providers(self) -> list[str]:
        return [self.provider]

    def get_inputs(self) -> list[Any]:
        return [self.input_node]

    def get_outputs(self) -> list[Any]:
        return [self.output_node]

    def run(self, output_names: list[str], feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.calls.append((output_names, feed))
        return [np.zeros(tuple(self.output_node.shape), dtype=np.float32)]


class _Runtime:
    def __init__(self, providers: tuple[str, ...] = ("CPUExecutionProvider",)) -> None:
        self.available = providers
        self.__version__ = "fixture-1.0"

    def get_available_providers(self) -> tuple[str, ...]:
        return self.available


def _backend(
    root: Path,
    *,
    session: _Session | None = None,
    runtime: _Runtime | None = None,
    factory_calls: list[tuple[object, list[str]]] | None = None,
    **kwargs: object,
) -> OnnxDetectorBackend:
    relative, digest = _write_model(root)
    session = session or _Session()
    calls = factory_calls if factory_calls is not None else []

    def factory(path: object, *, providers: list[str]) -> _Session:
        calls.append((path, providers))
        return session

    return OnnxDetectorBackend(
        external_root=root,
        model_relative_path=relative,
        model_sha256=digest,
        requested_provider="CPUExecutionProvider",
        runtime=runtime,
        session_factory=factory,
        **kwargs,
    )


@pytest.mark.parametrize(
    "relative", ["../detector.onnx", "nested/../../detector.onnx", "C:/detector.onnx"]
)
def test_path_escape_is_rejected_before_session_factory(tmp_path: Path, relative: str) -> None:
    model = tmp_path / "detector.onnx"
    model.write_bytes(b"outside")
    calls: list[tuple[object, list[str]]] = []
    with pytest.raises(OnnxBackendError, match="relative|normalized") as raised:
        OnnxDetectorBackend(
            external_root=tmp_path,
            model_relative_path=relative,
            model_sha256=hashlib.sha256(b"outside").hexdigest(),
            requested_provider="CPUExecutionProvider",
            session_factory=lambda payload, *, providers: calls.append((payload, providers)),
        )
    assert calls == []
    assert str(tmp_path) not in str(raised.value)


def test_missing_model_and_hash_mismatch_do_not_create_a_session(tmp_path: Path) -> None:
    calls: list[tuple[object, list[str]]] = []
    with pytest.raises(OnnxBackendError, match="unavailable"):
        OnnxDetectorBackend(
            external_root=tmp_path,
            model_relative_path="missing.onnx",
            model_sha256="a" * 64,
            requested_provider="CPUExecutionProvider",
            session_factory=lambda payload, *, providers: calls.append((payload, providers)),
        )
    assert calls == []

    relative, _ = _write_model(tmp_path)
    with pytest.raises(OnnxBackendError, match="does not match"):
        OnnxDetectorBackend(
            external_root=tmp_path,
            model_relative_path=relative,
            model_sha256="b" * 64,
            requested_provider="CPUExecutionProvider",
            session_factory=lambda payload, *, providers: calls.append((payload, providers)),
        )
    assert calls == []


def test_symlink_escape_is_rejected_before_hash_and_session(tmp_path: Path) -> None:
    outside = tmp_path.parent / "onnx-backend-outside.onnx"
    outside.write_bytes(b"outside")
    root = tmp_path / "external"
    root.mkdir()
    link = root / "detector.onnx"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    calls: list[tuple[object, list[str]]] = []
    with pytest.raises(OnnxBackendError, match="unavailable"):
        OnnxDetectorBackend(
            external_root=root,
            model_relative_path="detector.onnx",
            model_sha256=hashlib.sha256(b"outside").hexdigest(),
            requested_provider="CPUExecutionProvider",
            session_factory=lambda payload, *, providers: calls.append((payload, providers)),
        )
    assert calls == []


def test_provider_unavailable_is_rejected_before_session_factory(tmp_path: Path) -> None:
    calls: list[tuple[object, list[str]]] = []
    relative, digest = _write_model(tmp_path)
    with pytest.raises(OnnxBackendError, match="unavailable"):
        OnnxDetectorBackend(
            external_root=tmp_path,
            model_relative_path=relative,
            model_sha256=digest,
            requested_provider="CudaExecutionProvider",
            runtime=_Runtime(("CPUExecutionProvider",)),
            session_factory=lambda payload, *, providers: calls.append((payload, providers)),
        )
    assert calls == []


@pytest.mark.parametrize(
    ("session", "message"),
    [
        (_Session(input_node=_node(name="input")), "input metadata"),
        (_Session(input_node=_node(shape=(1, 3, 320, 640))), "input metadata"),
        (
            _Session(
                input_node=SimpleNamespace(
                    name="images", type="tensor(float16)", shape=[1, 3, 640, 640]
                )
            ),
            "input metadata",
        ),
        (_Session(output_node=_output_node(name="scores")), "output metadata"),
        (_Session(output_node=_output_node(shape=(1, 5, 100))), "output metadata"),
        (
            _Session(
                output_node=SimpleNamespace(
                    name="output0", type="tensor(float16)", shape=[1, 5, 8400]
                )
            ),
            "output metadata",
        ),
    ],
)
def test_session_metadata_drift_fails_closed(
    tmp_path: Path, session: _Session, message: str
) -> None:
    calls: list[tuple[object, list[str]]] = []
    with pytest.raises(OnnxBackendError, match=message) as raised:
        _backend(tmp_path, session=session, factory_calls=calls)
    assert len(calls) == 1
    assert calls[0][1] == ["CPUExecutionProvider"]
    assert isinstance(calls[0][0], bytes)
    assert calls[0][0] == b"synthetic onnx model bytes\x00\x01"
    assert str(tmp_path) not in str(raised.value)


def test_actual_provider_drift_is_rejected_without_fallback(tmp_path: Path) -> None:
    calls: list[tuple[object, list[str]]] = []
    with pytest.raises(OnnxBackendError, match="does not match"):
        _backend(
            tmp_path,
            session=_Session(provider="FixtureExecutionProvider"),
            runtime=_Runtime(("CPUExecutionProvider", "FixtureExecutionProvider")),
            factory_calls=calls,
        )
    # The fake runtime says the requested provider is available only when it
    # is configured accordingly; this case verifies session identity after
    # construction with a requested provider matching the backend.
    assert len(calls) == 1


def test_runtime_import_is_lazy_when_dependencies_are_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import maple_automation_core.vision.onnx_backend as module

    imported: list[str] = []
    original = module.importlib.import_module

    def observe_import(name: str, package: str | None = None) -> Any:
        imported.append(name)
        return original(name, package)

    monkeypatch.setattr(module.importlib, "import_module", observe_import)
    _backend(tmp_path)
    assert "onnxruntime" not in imported


def test_runtime_is_loaded_only_at_backend_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import maple_automation_core.vision.onnx_backend as module

    runtime = _Runtime()
    runtime.InferenceSession = lambda path, *, providers: _Session()  # type: ignore[attr-defined]
    imported: list[str] = []

    def fake_import(name: str) -> Any:
        imported.append(name)
        assert name == "onnxruntime"
        return runtime

    monkeypatch.setattr(module.importlib, "import_module", fake_import)
    relative, digest = _write_model(tmp_path)
    backend = OnnxDetectorBackend(
        external_root=tmp_path,
        model_relative_path=relative,
        model_sha256=digest,
        requested_provider="CPUExecutionProvider",
    )
    assert imported == ["onnxruntime"]
    assert backend.providers == ("CPUExecutionProvider",)
    assert backend.runtime_version == "fixture-1.0"


def test_runtime_construction_sources_are_unambiguous(tmp_path: Path) -> None:
    relative, digest = _write_model(tmp_path)
    with pytest.raises(OnnxBackendError, match="mutually exclusive"):
        OnnxDetectorBackend(
            external_root=tmp_path,
            model_relative_path=relative,
            model_sha256=digest,
            requested_provider="CPUExecutionProvider",
            runtime=_Runtime(),
            runtime_factory=_Runtime,
        )

    with pytest.raises(OnnxBackendError, match="invalid"):
        OnnxDetectorBackend(
            external_root=tmp_path,
            model_relative_path=relative,
            model_sha256=digest,
            requested_provider=" CPUExecutionProvider",
            session_factory=lambda path, *, providers: _Session(),
        )


def test_successful_inference_is_repeatable_and_returns_actual_metadata(tmp_path: Path) -> None:
    calls: list[tuple[object, list[str]]] = []
    session = _Session()
    backend = _backend(tmp_path, session=session, factory_calls=calls)
    tensor = np.zeros((1, 3, 640, 640), dtype=np.float32)

    first = backend.infer(
        tensor,
        provider="CPUExecutionProvider",
        input_name="images",
        output_name="output0",
    )
    second = backend.infer(
        tensor,
        provider="CPUExecutionProvider",
        input_name="images",
        output_name="output0",
    )
    third = backend.infer(
        tensor,
        provider="CPUExecutionProvider",
        input_name="images",
        output_name="output0",
    )

    assert calls[0][1] == ["CPUExecutionProvider"]
    assert len(session.calls) == 3
    assert first.provider == second.provider == third.provider == "CPUExecutionProvider"
    assert first.input_name == second.input_name == third.input_name == "images"
    assert first.output_name == second.output_name == third.output_name == "output0"
    assert first.input_shape == (1, 3, 640, 640)
    assert first.output_shape == (1, 5, 8400)
    assert first.output.dtype == second.output.dtype == np.float32
    assert np.array_equal(first.output, second.output)
    assert np.array_equal(second.output, third.output)
    assert not first.output.flags.writeable


@pytest.mark.parametrize(
    "tensor",
    [
        np.zeros((1, 3, 640, 640), dtype=np.float64),
        np.zeros((1, 3, 320, 640), dtype=np.float32),
        np.full((1, 3, 640, 640), np.nan, dtype=np.float32),
    ],
)
def test_infer_rejects_input_dtype_shape_and_nonfinite_values(
    tmp_path: Path, tensor: np.ndarray
) -> None:
    backend = _backend(tmp_path)
    with pytest.raises(OnnxBackendError, match="input"):
        backend.infer(
            tensor,  # type: ignore[arg-type]
            provider="CPUExecutionProvider",
            input_name="images",
            output_name="output0",
        )

    with pytest.raises(OnnxBackendError, match="provider"):
        backend.infer(
            np.zeros((1, 3, 640, 640), dtype=np.float32),
            provider="FixtureExecutionProvider",
            input_name="images",
            output_name="output0",
        )


def test_infer_rejects_metadata_argument_drift(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    tensor = np.zeros((1, 3, 640, 640), dtype=np.float32)
    with pytest.raises(OnnxBackendError, match="names"):
        backend.infer(
            tensor,
            provider="CPUExecutionProvider",
            input_name="wrong",
            output_name="output0",
        )


def test_immutable_expected_config_supports_a_small_fixture_contract(tmp_path: Path) -> None:
    config = OnnxBackendConfig(input_shape=(1, 3, 4, 4), output_shape=(1, 5, 2))
    session = _Session(
        input_node=_node(shape=(1, 3, 4, 4)),
        output_node=_output_node(shape=(1, 5, 2)),
    )
    relative, digest = _write_model(tmp_path)
    backend = OnnxDetectorBackend(
        external_root=tmp_path,
        model_relative_path=relative,
        model_sha256=digest,
        requested_provider="CPUExecutionProvider",
        session_factory=lambda path, *, providers: session,
        expected_config=config,
    )
    result = backend.infer(
        np.zeros((1, 3, 4, 4), dtype=np.float32),
        provider="CPUExecutionProvider",
        input_name="images",
        output_name="output0",
    )
    assert result.output_shape == (1, 5, 2)
