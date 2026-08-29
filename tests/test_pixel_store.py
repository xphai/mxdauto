from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import types
from array import array
from pathlib import Path

import numpy as np
import pytest
from jsonschema import Draft202012Validator

import maple_automation_core.capture.pixel_store as pixel_store_module
from maple_automation_core.capture.pixel_store import (
    DEFAULT_LENGTH,
    DEFAULT_PIXEL_SPEC,
    CaptureProvenance,
    CaptureSourceProvenance,
    ContentAddressedPixelStore,
    FramePixelArtifact,
    PixelArtifact,
    PixelIntegrityError,
    PixelPathError,
    PixelSpec,
    PixelStore,
    canonical_json,
    canonical_json_bytes,
    canonical_pixel_digest,
    cas_path,
    compute_pixel_digest,
    derive_cas_path,
    device_fingerprint_sha256,
    digest_pixels,
    encoded_hash,
    encoded_sha256,
    hash_physical_device_fingerprint,
    physical_device_fingerprint_hash,
    pixel_digest,
    pixel_sha256,
    read_pixel_artifact,
    validate_pixels,
    verify_pixel_digest,
    write_pixel_artifact,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _spec() -> PixelSpec:
    return PixelSpec(width=2, height=2)


def _pixels() -> bytes:
    return bytes(range(12))


def test_pixel_spec_defaults_and_known_answer() -> None:
    assert DEFAULT_PIXEL_SPEC.width == 1920
    assert DEFAULT_PIXEL_SPEC.height == 1080
    assert DEFAULT_PIXEL_SPEC.channels == 3
    assert DEFAULT_PIXEL_SPEC.stride == 5760
    assert DEFAULT_PIXEL_SPEC.length == DEFAULT_LENGTH == 6_220_800
    assert (
        canonical_pixel_digest(DEFAULT_PIXEL_SPEC, bytes(DEFAULT_LENGTH))
        == "c23a85d7fe7002f426293d40fb9a02a8795c41f7ef7ea801b082a969793ab4bc"
    )


def test_digest_preimage_is_exact_and_format_bound() -> None:
    spec = _spec()
    pixels = _pixels()
    expected = hashlib.sha256(
        b"MAPLE_PIXEL_V1\0" + canonical_json(spec) + b"\0" + pixels
    ).hexdigest()
    assert canonical_pixel_digest(spec, pixels) == expected
    assert canonical_pixel_digest(PixelSpec(width=3, height=1), pixels[:9]) != expected
    assert encoded_sha256(pixels) != expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": 0},
        {"height": 0},
        {"channels": 4},
        {"pixel_format": "RGB8"},
        {"dtype": "uint16"},
        {"width": 2, "height": 2, "stride": 7},
        {"width": 2, "height": 2, "length": 13},
    ],
)
def test_pixel_spec_rejects_noncanonical_layout(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        PixelSpec(**kwargs)  # type: ignore[arg-type]


def test_cas_put_get_is_immutable_and_records_separate_encoded_hash(tmp_path: Path) -> None:
    mutable = bytearray(_pixels())
    store = PixelStore(tmp_path / "cas")
    digest = store.put(
        _spec(),
        mutable,
        encoded_bytes=b"container-payload",
        session_id="session-a",
        source_sequence=4,
    )
    mutable[0] = 255
    assert store.read(digest) == _pixels()
    artifact = store.artifact(digest)
    assert artifact.pixel_digest == digest
    assert artifact.image_ref == f"cas://sha256/{digest}"
    assert artifact.encoded_sha256 == encoded_sha256(_pixels())
    assert artifact.encoded_sha256 != digest
    assert artifact.source_encoded_sha256 is None
    assert artifact.source_encoded_size is None
    assert artifact.session_id == "cas-object-v1"
    occurrence = store.occurrence(
        digest,
        source_provenance_id="unknown",
        session_id="session-a",
        source_sequence=4,
    )
    assert occurrence.source_encoded_sha256 == encoded_sha256(b"container-payload")
    assert occurrence.source_encoded_size == len(b"container-payload")
    assert occurrence.session_id == "session-a"
    assert occurrence.source_sequence == 4
    assert store.put(_spec(), _pixels(), encoded_bytes=b"container-payload") == digest


def test_cas_artifact_schema_and_roundtrip(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    artifact = store.put_artifact(_spec(), _pixels())
    payload = artifact.to_dict()
    schema = json.loads(
        (PROJECT_ROOT / "schemas" / "frame-pixel-artifact.schema.json").read_text(encoding="utf-8")
    )
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []
    assert type(artifact).from_dict(json.loads(json.dumps(payload))) == artifact


def test_cas_fail_closed_on_length_spec_hash_and_metadata_tamper(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    spec = _spec()
    digest = store.put(spec, _pixels())
    with pytest.raises(PixelIntegrityError):
        store.read(digest, PixelSpec(width=3, height=1))

    raw = store.path_for(digest)
    raw.write_bytes(b"short")
    with pytest.raises(PixelIntegrityError):
        store.read(digest, spec)

    store = PixelStore(tmp_path / "other-cas")
    digest = store.put(spec, _pixels())
    metadata = store.metadata_path_for(digest)
    value = json.loads(metadata.read_text(encoding="utf-8"))
    value["pixel_digest"] = "0" * 64
    metadata.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(PixelIntegrityError):
        store.read(digest)


def test_cas_rejects_traversal_and_symlink_escape(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    with pytest.raises(ValueError):
        store.path_for("../" + "a" * 62)

    spec = _spec()
    digest = canonical_pixel_digest(spec, _pixels())
    store.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (store.root / digest[:2]).symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this test host")
    with pytest.raises(PixelPathError):
        store.put(spec, _pixels())
    assert list(outside.iterdir()) == []


def test_cas_lock_symlink_fails_closed(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    store.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "lock-target"
    try:
        (store.root / ".pixel-store.lock").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this test host")

    with pytest.raises(PixelPathError, match="lock file"):
        store.put(_spec(), _pixels())
    assert not target.exists()
    assert list(outside.iterdir()) == []


def test_cas_lock_hardlink_fails_closed_without_writing_external_file(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    store.root.mkdir()
    outside = tmp_path / "outside-lock"
    sentinel = b"external-lock-sentinel"
    outside.write_bytes(sentinel)
    try:
        (store.root / ".pixel-store.lock").hardlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable on this test host")

    with pytest.raises(PixelPathError, match="hard link"):
        store.put(_spec(), _pixels())
    assert outside.read_bytes() == sentinel


def test_same_pixels_have_distinct_immutable_occurrences(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    provenance = "a" * 64
    first = store.put_artifact(
        _spec(),
        _pixels(),
        source_provenance_id=provenance,
        session_id="session-a",
        source_sequence=1,
    )
    second = store.put_artifact(
        _spec(),
        _pixels(),
        source_provenance_id=provenance,
        session_id="session-b",
        source_sequence=99,
    )

    assert first.pixel_digest == second.pixel_digest
    assert first.artifact_sha256 != second.artifact_sha256
    assert store.artifact(first.pixel_digest).session_id == "cas-object-v1"
    assert (
        store.occurrence(
            first.pixel_digest,
            source_provenance_id=provenance,
            session_id="session-a",
            source_sequence=1,
        )
        == first
    )
    assert (
        store.occurrence(
            second.pixel_digest,
            source_provenance_id=provenance,
            session_id="session-b",
            source_sequence=99,
        )
        == second
    )


def test_same_occurrence_key_rejects_conflicting_metadata(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    fields = {
        "source_provenance_id": "a" * 64,
        "session_id": "session-a",
        "source_sequence": 1,
    }
    store.put(_spec(), _pixels(), encoded_bytes=b"container-a", **fields)
    with pytest.raises(PixelIntegrityError, match="occurrence conflicts"):
        store.put(_spec(), _pixels(), encoded_bytes=b"container-b", **fields)


def test_read_requires_object_metadata_even_with_explicit_spec(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    digest = store.put(_spec(), _pixels())
    store.metadata_path_for(digest).unlink()
    with pytest.raises(PixelIntegrityError, match="metadata is required"):
        store.read(digest, _spec())
    assert not store.exists(digest, _spec())


def test_resigned_object_occurrence_fields_break_object_fact_contract(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    digest = store.put(_spec(), _pixels(), session_id="session-a", source_sequence=7)
    metadata = store.metadata_path_for(digest)
    value = json.loads(metadata.read_text(encoding="utf-8"))
    value["session_id"] = "session-tampered"
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    value["artifact_sha256"] = hashlib.sha256(canonical_json(body)).hexdigest()
    metadata.write_bytes(canonical_json(value) + b"\n")

    with pytest.raises(PixelIntegrityError, match="immutable object facts"):
        store.read(digest)


def test_double_resigned_object_and_occurrence_cannot_promote_policy(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    digest = store.put(_spec(), _pixels(), session_id="session-a", source_sequence=7)
    paths = [
        store.metadata_path_for(digest),
        store.occurrence_path_for(digest, "unknown", "session-a", 7),
    ]
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["privacy_class"] = "restricted"
        body = {key: item for key, item in value.items() if key != "artifact_sha256"}
        value["artifact_sha256"] = hashlib.sha256(canonical_json(body)).hexdigest()
        path.write_bytes(canonical_json(value) + b"\n")

    with pytest.raises(PixelIntegrityError, match="immutable object facts"):
        store.read(digest)


def test_derivation_is_complete_acyclic_and_parent_must_exist(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    spec = _spec()
    digest = canonical_pixel_digest(spec, _pixels())
    with pytest.raises(ValueError, match="all null.*all present"):
        store.put_artifact(spec, _pixels(), transform_version="transform-v1")
    with pytest.raises(ValueError, match="must not equal"):
        store.put_artifact(
            spec,
            _pixels(),
            parent_pixel_digest=digest,
            transform_version="transform-v1",
            calibration_sha256="1" * 64,
        )
    with pytest.raises(PixelIntegrityError):
        store.put_artifact(
            spec,
            _pixels(),
            parent_pixel_digest="2" * 64,
            transform_version="transform-v1",
            calibration_sha256="1" * 64,
        )

    parent = store.put_artifact(spec, bytes(reversed(_pixels())))
    child = store.put_artifact(
        spec,
        _pixels(),
        source_sequence=1,
        parent_pixel_digest=parent.pixel_digest,
        transform_version="transform-v1",
        calibration_sha256="1" * 64,
    )
    assert child.parent_pixel_digest == parent.pixel_digest

    with pytest.raises(PixelIntegrityError, match="cycle"):
        store.put_artifact(
            spec,
            bytes(reversed(_pixels())),
            source_sequence=2,
            parent_pixel_digest=child.pixel_digest,
            transform_version="transform-v2",
            calibration_sha256="2" * 64,
        )

    third_pixels = _pixels()[3:] + _pixels()[:3]
    third = store.put_artifact(
        spec,
        third_pixels,
        source_sequence=3,
        parent_pixel_digest=child.pixel_digest,
        transform_version="transform-v3",
        calibration_sha256="3" * 64,
    )
    with pytest.raises(PixelIntegrityError, match="cycle"):
        store.put_artifact(
            spec,
            bytes(reversed(_pixels())),
            source_sequence=4,
            parent_pixel_digest=third.pixel_digest,
            transform_version="transform-v4",
            calibration_sha256="4" * 64,
        )


def test_parentless_live_put_skips_unchanged_graph_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PixelStore(tmp_path / "cas")
    spec = _spec()
    calls = 0
    original = store._verify_derivation_dag

    def verify_graph() -> None:
        nonlocal calls
        calls += 1
        original()

    monkeypatch.setattr(store, "_verify_derivation_dag", verify_graph)
    parent = store.put_artifact(spec, _pixels(), source_sequence=1)
    assert calls == 0

    store.put_artifact(
        spec,
        bytes(reversed(_pixels())),
        source_sequence=2,
        parent_pixel_digest=parent.pixel_digest,
        transform_version="transform-v1",
        calibration_sha256="1" * 64,
    )
    assert calls == 1


def test_concurrent_derivation_append_cannot_create_two_node_cycle(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    spec = _spec()
    left_pixels = _pixels()
    right_pixels = bytes(reversed(_pixels()))
    left = store.put_artifact(spec, left_pixels)
    right = store.put_artifact(spec, right_pixels)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def append_edge(pixels: bytes, parent: str, sequence: int) -> None:
        barrier.wait()
        try:
            store.put_artifact(
                spec,
                pixels,
                source_sequence=sequence,
                parent_pixel_digest=parent,
                transform_version="concurrent-v1",
                calibration_sha256="5" * 64,
            )
        except PixelIntegrityError:
            outcomes.append("rejected")
        else:
            outcomes.append("accepted")

    threads = [
        threading.Thread(target=append_edge, args=(left_pixels, right.pixel_digest, 1)),
        threading.Thread(target=append_edge, args=(right_pixels, left.pixel_digest, 1)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2.0)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["accepted", "rejected"]


def test_cross_process_derivation_append_is_one_edge_and_acyclic(tmp_path: Path) -> None:
    """Two interpreters must serialize parent/graph validation and publication."""

    store = PixelStore(tmp_path / "cas")
    spec = _spec()
    left_pixels = _pixels()
    right_pixels = bytes(reversed(_pixels()))
    left = store.put_artifact(spec, left_pixels)
    right = store.put_artifact(spec, right_pixels)
    release = tmp_path / "release"
    script = textwrap.dedent(
        """
        import json
        import sys
        import time
        from pathlib import Path

        from maple_automation_core.capture.pixel_store import PixelSpec, PixelStore

        root = Path(sys.argv[1])
        ready_path = Path(sys.argv[2])
        result_path = Path(sys.argv[3])
        direction = sys.argv[4]
        parent = sys.argv[5]
        role = sys.argv[6]
        marker_path = Path(sys.argv[7])
        release_path = Path(sys.argv[8])
        go_path = Path(sys.argv[9])
        attempting_path = Path(sys.argv[10])
        ready_path.write_text("ready", encoding="ascii")
        if role == "contender":
            deadline = time.monotonic() + 15.0
            while not go_path.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("parent did not release the contender barrier")
                time.sleep(0.005)

        store = PixelStore(root)
        original = store._reject_derivation_cycle

        def widened(child, parent_digest):
            marker_path.write_text("entered", encoding="ascii")
            result = original(child, parent_digest)
            if role == "holder":
                deadline = time.monotonic() + 15.0
                while not release_path.exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("parent did not release the holder")
                    time.sleep(0.005)
            return result

        store._reject_derivation_cycle = widened
        spec = PixelSpec(width=2, height=2)
        pixels = bytes(range(12)) if direction == "a-to-b" else bytes(reversed(range(12)))
        attempting_path.write_text("attempting", encoding="ascii")
        try:
            artifact = store.put_artifact(
                spec,
                pixels,
                source_provenance_id="proc-" + direction,
                session_id="proc-session",
                source_sequence=1,
                parent_pixel_digest=parent,
                transform_version="proc-transform",
                calibration_sha256="9" * 64,
            )
        except Exception as exc:
            result = {
                "status": "rejected",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        else:
            result = {
                "status": "accepted",
                "digest": artifact.pixel_digest,
                "parent": artifact.parent_pixel_digest,
            }
        result_path.write_text(json.dumps(result), encoding="utf-8")
        """
    )
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = source_path + os.pathsep + environment.get("PYTHONPATH", "")
    processes: list[subprocess.Popen[str]] = []
    holder_marker = tmp_path / "holder-entered"
    contender_ready = tmp_path / "contender-ready"
    contender_marker = tmp_path / "contender-entered"
    contender_attempting = tmp_path / "contender-attempting"
    contender_go = tmp_path / "contender-go"
    holder_ready = tmp_path / "holder-ready"
    try:
        holder_result = tmp_path / "result-holder.json"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(store.root),
                str(holder_ready),
                str(holder_result),
                "a-to-b",
                right.pixel_digest,
                "holder",
                str(holder_marker),
                str(release),
                str(tmp_path / "holder-go"),
                str(tmp_path / "holder-attempting"),
            ],
            env=environment,
            text=True,
        )
        processes.append(holder)

        deadline = time.monotonic() + 15.0
        while not holder_marker.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("holder process did not acquire the CAS transaction")
            time.sleep(0.01)

        contender_result = tmp_path / "result-contender.json"
        contender = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(store.root),
                str(contender_ready),
                str(contender_result),
                "b-to-a",
                left.pixel_digest,
                "contender",
                str(contender_marker),
                str(release),
                str(contender_go),
                str(contender_attempting),
            ],
            env=environment,
            text=True,
        )
        processes.append(contender)
        deadline = time.monotonic() + 15.0
        while not contender_ready.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("contender process did not reach the barrier")
            time.sleep(0.01)
        contender_go.write_text("go", encoding="ascii")
        deadline = time.monotonic() + 15.0
        while not contender_attempting.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("contender process did not attempt the CAS transaction")
            time.sleep(0.01)
        time.sleep(0.3)
        assert not contender_marker.exists(), "contender entered scan before holder released"
        release.write_text("release", encoding="ascii")
        for process in processes:
            assert process.wait(timeout=15.0) == 0
    finally:
        release.write_text("release", encoding="ascii")
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5.0)

    outcomes = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "result-holder.json", tmp_path / "result-contender.json")
    ]
    assert sorted(outcome["status"] for outcome in outcomes) == ["accepted", "rejected"]
    rejected = next(outcome for outcome in outcomes if outcome["status"] == "rejected")
    assert rejected["error_type"] == "PixelIntegrityError"
    assert "cycle" in rejected["error"]
    assert sum(len(parents) for parents in store._derivation_edges().values()) == 1
    store._verify_derivation_dag()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("storage_encoding", "jpeg"),
        ("privacy_class", "public_unreviewed"),
        ("retention_class", "forever"),
    ],
)
def test_put_rejects_unfrozen_storage_and_policy_values(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    store = PixelStore(tmp_path / "cas")
    with pytest.raises(ValueError):
        store.put(_spec(), _pixels(), **{field: value})
    assert not store.root.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_provenance_id", r"F:\\Users\\Alice\\raw"),
        ("session_id", r"C:\\Users\\Alice"),
        ("session_id", "session with spaces"),
        ("source_provenance_id", "source/路径"),
    ],
)
def test_occurrence_identifiers_reject_path_or_identifying_tokens_before_write(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    store = PixelStore(tmp_path / "cas")
    with pytest.raises(ValueError, match="portable token"):
        store.put_artifact(_spec(), _pixels(), **{field: value})
    assert not store.root.exists()


def test_relative_store_root_is_frozen_across_cwd_changes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    original = Path.cwd()
    try:
        os.chdir(first)
        store = PixelStore("cas")
        digest = store.put(_spec(), _pixels())
        os.chdir(second)
        assert store.read(digest) == _pixels()
        assert store.root == first / "cas"
        assert not (second / "cas").exists()
    finally:
        os.chdir(original)


def test_strict_json_rejects_nonfinite_cycles_nonstring_keys_and_objects() -> None:
    assert canonical_json({"z": (1, 2), "a": [True, None]}) == (b'{"a":[true,null],"z":[1,2]}')
    with pytest.raises(ValueError, match="JSON-serializable"):
        canonical_json(float("nan"))
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="cyclic"):
        canonical_json(cyclic)
    with pytest.raises(ValueError, match="keys must be strings"):
        canonical_json({1: "not-a-json-key"})
    with pytest.raises(ValueError, match="JSON-serializable"):
        canonical_json({"unsupported": {"set-value"}})

    # Keep the serializer exception guard covered even if a future stdlib
    # serializer grows support for another value type.
    original_dumps = pixel_store_module.json.dumps

    def fail_dumps(*args: object, **kwargs: object) -> str:
        raise TypeError("synthetic serializer failure")

    pixel_store_module.json.dumps = fail_dumps  # type: ignore[assignment]
    try:
        with pytest.raises(ValueError, match="strict JSON"):
            canonical_json({"ok": True})
    finally:
        pixel_store_module.json.dumps = original_dumps


def test_pixel_spec_aliases_and_mapping_contracts() -> None:
    spec = _spec()
    assert spec.stride_bytes == spec.stride == 6
    assert spec.byte_length == spec.length == 12
    assert spec.shape == (2, 2, 3)
    assert spec.is_packed
    assert PixelSpec.from_dict(spec.to_dict()) == spec
    assert canonical_json_bytes(spec) == canonical_json(spec)
    assert compute_pixel_digest(spec, _pixels()) == pixel_digest(spec, _pixels())
    assert pixel_sha256(spec, _pixels()) == digest_pixels(spec, _pixels())
    with pytest.raises(ValueError, match="mapping"):
        PixelSpec.from_dict([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown keys"):
        PixelSpec.from_dict({**spec.to_dict(), "extra": 1})
    missing = spec.to_dict()
    del missing["stride"]
    with pytest.raises(ValueError, match="missing key: stride"):
        PixelSpec.from_dict(missing)
    with pytest.raises(ValueError, match="pixel_format"):
        PixelSpec(width=2, height=2, pixel_format=" ")


def test_pixel_buffer_validation_supports_flat_and_packed_3d_views() -> None:
    spec = _spec()
    packed = np.arange(12, dtype=np.uint8).reshape(spec.shape)
    assert validate_pixels(spec, packed) == _pixels()
    assert validate_pixels(spec, bytearray(_pixels())) == _pixels()
    assert verify_pixel_digest(pixel_digest(spec, _pixels()), spec, packed)
    assert not verify_pixel_digest("0" * 64, spec, packed)
    with pytest.raises(TypeError, match="PixelSpec"):
        validate_pixels(object(), _pixels())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="contiguous uint8"):
        validate_pixels(spec, object())
    with pytest.raises(ValueError, match="uint8"):
        validate_pixels(spec, array("H", [0] * 6))
    with pytest.raises(ValueError, match="C-contiguous"):
        validate_pixels(spec, memoryview(bytearray(range(24)))[::2])
    with pytest.raises(ValueError, match="shape"):
        validate_pixels(spec, np.arange(12, dtype=np.uint8).reshape(1, 4, 3))
    with pytest.raises(ValueError, match="flat buffer"):
        validate_pixels(spec, np.arange(12, dtype=np.uint8).reshape(2, 6))
    with pytest.raises(ValueError, match="exactly 12"):
        validate_pixels(spec, bytes(11))
    with pytest.raises(ValueError, match="expected must"):
        verify_pixel_digest("not-a-digest", spec, _pixels())


def test_encoded_hash_boundaries() -> None:
    assert encoded_hash(b"payload") == encoded_sha256(b"payload")
    with pytest.raises(TypeError, match="byte buffer"):
        encoded_sha256(object())
    with pytest.raises(ValueError, match="contain bytes"):
        encoded_sha256(array("H", [1]))
    with pytest.raises(ValueError, match="C-contiguous"):
        encoded_sha256(memoryview(bytearray(range(8)))[::2])
    with pytest.raises(TypeError, match="byte buffer"):
        pixel_store_module._buffer_length(object())
    with pytest.raises(ValueError, match="contiguous byte buffer"):
        pixel_store_module._buffer_length(array("H", [1]))
    with pytest.raises(ValueError, match="contiguous byte buffer"):
        pixel_store_module._buffer_length(memoryview(bytearray(range(8)))[::2])


def _valid_artifact_kwargs() -> dict[str, object]:
    spec = _spec()
    pixels = _pixels()
    digest = pixel_digest(spec, pixels)
    return {
        "pixel_digest": digest,
        "spec": spec,
        "byte_length": len(pixels),
        "encoded_sha256": encoded_sha256(pixels),
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("spec", object(), "PixelSpec"),
        ("byte_length", 11, "byte_length"),
        ("path", Path("not-a-string"), "path must be str"),
        ("encoded_sha256", None, "encoded_sha256 is required"),
        ("source_encoded_sha256", "a" * 64, "both be present"),
        ("schema_version", "0.0.0", "schema_version"),
        ("storage_encoding", "jpeg", "storage_encoding"),
        ("privacy_class", "public", "privacy_class"),
        ("retention_class", "forever", "retention_class"),
        ("source_provenance_id", "source/path", "portable token"),
        ("source_sequence", -1, "source_sequence"),
    ],
)
def test_pixel_artifact_constructor_rejects_invalid_metadata(
    field: str, value: object, message: str
) -> None:
    kwargs = _valid_artifact_kwargs()
    kwargs[field] = value
    with pytest.raises((TypeError, ValueError), match=message):
        PixelArtifact(**kwargs)  # type: ignore[arg-type]


def test_pixel_artifact_properties_and_aliases() -> None:
    artifact = PixelArtifact(**_valid_artifact_kwargs())  # type: ignore[arg-type]
    assert artifact.digest == artifact.pixel_digest
    assert artifact.spec_dict == artifact.spec.to_dict()
    assert artifact.pixel_spec is artifact.spec
    assert artifact.length == artifact.byte_length
    assert artifact.encoded_hash == artifact.encoded_sha256
    assert artifact.ref == artifact.image_ref == f"cas://sha256/{artifact.pixel_digest}"
    assert (
        artifact.artifact_sha256
        == hashlib.sha256(canonical_json(artifact._body_dict())).hexdigest()
    )
    assert FramePixelArtifact.from_dict(artifact.to_dict()) == artifact


def test_pixel_artifact_from_dict_rejects_unknown_aliases_refs_missing_and_hash() -> None:
    artifact = PixelArtifact(**_valid_artifact_kwargs())  # type: ignore[arg-type]
    payload = artifact.to_dict()
    with pytest.raises(ValueError, match="mapping"):
        PixelArtifact.from_dict([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown keys"):
        PixelArtifact.from_dict({**payload, "unknown": True})
    with pytest.raises(ValueError, match="spec and pixel_spec"):
        PixelArtifact.from_dict({**payload, "spec": {}, "pixel_spec": {"different": True}})
    with pytest.raises(ValueError, match="ref must"):
        PixelArtifact.from_dict({**payload, "ref": "cas://sha256/" + "0" * 64})
    with pytest.raises(ValueError, match="image_ref and ref"):
        PixelArtifact.from_dict({**payload, "image_ref": "wrong-ref"})
    for key in ("schema_version", "pixel_digest", "byte_length", "path", "artifact_sha256"):
        missing = dict(payload)
        del missing[key]
        if key == "pixel_digest":
            missing.pop("ref", None)
            missing.pop("image_ref", None)
        with pytest.raises(ValueError, match=f"missing key: {key}"):
            PixelArtifact.from_dict(missing)
    no_spec = dict(payload)
    del no_spec["spec"]
    with pytest.raises(ValueError, match="missing key: spec"):
        PixelArtifact.from_dict(no_spec)
    with pytest.raises(ValueError, match="artifact_sha256 mismatch"):
        PixelArtifact.from_dict({**payload, "artifact_sha256": "0" * 64})


def test_pixel_artifact_derivation_fields_and_path_validation() -> None:
    kwargs = _valid_artifact_kwargs()
    digest = str(kwargs["pixel_digest"])
    kwargs["path"] = f"{digest[:2]}\\{digest[2:]}"
    artifact = PixelArtifact(**kwargs)  # type: ignore[arg-type]
    assert artifact.path.replace("\\", "/") == f"{digest[:2]}/{digest[2:]}"
    for path in ("wrong", f"{digest[:2]}/{digest[2:]}-extra"):
        with pytest.raises(PixelPathError, match="canonical digest-derived"):
            pixel_store_module._validate_relative_cas_path(path, digest)
    with pytest.raises(PixelPathError, match="relative and normalized"):
        pixel_store_module._validate_relative_cas_path(f":a/{'b' * 62}", ":a" + "b" * 62)
    with pytest.raises(PixelPathError, match="traversal"):
        pixel_store_module._validate_relative_cas_path("../" + "a" * 62, ".." + "a" * 62)
    with pytest.raises(ValueError, match="all null.*all present"):
        PixelArtifact(**{**_valid_artifact_kwargs(), "transform_version": "v1"})  # type: ignore[arg-type]


def test_digest_path_helpers_and_wrappers(tmp_path: Path) -> None:
    spec = _spec()
    expected = pixel_digest(spec, _pixels())
    assert cas_path(tmp_path, expected) == tmp_path / expected[:2] / expected[2:]
    assert derive_cas_path(tmp_path, expected) == cas_path(tmp_path, expected)
    store = ContentAddressedPixelStore(tmp_path / "cas")
    artifact = write_pixel_artifact(tmp_path / "wrapped", spec, _pixels())
    assert read_pixel_artifact(tmp_path / "wrapped", artifact.pixel_digest, spec) == _pixels()
    assert store.put(_pixels(), spec) == expected
    with pytest.raises(ValueError, match="digest must"):
        cas_path(tmp_path, "not-a-digest")


def test_fingerprint_hash_aliases_and_input_boundaries() -> None:
    expected = hashlib.sha256(b"device").hexdigest()
    assert hash_physical_device_fingerprint("device") == expected
    assert hash_physical_device_fingerprint(b"device") == expected
    assert hash_physical_device_fingerprint(bytearray(b"device")) == expected
    assert hash_physical_device_fingerprint(memoryview(b"device")) == expected
    assert device_fingerprint_sha256("device") == expected
    assert physical_device_fingerprint_hash("device") == expected
    with pytest.raises(TypeError, match="text or bytes"):
        hash_physical_device_fingerprint(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must not be empty"):
        hash_physical_device_fingerprint(b"")


def test_store_argument_orders_aliases_and_encoded_contracts(tmp_path: Path) -> None:
    spec = _spec()
    store = PixelStore(tmp_path / "cas")
    digest = store.put(_pixels(), spec)
    assert store.store(spec, _pixels()) == digest
    assert store.write(_pixels(), spec) == digest
    assert store.get(digest) == store.load(digest) == store.read_pixels(digest) == _pixels()
    assert store.has(digest, spec)
    assert store.get_artifact(digest) == store.read_artifact(digest) == store.artifact(digest)

    encoded = b"container-payload"
    encoded_digest = encoded_sha256(encoded)
    assert (
        store.put(
            spec,
            _pixels(),
            encoded_sha256=encoded_digest,
            encoded_size=len(encoded),
            source_sequence=1,
        )
        == digest
    )
    assert (
        store.put(
            spec,
            _pixels(),
            encoded_hash=encoded_digest,
            encoded_size=len(encoded),
            source_sequence=2,
        )
        == digest
    )
    with pytest.raises(TypeError, match="pixels are required"):
        store.put(spec)
    with pytest.raises(TypeError, match="expects"):
        store.put(_pixels(), object())
    with pytest.raises(ValueError, match="encoded_size does not match"):
        store.put(spec, _pixels(), encoded_bytes=encoded, encoded_size=1)
    with pytest.raises(PixelIntegrityError, match="encoded_sha256 does not match"):
        store.put(spec, _pixels(), encoded_bytes=encoded, encoded_sha256="0" * 64)
    with pytest.raises(PixelIntegrityError, match="encoded_hash does not match"):
        store.put(spec, _pixels(), encoded_bytes=encoded, encoded_hash="0" * 64)
    with pytest.raises(ValueError, match="encoded_size is required"):
        store.put(spec, _pixels(), encoded_sha256=encoded_digest)
    with pytest.raises(ValueError, match="requires encoded source"):
        store.put(spec, _pixels(), encoded_size=len(encoded))
    with pytest.raises(ValueError, match="encoded_sha256"):
        store.put(spec, _pixels(), encoded_sha256="bad")
    with pytest.raises(ValueError, match="encoded_hash"):
        store.put(spec, _pixels(), encoded_hash="bad")


def test_store_default_pixel_contract_and_exists_fail_closed(tmp_path: Path) -> None:
    store = PixelStore(tmp_path / "cas")
    default_pixels = bytes(DEFAULT_LENGTH)
    digest = store.put(default_pixels)
    assert store.read(digest) == default_pixels
    assert store.exists(digest)
    assert not store.exists("f" * 64)
    assert not store.exists(digest, _spec())
    assert not store.exists("0" * 64)
    assert store.artifact(digest).spec == DEFAULT_PIXEL_SPEC


def test_store_root_validation_rejects_non_directories_and_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(TypeError, match="root must"):
        PixelStore(object())  # type: ignore[arg-type]
    store = PixelStore(tmp_path / "missing")
    with pytest.raises(PixelPathError, match="does not exist"):
        store._check_root(create=False)
    file_root = tmp_path / "root-file"
    file_root.write_bytes(b"not-a-directory")
    with pytest.raises(PixelPathError, match="directory"):
        PixelStore(file_root)._check_root(create=True)

    target = tmp_path / "target-root"
    target.mkdir()
    link_root = tmp_path / "link-root"
    try:
        link_root.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this test host")
    link_checks = 0
    original_link_check = pixel_store_module._is_symlink_or_reparse

    def report_link_after_root_loop(path: Path) -> bool:
        nonlocal link_checks
        if path == link_root:
            link_checks += 1
            return link_checks > 1
        return original_link_check(path)

    monkeypatch.setattr(pixel_store_module, "_is_symlink_or_reparse", report_link_after_root_loop)
    with pytest.raises(PixelPathError, match="root must not be a symlink"):
        PixelStore(link_root)._check_root(create=True)

    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PixelPathError, match="storage path"):
        PixelStore(parent_link / "child" / "cas")._check_root(create=True)

    created = PixelStore(tmp_path / "created")
    original_check = pixel_store_module._is_symlink_or_reparse

    def report_created_root(path: Path) -> bool:
        return path == created.root or original_check(path)

    monkeypatch.setattr(pixel_store_module, "_is_symlink_or_reparse", report_created_root)
    with pytest.raises(PixelPathError, match="real directory"):
        created._check_root(create=True)


def test_lock_identity_rejects_races_and_non_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenPath:
        def lstat(self) -> os.stat_result:
            raise OSError("synthetic stat failure")

    with pytest.raises(PixelPathError, match="stat'ed"):
        PixelStore._lock_file_identity(BrokenPath(), -1)  # type: ignore[arg-type]

    regular = tmp_path / "regular.lock"
    regular.write_bytes(b"x")
    fd = os.open(regular, os.O_RDWR)
    try:
        assert PixelStore._lock_file_identity(regular, fd)[0] == os.fstat(fd).st_dev
    finally:
        os.close(fd)

    directory = tmp_path / "directory.lock"
    directory.mkdir()
    fd = os.open(regular, os.O_RDWR)
    try:
        with pytest.raises(PixelPathError, match="regular file"):
            PixelStore._lock_file_identity(directory, fd)
    finally:
        os.close(fd)

    other = tmp_path / "other.lock"
    other.write_bytes(b"y")
    fd = os.open(other, os.O_RDWR)
    try:
        with pytest.raises(PixelPathError, match="identity changed"):
            PixelStore._lock_file_identity(regular, fd)
    finally:
        os.close(fd)

    symlink = tmp_path / "symlink.lock"
    try:
        symlink.symlink_to(regular)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this test host")
    fd = os.open(regular, os.O_RDWR)
    try:
        with pytest.raises(PixelPathError, match="symlink"):
            PixelStore._lock_file_identity(symlink, fd)
    finally:
        os.close(fd)

    calls: list[tuple[int, int]] = []
    fake_fcntl = types.SimpleNamespace(
        LOCK_EX=1,
        LOCK_UN=2,
        flock=lambda descriptor, mode: calls.append((descriptor, mode)),
    )
    lock_fd = os.open(regular, os.O_RDWR)
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    monkeypatch.setattr(pixel_store_module.os, "name", "posix")
    try:
        PixelStore._acquire_lock_descriptor(lock_fd)
        PixelStore._release_lock_descriptor(lock_fd)
    finally:
        os.close(lock_fd)
    assert [mode for _, mode in calls] == [1, 2]


def test_reparse_inspection_fails_closed_on_stat_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inspected = tmp_path / "inspected"
    inspected.write_bytes(b"fixture")
    original_lstat = Path.lstat

    def fail_inspection(path: Path) -> os.stat_result:
        if path == inspected:
            raise PermissionError("synthetic inspection denial")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_inspection)
    with pytest.raises(PixelPathError, match="inspected safely"):
        pixel_store_module._is_symlink_or_reparse(inspected)


def test_root_lock_inspection_and_open_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PixelStore(tmp_path / "cas")
    store.root.mkdir()
    original_lstat = Path.lstat

    def failing_lock_lstat(path: Path) -> os.stat_result:
        if path.name == ".pixel-store.lock":
            raise OSError("synthetic lock race")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", failing_lock_lstat)
    with pytest.raises(PixelPathError, match="inspected safely"), store._root_file_lock():
        pass

    monkeypatch.undo()

    def fail_open(*args: object, **kwargs: object) -> int:
        raise OSError("synthetic open failure")

    monkeypatch.setattr(pixel_store_module.os, "open", fail_open)
    with pytest.raises(PixelPathError, match="opened safely"), store._root_file_lock():
        pass

    monkeypatch.undo()
    original_link_check = pixel_store_module._is_symlink_or_reparse
    monkeypatch.setattr(
        pixel_store_module,
        "_is_symlink_or_reparse",
        lambda path: path.name == ".pixel-store.lock" or original_link_check(path),
    )
    monkeypatch.setattr(pixel_store_module.os, "open", fail_open)
    with pytest.raises(PixelPathError, match="must not be a symlink"), store._root_file_lock():
        pass


def test_digest_and_occurrence_directory_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PixelStore(tmp_path / "cas")
    digest = store.put(_spec(), _pixels())
    occurrence_directory = store.occurrence_directory_for(digest)
    for child in occurrence_directory.iterdir():
        child.unlink()
    occurrence_directory.rmdir()
    occurrence_directory.write_bytes(b"not-a-directory")
    with pytest.raises(PixelPathError, match="occurrence directory"):
        store._ensure_occurrence_directory(digest)

    occurrence_directory.unlink()
    original_check = pixel_store_module._is_symlink_or_reparse

    def flag_occurrence(path: Path) -> bool:
        return path == occurrence_directory or original_check(path)

    monkeypatch.setattr(pixel_store_module, "_is_symlink_or_reparse", flag_occurrence)
    with pytest.raises(PixelPathError, match="real directory"):
        store._ensure_occurrence_directory(digest)
    monkeypatch.undo()

    empty_store = PixelStore(tmp_path / "empty-cas")
    empty_raw = empty_store.path_for(digest)
    prefix = empty_raw.parent
    prefix.write_bytes(b"not-a-prefix-directory")
    with pytest.raises(PixelPathError, match="digest directory"):
        empty_store._ensure_digest_directory(digest)
    prefix.unlink()

    original_check = pixel_store_module._is_symlink_or_reparse

    def flag_prefix(path: Path) -> bool:
        return path == prefix or original_check(path)

    monkeypatch.setattr(pixel_store_module, "_is_symlink_or_reparse", flag_prefix)
    with pytest.raises(PixelPathError, match="real directory"):
        empty_store._ensure_digest_directory(digest)


def test_derivation_scan_rejects_links_unexpected_entries_and_noncanonical_records(
    tmp_path: Path,
) -> None:
    store = PixelStore(tmp_path / "cas")
    artifact = store.put_artifact(_spec(), _pixels(), source_sequence=1)
    occurrences = store.occurrence_directory_for(artifact.pixel_digest)
    unexpected = occurrences / "notes.txt"
    unexpected.write_text("ignore me", encoding="ascii")
    with pytest.raises(PixelIntegrityError, match="unexpected entry"):
        store._derivation_edges()
    unexpected.unlink()

    occurrence_path = store.occurrence_path_for(artifact.pixel_digest, "unknown", "unknown", 1)
    canonical = occurrence_path.read_bytes()
    occurrence_path.write_text(json.dumps(json.loads(canonical.decode("utf-8"))), encoding="utf-8")
    with pytest.raises(PixelIntegrityError, match="not canonical"):
        store._derivation_edges()
    occurrence_path.write_bytes(canonical)

    # Directly exercise the iterative cycle detector's back-edge branch.
    with pytest.raises(PixelIntegrityError, match="cycle"):
        PixelStore._assert_derivation_dag({"a": {"b"}, "b": {"a"}})
    PixelStore._assert_derivation_dag({"a": {"b", "c"}, "b": {"c"}})

    root = store.root
    prefix = next(path for path in root.iterdir() if path.name != ".pixel-store.lock")
    link_target = tmp_path / "outside-prefix"
    link_target.mkdir()
    link = root / "aa"
    try:
        link.symlink_to(link_target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this test host")
    with pytest.raises(PixelPathError, match="derivation scan"):
        store._derivation_edges()
    link.unlink()

    entry_target = tmp_path / "outside-entry"
    entry_target.mkdir()
    entry_link = prefix / "linked.occurrences"
    entry_link.symlink_to(entry_target, target_is_directory=True)
    try:
        with pytest.raises(PixelPathError, match="derivation scan"):
            store._derivation_edges()
    finally:
        entry_link.unlink()

    occurrence_link = occurrences / "linked.json"
    occurrence_link.symlink_to(unexpected if unexpected.exists() else occurrence_path)
    try:
        with pytest.raises(PixelPathError, match="derivation scan"):
            store._derivation_edges()
    finally:
        occurrence_link.unlink()


def test_atomic_write_and_read_helpers_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "object"
    destination.write_bytes(b"sentinel")
    link = tmp_path / "object-link"
    try:
        link.symlink_to(destination)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this test host")
    with pytest.raises(PixelPathError, match="must not be a symlink"):
        PixelStore._atomic_write(link, b"overwrite")
    assert destination.read_bytes() == b"sentinel"

    output = tmp_path / "atomic-output"
    original_replace = pixel_store_module.os.replace

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(pixel_store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        PixelStore._atomic_write(output, b"payload")
    assert list(tmp_path.glob(".atomic-output.*.tmp")) == []
    monkeypatch.setattr(pixel_store_module.os, "replace", original_replace)

    with pytest.raises(PixelIntegrityError, match="missing"):
        PixelStore._read_bytes(tmp_path / "does-not-exist", root=tmp_path)
    with pytest.raises(PixelIntegrityError, match="missing"):
        PixelStore._read_metadata(tmp_path / "does-not-exist.json", root=tmp_path)

    unreadable = tmp_path / "unreadable"
    unreadable.write_bytes(b"x")
    original_open = pixel_store_module.os.open

    def failing_open(path: object, flags: int, mode: int = 0o777) -> int:
        if Path(path) == unreadable:
            raise OSError("synthetic read failure")
        return original_open(path, flags, mode)

    monkeypatch.setattr(pixel_store_module.os, "open", failing_open)
    with pytest.raises(PixelIntegrityError, match="cannot be read"):
        PixelStore._read_bytes(unreadable, root=tmp_path)
    monkeypatch.undo()

    malformed = tmp_path / "malformed.json"
    for value in ('{"a": 1, "a": 2}', "NaN"):
        malformed.write_text(value, encoding="utf-8")
        with pytest.raises(PixelIntegrityError, match="strict JSON"):
            PixelStore._read_metadata(malformed, root=tmp_path)
    malformed.write_text("[]", encoding="utf-8")
    with pytest.raises(PixelIntegrityError, match="JSON object"):
        PixelStore._read_metadata(malformed, root=tmp_path)


def test_descriptor_reads_reject_symlink_escapes_before_data_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "external.bin"
    external.write_bytes(b"external-secret")
    read_calls = 0
    original_read = pixel_store_module.os.read

    def track_read(descriptor: int, count: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return original_read(descriptor, count)

    monkeypatch.setattr(pixel_store_module.os, "read", track_read)

    final_link = root / "final-link"
    ancestor_link = root / "linked-parent"
    try:
        final_link.symlink_to(external)
        ancestor_link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this test host")
    with pytest.raises((PixelIntegrityError, PixelPathError)):
        PixelStore._read_bytes(final_link, root=root)
    with pytest.raises((PixelIntegrityError, PixelPathError)):
        PixelStore._read_bytes(ancestor_link / external.name, root=root)
    assert read_calls == 0
    assert external.read_bytes() == b"external-secret"


def test_descriptor_reads_reject_hardlinks_before_data_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    external = tmp_path / "external.bin"
    external.write_bytes(b"external-secret")
    read_calls = 0
    original_read = pixel_store_module.os.read

    def track_read(descriptor: int, count: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return original_read(descriptor, count)

    monkeypatch.setattr(pixel_store_module.os, "read", track_read)
    hardlink = root / "hardlink"
    try:
        os.link(external, hardlink)
    except OSError:
        pytest.skip("hard links are unavailable on this test host")
    with pytest.raises(PixelIntegrityError, match="exactly one hard link"):
        PixelStore._read_bytes(hardlink, root=root)
    assert read_calls == 0


def test_descriptor_reads_reject_identity_change_before_data_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    read_calls = 0
    original_read = pixel_store_module.os.read

    def track_read(descriptor: int, count: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return original_read(descriptor, count)

    monkeypatch.setattr(pixel_store_module.os, "read", track_read)
    stable = root / "stable"
    replacement = root / "replacement"
    stable.write_bytes(b"stable")
    replacement.write_bytes(b"replacement")
    original_lstat = Path.lstat

    def changed_identity(path: Path) -> os.stat_result:
        return original_lstat(replacement if path == stable else path)

    monkeypatch.setattr(Path, "lstat", changed_identity)
    with pytest.raises(PixelIntegrityError, match="identity changed"):
        PixelStore._read_bytes(stable, root=root)
    assert read_calls == 0


def test_verified_read_rejects_noncanonical_metadata_and_all_integrity_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PixelStore(tmp_path / "cas")
    spec = _spec()
    pixels = _pixels()
    digest = store.put(spec, pixels)
    metadata_path = store.metadata_path_for(digest)
    canonical = metadata_path.read_bytes()
    metadata_path.write_text(json.dumps(json.loads(canonical.decode("utf-8"))), encoding="utf-8")
    with pytest.raises(PixelIntegrityError, match="canonical byte"):
        store.read(digest)
    metadata_path.write_bytes(canonical)

    other_digest = store.put(spec, bytes(reversed(pixels)), source_sequence=1)
    other_metadata = store.metadata_path_for(other_digest).read_bytes()
    metadata_path.write_bytes(other_metadata)
    with pytest.raises(PixelIntegrityError, match="does not match requested"):
        store.read(digest)
    metadata_path.write_bytes(canonical)

    raw_path = store.path_for(digest)
    raw = raw_path.read_bytes()
    raw_path.write_bytes(bytes([raw[0] ^ 0xFF]) + raw[1:])
    with pytest.raises(PixelIntegrityError, match="pixel digest mismatch"):
        store.read(digest)
    raw_path.write_bytes(raw)

    artifact = store.artifact(digest)
    original_read_metadata = store._read_metadata
    original_read_bytes = store._read_bytes

    def fake_metadata(_path: Path, *, root: Path) -> PixelArtifact:
        assert root == store.root
        return artifact

    def fake_bytes(path: Path, *, root: Path) -> bytes:
        assert root == store.root
        if path == metadata_path:
            return canonical_json(artifact.to_dict()) + b"\n"
        return pixels

    monkeypatch.setattr(store, "_read_metadata", fake_metadata)
    monkeypatch.setattr(store, "_read_bytes", fake_bytes)
    object.__setattr__(artifact, "path", "wrong")
    with pytest.raises(PixelIntegrityError, match="path/length"):
        store.read(digest)
    object.__setattr__(artifact, "path", f"{digest[:2]}/{digest[2:]}")
    object.__setattr__(artifact, "byte_length", len(pixels) - 1)
    with pytest.raises(PixelIntegrityError, match="byte length mismatch"):
        store.read(digest)
    object.__setattr__(artifact, "byte_length", len(pixels))
    object.__setattr__(artifact, "encoded_size", len(pixels) - 1)
    with pytest.raises(PixelIntegrityError, match="encoding hash/size"):
        store.read(digest)
    monkeypatch.setattr(store, "_read_metadata", original_read_metadata)
    monkeypatch.setattr(store, "_read_bytes", original_read_bytes)


def test_put_rejects_orphan_objects_existing_conflicts_and_postwrite_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec()
    pixels = _pixels()
    store = PixelStore(tmp_path / "cas")
    digest = store.put(spec, pixels)
    artifact = store.artifact(digest)
    original_verified_read = store._verified_read
    monkeypatch.setattr(store, "_verified_read", lambda _digest, _spec: (b"wrong", artifact))
    with pytest.raises(PixelIntegrityError, match="bytes do not match"):
        store.put(spec, pixels)
    monkeypatch.setattr(store, "_verified_read", original_verified_read)

    raw_path = store.path_for(digest)
    raw_path.unlink()
    with pytest.raises(PixelIntegrityError, match="orphan CAS metadata"):
        store.put(spec, pixels)

    # A reader returning a different occurrence object is treated as a
    # post-publication integrity failure rather than silently accepted.
    store = PixelStore(tmp_path / "postwrite")
    original_read_metadata = store._read_metadata
    first = store.put_artifact(spec, pixels, source_sequence=1)

    def alter_occurrence(path: Path, *, root: Path) -> PixelArtifact:
        value = original_read_metadata(path, root=root)
        if path.parent.name.endswith(".occurrences"):
            object.__setattr__(value, "session_id", "tampered")
        return value

    monkeypatch.setattr(store, "_read_metadata", alter_occurrence)
    with pytest.raises(PixelIntegrityError, match="post-write verification"):
        store.put_artifact(spec, pixels, source_sequence=2)
    assert first.pixel_digest == pixel_digest(spec, pixels)


def test_occurrence_ledger_rejects_invalid_directory_canonical_bytes_and_identity(
    tmp_path: Path,
) -> None:
    spec = _spec()
    pixels = _pixels()
    store = PixelStore(tmp_path / "cas")
    artifact = store.put_artifact(spec, pixels)
    occurrence_directory = store.occurrence_directory_for(artifact.pixel_digest)
    occurrence_path = store.occurrence_path_for(artifact.pixel_digest, "unknown", "unknown", 0)
    occurrence_bytes = occurrence_path.read_bytes()

    occurrence_path.write_text(
        json.dumps(json.loads(occurrence_bytes.decode("utf-8"))), encoding="utf-8"
    )
    with pytest.raises(PixelIntegrityError, match="canonical bytes"):
        store.occurrence(
            artifact.pixel_digest,
            source_provenance_id="unknown",
            session_id="unknown",
            source_sequence=0,
        )
    occurrence_path.write_bytes(occurrence_bytes)

    value = json.loads(occurrence_bytes.decode("utf-8"))
    value["source_sequence"] = 1
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    value["artifact_sha256"] = hashlib.sha256(canonical_json(body)).hexdigest()
    occurrence_path.write_bytes(canonical_json(value) + b"\n")
    with pytest.raises(PixelIntegrityError, match="identity"):
        store.occurrence(
            artifact.pixel_digest,
            source_provenance_id="unknown",
            session_id="unknown",
            source_sequence=0,
        )
    occurrence_path.write_bytes(occurrence_bytes)

    for child in occurrence_directory.iterdir():
        child.unlink()
    occurrence_directory.rmdir()
    occurrence_directory.write_bytes(b"not-a-directory")
    with pytest.raises(PixelIntegrityError, match="ledger"):
        store.occurrence(
            artifact.pixel_digest,
            source_provenance_id="unknown",
            session_id="unknown",
            source_sequence=0,
        )


def _format_mapping(backend: str = "dshow") -> dict[str, object]:
    return {
        "width": 2,
        "height": 2,
        "fps": 30.0,
        "fourcc": "BGR8",
        "backend": backend,
        "channels": 3,
        "pixel_format": "BGR8",
        "dtype": "uint8",
        "stride": 6,
        "length": 12,
    }


def _provenance_kwargs() -> dict[str, object]:
    fingerprint = hash_physical_device_fingerprint(b"device-serial")
    return {
        "source_id": "capture-card-primary",
        "session_id": "session-001",
        "requested": _format_mapping(),
        "negotiated": _format_mapping(),
        "backend": "dshow",
        "timestamp_origin": "host_monotonic_post_retrieve",
        "upstream_queue": "unknown",
        "physical_device_fingerprint_sha256": fingerprint,
        "input_owner": "legacy",
        "real_input_enabled": False,
        "real_input_call_count": 0,
        "backend_version": "backend-1",
        "tool_artifact_sha256": "1" * 64,
        "dependency_lock_sha256": "2" * 64,
        "source_artifact_sha256": "3" * 64,
        "source_commit": "a" * 40,
        "config_sha256": "4" * 64,
        "calibration_sha256": "5" * 64,
    }


def test_capture_provenance_roundtrip_aliases_and_deidentified_layout() -> None:
    kwargs = _provenance_kwargs()
    fingerprint = str(kwargs["physical_device_fingerprint_sha256"])
    requested = kwargs["requested"]
    negotiated = kwargs["negotiated"]
    provenance = CaptureSourceProvenance(
        **kwargs,  # type: ignore[arg-type]
        requested_format=requested,  # type: ignore[arg-type]
        negotiated_format=negotiated,  # type: ignore[arg-type]
        backend_name="dshow",
        timestamp_source="host_monotonic_post_retrieve",
        upstream_queue_state="unknown",
        device_fingerprint_sha256=fingerprint,
        physical_device_fingerprint_hash=fingerprint,
        physical_device_fingerprint=b"device-serial",
        real_input=0,
    )
    payload = provenance.to_dict()
    assert payload["requested"] == payload["negotiated"]
    assert payload["physical_device_fingerprint_sha256"] == fingerprint
    assert payload["upstream_queue_depth"] == "unknown"
    assert provenance.real_input == 0
    assert provenance.requested_format == provenance.requested
    assert provenance.negotiated_format == provenance.negotiated
    assert provenance.backend_name == provenance.backend == "dshow"
    assert provenance.timestamp_source == provenance.timestamp_origin
    assert provenance.upstream_queue_state == provenance.upstream_queue_depth == "unknown"
    assert provenance.device_fingerprint_sha256 == provenance.physical_device_fingerprint_hash
    assert provenance.fingerprint_sha256 == fingerprint
    assert provenance.provenance_id == provenance.provenance_id
    assert CaptureProvenance.from_json(provenance.to_json()) == provenance


def test_capture_provenance_defaults_and_pixel_spec_formats() -> None:
    defaults = CaptureSourceProvenance()
    assert defaults.requested["backend"] == "unknown"
    assert defaults.negotiated["fps"] == 30.0
    assert defaults.physical_device_fingerprint_sha256
    provenance = CaptureSourceProvenance(
        requested=PixelSpec(width=2, height=2),
        negotiated=PixelSpec(width=2, height=2),
        backend="dshow",
    )
    assert provenance.requested["backend"] == "dshow"
    assert provenance.negotiated["backend"] == "dshow"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("requested", {"width": 2}, "missing key"),
        ("requested", {**_format_mapping(), "extra": 1}, "unknown keys"),
        ("requested", {**_format_mapping(), "fps": 0}, "positive number"),
        (
            "requested",
            {
                key: value
                for key, value in _format_mapping().items()
                if key not in {"pixel_format", "dtype", "length"}
            },
            "incomplete",
        ),
        (
            "requested",
            {**_format_mapping(), "serial": "raw-id"},
            "physical-device identifier",
        ),
        ("requested", {**_format_mapping(), "channels": 4}, "inconsistent"),
        ("requested", object(), "PixelSpec or a mapping"),
    ],
)
def test_capture_provenance_rejects_malformed_format_declarations(
    field: str, value: object, message: str
) -> None:
    kwargs = _provenance_kwargs()
    kwargs[field] = value
    with pytest.raises((TypeError, ValueError), match=message):
        CaptureSourceProvenance(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"requested": _format_mapping("other")},
        {"negotiated": _format_mapping("other")},
        {"backend": "dshow", "backend_name": "other"},
        {
            "timestamp_origin": "host_monotonic_post_retrieve",
            "timestamp_source": "other",
        },
        {"upstream_queue": "unknown", "upstream_queue_state": "other"},
        {"physical_device_fingerprint_sha256": "1" * 64, "device_fingerprint_sha256": "2" * 64},
        {"physical_device_fingerprint": b"raw", "physical_device_fingerprint_sha256": "0" * 64},
        {"real_input": True},
        {"real_input": 2},
        {"real_input_enabled": True},
        {"real_input_call_count": 1},
        {"input_owner": "core_v2"},
        {"source_id": "source/path"},
        {"session_id": "session with spaces"},
        {"backend": ""},
        {"timestamp_origin": "wall_clock"},
        {"upstream_queue": "ready"},
        {"schema_version": "2.0.0"},
        {"source_commit": "a" * 39},
        {"source_commit": "g" * 40},
        {"source_commit": "A" * 40},
        {"tool_artifact_sha256": "bad"},
    ],
)
def test_capture_provenance_rejects_alias_policy_and_provenance_boundaries(
    kwargs: dict[str, object],
) -> None:
    values = _provenance_kwargs()
    values.update(kwargs)
    with pytest.raises((TypeError, ValueError)):
        CaptureSourceProvenance(**values)  # type: ignore[arg-type]


def test_capture_provenance_alias_conflicts_and_strict_deserialization() -> None:
    kwargs = _provenance_kwargs()
    for alias in ("requested_format", "negotiated_format"):
        values = dict(kwargs)
        values[alias] = _format_mapping("other")
        with pytest.raises(ValueError, match="must match"):
            CaptureSourceProvenance(**values)  # type: ignore[arg-type]

    values = dict(kwargs)
    values["real_input"] = False
    assert CaptureSourceProvenance(**values).real_input == 0  # type: ignore[arg-type]

    provenance = CaptureSourceProvenance(**kwargs)  # type: ignore[arg-type]
    payload = provenance.to_dict()
    assert CaptureSourceProvenance.from_json(provenance.to_json()) == provenance
    with pytest.raises(TypeError, match="must be str"):
        CaptureSourceProvenance.from_json(b"{}")  # type: ignore[arg-type]
    for value in ("[]", '{"source_id": "x", "source_id": "y"}', "NaN"):
        with pytest.raises(ValueError):
            CaptureSourceProvenance.from_json(value)
    with pytest.raises(ValueError, match="mapping"):
        CaptureSourceProvenance.from_dict([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown keys"):
        CaptureSourceProvenance.from_dict({**payload, "extra": True})
    missing = dict(payload)
    del missing["source_id"]
    with pytest.raises(ValueError, match="missing key: source_id"):
        CaptureSourceProvenance.from_dict(missing)
    mismatched_queue = dict(payload)
    mismatched_queue["upstream_queue_depth"] = "ready"
    with pytest.raises(ValueError, match="queue"):
        CaptureSourceProvenance.from_dict(mismatched_queue)


def test_deidentified_mapping_rejects_sensitive_key() -> None:
    value = {**_format_mapping(), "serial": "synthetic-id"}
    with pytest.raises(ValueError, match="physical-device identifier"):
        pixel_store_module._deidentified_mapping(value, "requested")


def test_remaining_strict_helpers_and_path_resolution_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="hexadecimal"):
        pixel_store_module._ensure_sha256("g" * 64, "digest")
    frozen = pixel_store_module._freeze_json({"items": [1, (2, 3)]})
    assert pixel_store_module._thaw_json(frozen) == {"items": [1, [2, 3]]}

    root = tmp_path / "root"
    candidate = tmp_path / "outside" / "object"
    with pytest.raises(PixelPathError, match="escapes"):
        pixel_store_module._ensure_inside_root(root, candidate)

    original_resolve = Path.resolve

    def fail_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == candidate.parent:
            raise OSError("synthetic resolve failure")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    with pytest.raises(PixelPathError, match="cannot be resolved"):
        pixel_store_module._ensure_inside_root(root, candidate)


def test_capture_provenance_alias_queue_conflict_is_rejected() -> None:
    values = _provenance_kwargs()
    values["upstream_queue"] = "ready"
    values["upstream_queue_state"] = "other"
    with pytest.raises(ValueError, match="must match"):
        CaptureSourceProvenance(**values)  # type: ignore[arg-type]
