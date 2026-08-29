"""Content-addressed storage for canonical BGR8 capture pixels.

This module intentionally has no image-library dependency.  The capture
boundary deals in a byte-exact, packed ``uint8`` BGR frame and records the
format alongside the bytes.  A pixel digest is consequently an identity for
both the bytes *and* the interpretation of those bytes::

    sha256(
        b"MAPLE_PIXEL_V1\\0"
        + canonical_json(pixel_spec)
        + b"\\0"
        + exact_bgr8_bytes
    )

The small CAS implementation below is deliberately conservative.  It never
follows a storage-root or digest-directory symlink, writes through a temporary
file followed by ``os.replace``, and re-reads and verifies every object before
returning it to a caller.  A malformed object is an error rather than a cache
miss: callers must not accidentally continue with unverified pixels.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

PIXEL_DIGEST_DOMAIN = b"MAPLE_PIXEL_V1\0"
PIXEL_OCCURRENCE_DOMAIN = b"MAPLE_PIXEL_OCCURRENCE_V1\0"
PIXEL_SCHEMA_VERSION = "1.0.0"
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_CHANNELS = 3
DEFAULT_PIXEL_FORMAT = "BGR8"
DEFAULT_DTYPE = "uint8"
DEFAULT_STRIDE = DEFAULT_WIDTH * DEFAULT_CHANNELS
DEFAULT_LENGTH = DEFAULT_STRIDE * DEFAULT_HEIGHT
PIXEL_PRIVACY_CLASSES = frozenset({"private", "restricted", "deidentified_public", "hash_only"})
PIXEL_RETENTION_CLASSES = frozenset({"ephemeral", "candidate", "persistent"})
_PIXEL_STORE_LOCK_FILENAME = ".pixel-store.lock"
_PROCESS_PIXEL_WRITE_LOCK = threading.RLock()


class PixelStoreError(ValueError):
    """Base error for malformed pixel specifications or CAS objects."""


class PixelIntegrityError(PixelStoreError):
    """Raised when a CAS path, metadata record, or pixel bytes are tampered."""


class PixelPathError(PixelStoreError):
    """Raised when a CAS path would leave the configured storage root."""


def _ensure_int(value: object, field_name: str, *, positive: bool = False) -> int:
    if type(value) is not int or (value <= 0 if positive else value < 0):
        comparator = "> 0" if positive else ">= 0"
        raise ValueError(f"{field_name} must be an integer {comparator}.")
    return value


def _ensure_non_empty_str(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value


def _ensure_portable_token(value: object, field_name: str) -> str:
    text = _ensure_non_empty_str(value, field_name)
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
    if any(character not in allowed for character in text):
        raise ValueError(f"{field_name} must be a de-identified portable token.")
    return text


def _ensure_sha256(value: object, field_name: str) -> str:
    text = _ensure_non_empty_str(value, field_name)
    if len(text) != 64:
        raise ValueError(f"{field_name} must be a 64-character SHA-256 hex string.")
    try:
        bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be hexadecimal SHA-256 digest.") from exc
    return text.lower()


def _ensure_json_value(
    value: object,
    field_name: str = "value",
    active: set[int] | None = None,
) -> None:
    """Validate the strict JSON subset used by canonical contract records."""

    if type(value) is str or type(value) is int or type(value) is bool or value is None:
        return
    if type(value) is float:
        # ``json.dumps(allow_nan=False)`` will reject these too, but checking
        # here gives all public boundaries the same deterministic error type.
        import math

        if math.isfinite(value):
            return
        raise ValueError(f"{field_name} must be JSON-serializable.")

    if active is None:
        active = set()
    value_id = id(value)
    if value_id in active:
        raise ValueError(f"{field_name} must not contain cyclic containers.")

    if type(value) is list or type(value) is tuple:
        active.add(value_id)
        try:
            for index, item in enumerate(value):
                _ensure_json_value(item, f"{field_name}[{index}]", active)
        finally:
            active.remove(value_id)
        return

    if isinstance(value, Mapping):
        active.add(value_id)
        try:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError(f"{field_name} keys must be strings.")
                _ensure_json_value(item, f"{field_name}.{key}", active)
        finally:
            active.remove(value_id)
        return

    raise ValueError(f"{field_name} must be JSON-serializable.")


def _thaw_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {cast(str, key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    if isinstance(value, list):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_json(value: object) -> Any:
    _ensure_json_value(value)
    if isinstance(value, Mapping):
        return MappingProxyType({cast(str, key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def canonical_json(payload: PixelSpec | Mapping[str, Any] | Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for a strict JSON payload.

    ``PixelSpec`` is accepted directly because it is the only object used in
    the digest preimage.  Mapping values are intentionally not coerced from
    arbitrary objects; this prevents accidental identity changes caused by a
    serializer's ``default=`` hook.
    """

    if isinstance(payload, PixelSpec):
        value: Any = payload.to_dict()
    else:
        value = payload
    _ensure_json_value(value)
    try:
        encoded = json.dumps(
            _thaw_json(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be strict JSON.") from exc
    return encoded.encode("utf-8")


# The longer spelling mirrors ``domain._contract_utils`` and is convenient to
# callers that already use that helper elsewhere in the runtime.
canonical_json_bytes = canonical_json


@dataclass(frozen=True, slots=True, init=False)
class PixelSpec:
    """The only pixel layout accepted by the G1 raw-pixel boundary.

    ``stride`` and ``length`` are derived fields.  They remain constructor
    arguments so a decoded artifact can prove that its declared values agree
    with the packed layout; a caller passing a non-derived value is rejected.
    """

    width: int
    height: int
    channels: int
    pixel_format: str
    dtype: str
    stride: int
    length: int

    def __init__(
        self,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        channels: int = DEFAULT_CHANNELS,
        pixel_format: str = DEFAULT_PIXEL_FORMAT,
        dtype: str = DEFAULT_DTYPE,
        stride: int | None = None,
        length: int | None = None,
    ) -> None:
        width_value = _ensure_int(width, "width", positive=True)
        height_value = _ensure_int(height, "height", positive=True)
        channels_value = _ensure_int(channels, "channels", positive=True)
        if channels_value != DEFAULT_CHANNELS:
            raise ValueError("BGR8 requires exactly three channels.")
        if (
            not isinstance(pixel_format, str)
            or pixel_format.casefold() != DEFAULT_PIXEL_FORMAT.casefold()
        ):
            raise ValueError("pixel_format must be exactly 'BGR8'.")
        if dtype != DEFAULT_DTYPE:
            raise ValueError("dtype must be exactly 'uint8'.")
        stride_value = width_value * channels_value
        length_value = stride_value * height_value
        if stride is not None and _ensure_int(stride, "stride", positive=True) != stride_value:
            raise ValueError("stride must equal width * channels for packed BGR8.")
        if length is not None and _ensure_int(length, "length", positive=True) != length_value:
            raise ValueError("length must equal stride * height for packed BGR8.")

        object.__setattr__(self, "width", width_value)
        object.__setattr__(self, "height", height_value)
        object.__setattr__(self, "channels", channels_value)
        object.__setattr__(self, "pixel_format", DEFAULT_PIXEL_FORMAT)
        object.__setattr__(self, "dtype", DEFAULT_DTYPE)
        object.__setattr__(self, "stride", stride_value)
        object.__setattr__(self, "length", length_value)

    @property
    def stride_bytes(self) -> int:
        """Alias used by byte-oriented callers."""

        return self.stride

    @property
    def byte_length(self) -> int:
        """Alias for the exact packed byte count."""

        return self.length

    @property
    def shape(self) -> tuple[int, int, int]:
        """C-order array shape ``(height, width, channels)``."""

        return (self.height, self.width, self.channels)

    @property
    def is_packed(self) -> bool:
        return self.stride == self.width * self.channels and self.length == (
            self.stride * self.height
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "channels": self.channels,
            "pixel_format": self.pixel_format,
            "dtype": self.dtype,
            "stride": self.stride,
            "length": self.length,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PixelSpec:
        if not isinstance(value, Mapping):
            raise ValueError("PixelSpec payload must be a mapping.")
        allowed = {"width", "height", "channels", "pixel_format", "dtype", "stride", "length"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"PixelSpec payload has unknown keys: {sorted(unknown)!r}.")
        missing = allowed - set(value)
        if missing:
            raise ValueError(f"PixelSpec payload missing key: {sorted(missing)[0]}.")
        return cls(
            width=value["width"],
            height=value["height"],
            channels=value["channels"],
            pixel_format=value["pixel_format"],
            dtype=value["dtype"],
            stride=value["stride"],
            length=value["length"],
        )


DEFAULT_PIXEL_SPEC = PixelSpec()


def _pixel_bytes(spec: PixelSpec, pixels: object) -> bytes:
    if not isinstance(spec, PixelSpec):
        raise TypeError("spec must be PixelSpec.")
    # Objects such as a NumPy uint8 ndarray expose the buffer protocol but are
    # deliberately not imported by this package.  ``memoryview`` is the
    # standard-library bridge for that protocol.
    try:
        view = memoryview(cast(Any, pixels))
    except TypeError as exc:
        raise TypeError("pixels must expose a contiguous uint8 buffer.") from exc

    try:
        if view.format != "B" or view.itemsize != 1:
            raise ValueError("pixels must have uint8 ('B') format.")
        if not view.c_contiguous:
            raise ValueError("pixels must be C-contiguous.")
        if view.ndim == 3:
            if view.shape != spec.shape:
                raise ValueError(f"pixels shape must be {spec.shape!r}.")
            if view.strides != (spec.stride, spec.channels, 1):
                raise ValueError("pixels must use packed row-major BGR8 strides.")
        elif view.ndim != 1:
            raise ValueError("pixels must be a flat buffer or (height,width,3) array.")
        data = view.cast("B").tobytes() if view.ndim != 1 else view.tobytes()
    finally:
        view.release()

    if len(data) != spec.length:
        raise ValueError(f"pixels length must be exactly {spec.length} bytes.")
    return data


def validate_pixels(spec: PixelSpec, pixels: object) -> bytes:
    """Validate and return an immutable byte copy of packed BGR8 pixels."""

    return _pixel_bytes(spec, pixels)


def pixel_digest(spec: PixelSpec, pixels: object) -> str:
    """Compute the frozen, format-bound digest for one BGR8 frame."""

    data = _pixel_bytes(spec, pixels)
    digest = sha256()
    digest.update(PIXEL_DIGEST_DOMAIN)
    digest.update(canonical_json(spec))
    digest.update(b"\0")
    digest.update(data)
    return digest.hexdigest()


# Naming aliases make the frozen identity explicit at integration call sites.
compute_pixel_digest = pixel_digest
canonical_pixel_digest = pixel_digest
pixel_sha256 = pixel_digest
digest_pixels = pixel_digest


def encoded_sha256(encoded: object) -> str:
    """Hash an encoded source payload separately from the pixel digest."""

    try:
        view = memoryview(encoded)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError("encoded payload must expose a byte buffer.") from exc
    try:
        if view.itemsize != 1:
            raise ValueError("encoded payload must contain bytes.")
        if not view.c_contiguous:
            raise ValueError("encoded payload must be C-contiguous.")
        return sha256(view.cast("B").tobytes()).hexdigest()
    finally:
        view.release()


encoded_hash = encoded_sha256
_encoded_sha256 = encoded_sha256


def _buffer_length(value: object) -> int:
    try:
        view = memoryview(cast(Any, value))
    except TypeError as exc:
        raise TypeError("encoded payload must expose a byte buffer.") from exc
    try:
        if view.itemsize != 1 or not view.c_contiguous:
            raise ValueError("encoded payload must be a contiguous byte buffer.")
        return len(view.cast("B"))
    finally:
        view.release()


def verify_pixel_digest(expected: str, spec: PixelSpec, pixels: object) -> bool:
    """Return whether bytes have exactly the supplied format-bound digest."""

    expected_value = _ensure_sha256(expected, "expected")
    return pixel_digest(spec, pixels) == expected_value


@dataclass(frozen=True, slots=True)
class PixelArtifact:
    """Strict metadata describing one raw pixel CAS object."""

    pixel_digest: str
    spec: PixelSpec
    byte_length: int
    path: str = ""
    encoded_sha256: str | None = None
    schema_version: str = PIXEL_SCHEMA_VERSION
    storage_encoding: str = "raw"
    encoded_size: int | None = None
    source_encoded_sha256: str | None = None
    source_encoded_size: int | None = None
    privacy_class: str = "private"
    retention_class: str = "persistent"
    source_provenance_id: str = "unknown"
    session_id: str = "unknown"
    source_sequence: int = 0
    parent_pixel_digest: str | None = None
    transform_version: str | None = None
    calibration_sha256: str | None = None

    def __post_init__(self) -> None:
        digest = _ensure_sha256(self.pixel_digest, "pixel_digest")
        if not isinstance(self.spec, PixelSpec):
            raise TypeError("spec must be PixelSpec.")
        if _ensure_int(self.byte_length, "byte_length", positive=True) != self.spec.length:
            raise ValueError("byte_length must match spec.length.")
        if type(self.path) is not str:
            raise TypeError("path must be str.")
        if self.path:
            _validate_relative_cas_path(self.path, digest)
        if self.encoded_sha256 is not None:
            encoded_digest = _ensure_sha256(self.encoded_sha256, "encoded_sha256")
            object.__setattr__(self, "encoded_sha256", encoded_digest)
        else:
            raise ValueError("encoded_sha256 is required for the raw backing object.")
        if self.encoded_size is None:
            object.__setattr__(self, "encoded_size", self.byte_length)
        else:
            _ensure_int(self.encoded_size, "encoded_size", positive=True)
        if self.source_encoded_sha256 is not None:
            object.__setattr__(
                self,
                "source_encoded_sha256",
                _ensure_sha256(self.source_encoded_sha256, "source_encoded_sha256"),
            )
        if (self.source_encoded_sha256 is None) != (self.source_encoded_size is None):
            raise ValueError("source encoded hash and size must both be present or null.")
        if self.source_encoded_size is not None:
            _ensure_int(self.source_encoded_size, "source_encoded_size", positive=True)
        if self.schema_version != PIXEL_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PIXEL_SCHEMA_VERSION}.")
        if self.storage_encoding != "raw":
            raise ValueError("storage_encoding must be raw lossless bytes.")
        if self.privacy_class not in PIXEL_PRIVACY_CLASSES:
            raise ValueError("privacy_class is unsupported.")
        if self.retention_class not in PIXEL_RETENTION_CLASSES:
            raise ValueError("retention_class is unsupported.")
        _ensure_portable_token(self.source_provenance_id, "source_provenance_id")
        _ensure_portable_token(self.session_id, "session_id")
        _ensure_int(self.source_sequence, "source_sequence")
        if self.parent_pixel_digest is not None:
            object.__setattr__(
                self,
                "parent_pixel_digest",
                _ensure_sha256(self.parent_pixel_digest, "parent_pixel_digest"),
            )
        if self.transform_version is not None:
            _ensure_non_empty_str(self.transform_version, "transform_version")
        if self.calibration_sha256 is not None:
            object.__setattr__(
                self,
                "calibration_sha256",
                _ensure_sha256(self.calibration_sha256, "calibration_sha256"),
            )
        derivation_fields = (
            self.parent_pixel_digest,
            self.transform_version,
            self.calibration_sha256,
        )
        if any(value is None for value in derivation_fields) and any(
            value is not None for value in derivation_fields
        ):
            raise ValueError(
                "parent_pixel_digest, transform_version and calibration_sha256 "
                "must be all null for raw pixels or all present for a derivation."
            )
        if self.parent_pixel_digest == digest:
            raise ValueError("parent_pixel_digest must not equal pixel_digest.")
        object.__setattr__(self, "pixel_digest", digest)

    @property
    def digest(self) -> str:
        return self.pixel_digest

    @property
    def spec_dict(self) -> dict[str, Any]:
        return self.spec.to_dict()

    @property
    def pixel_spec(self) -> PixelSpec:
        return self.spec

    @property
    def length(self) -> int:
        return self.byte_length

    @property
    def encoded_hash(self) -> str | None:
        return self.encoded_sha256

    @property
    def ref(self) -> str:
        return f"cas://sha256/{self.pixel_digest}"

    @property
    def image_ref(self) -> str:
        return self.ref

    def _body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pixel_digest": self.pixel_digest,
            "ref": self.ref,
            "image_ref": self.image_ref,
            "encoded_sha256": self.encoded_sha256,
            "encoded_size": self.encoded_size,
            "source_encoded_sha256": self.source_encoded_sha256,
            "source_encoded_size": self.source_encoded_size,
            "storage_encoding": self.storage_encoding,
            "spec": self.spec.to_dict(),
            "byte_length": self.byte_length,
            "path": self.path,
            "privacy_class": self.privacy_class,
            "retention_class": self.retention_class,
            "source_provenance_id": self.source_provenance_id,
            "session_id": self.session_id,
            "source_sequence": self.source_sequence,
            "parent_pixel_digest": self.parent_pixel_digest,
            "transform_version": self.transform_version,
            "calibration_sha256": self.calibration_sha256,
        }

    @property
    def artifact_sha256(self) -> str:
        """Canonical occurrence-envelope digest (excluding itself)."""

        return sha256(canonical_json(self._body_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self._body_dict(), "artifact_sha256": self.artifact_sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PixelArtifact:
        if not isinstance(value, Mapping):
            raise ValueError("PixelArtifact payload must be a mapping.")
        allowed = {
            "schema_version",
            "pixel_digest",
            "ref",
            "image_ref",
            "encoded_sha256",
            "encoded_size",
            "source_encoded_sha256",
            "source_encoded_size",
            "storage_encoding",
            "spec",
            "pixel_spec",
            "byte_length",
            "path",
            "privacy_class",
            "retention_class",
            "source_provenance_id",
            "session_id",
            "source_sequence",
            "parent_pixel_digest",
            "transform_version",
            "calibration_sha256",
            "artifact_sha256",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"PixelArtifact payload has unknown keys: {sorted(unknown)!r}.")
        if "spec" in value and "pixel_spec" in value and value["spec"] != value["pixel_spec"]:
            raise ValueError("spec and pixel_spec must match.")
        if "ref" in value and value["ref"] != f"cas://sha256/{value.get('pixel_digest', '')}":
            raise ValueError("ref must be derived from pixel_digest.")
        if "image_ref" in value and value["image_ref"] != value.get("ref"):
            raise ValueError("image_ref and ref must match.")
        spec_value = value.get("spec", value.get("pixel_spec"))
        required = (
            "schema_version",
            "pixel_digest",
            "byte_length",
            "path",
            "artifact_sha256",
        )
        for key in required:
            if key not in value:
                raise ValueError(f"PixelArtifact payload missing key: {key}.")
        if spec_value is None:
            raise ValueError("PixelArtifact payload missing key: spec.")
        artifact = cls(
            schema_version=value["schema_version"],
            pixel_digest=value["pixel_digest"],
            encoded_sha256=value.get("encoded_sha256"),
            spec=PixelSpec.from_dict(cast(Mapping[str, Any], spec_value)),
            byte_length=value["byte_length"],
            path=value["path"],
            encoded_size=value.get("encoded_size"),
            source_encoded_sha256=value.get("source_encoded_sha256"),
            source_encoded_size=value.get("source_encoded_size"),
            storage_encoding=value.get("storage_encoding", "raw"),
            privacy_class=value.get("privacy_class", "private"),
            retention_class=value.get("retention_class", "persistent"),
            source_provenance_id=value.get("source_provenance_id", "unknown"),
            session_id=value.get("session_id", "unknown"),
            source_sequence=value.get("source_sequence", 0),
            parent_pixel_digest=value.get("parent_pixel_digest"),
            transform_version=value.get("transform_version"),
            calibration_sha256=value.get("calibration_sha256"),
        )
        if _ensure_sha256(value["artifact_sha256"], "artifact_sha256") != artifact.artifact_sha256:
            raise ValueError("PixelArtifact artifact_sha256 mismatch.")
        return artifact


FramePixelArtifact = PixelArtifact


def _validate_relative_cas_path(value: str, digest: str) -> None:
    normalized = value.replace("\\", "/")
    expected = f"{digest[:2]}/{digest[2:]}"
    if normalized != expected:
        raise PixelPathError("artifact path must be the canonical digest-derived relative path.")
    if normalized.startswith("/") or ":" in normalized or "//" in normalized:
        raise PixelPathError("artifact path must be relative and normalized.")
    if any(part in ("", ".", "..") for part in normalized.split("/")):
        raise PixelPathError("artifact path contains traversal components.")


def cas_path(root: str | Path, digest: str) -> Path:
    """Derive a raw CAS path from a validated digest, without reading it."""

    digest_value = _ensure_sha256(digest, "digest")
    root_path = Path(root).expanduser()
    return root_path / digest_value[:2] / digest_value[2:]


derive_cas_path = cas_path


def _reject_duplicate_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise PixelIntegrityError(f"duplicate JSON key: {key}")
        result[key] = item
    return result


def _reject_json_constant(value: str) -> None:
    raise PixelIntegrityError(f"non-standard JSON constant: {value}")


def _is_symlink_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PixelPathError("CAS path could not be inspected safely.") from exc
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class PixelStore:
    """Fail-closed, atomic content-addressed storage for raw BGR8 pixels."""

    def __init__(self, root: str | Path) -> None:
        if not isinstance(root, str | Path):
            raise TypeError("root must be str or Path.")
        # Freeze the lexical absolute root at construction so later CWD
        # changes cannot redirect an existing store instance.
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        self._write_lock = threading.RLock()

    def _check_root(self, *, create: bool) -> Path:
        root = self.root
        for component in (*reversed(root.parents), root):
            if (component.exists() or component.is_symlink()) and _is_symlink_or_reparse(component):
                raise PixelPathError("CAS storage path must not contain symlinks/reparse points.")
        if root.exists() or root.is_symlink():
            if _is_symlink_or_reparse(root):
                raise PixelPathError("CAS storage root must not be a symlink.")
            if not root.is_dir():
                raise PixelPathError("CAS storage root must be a directory.")
        elif create:
            root.mkdir(parents=True, exist_ok=True)
            if _is_symlink_or_reparse(root) or not root.is_dir():
                raise PixelPathError("CAS storage root is not a real directory.")
        else:
            raise PixelPathError("CAS storage root does not exist.")
        return root

    @staticmethod
    def _lock_file_identity(path: Path, descriptor: int) -> tuple[int, int]:
        """Validate and return the identity shared by a lock path and fd.

        ``flock``/``msvcrt.locking`` protect an opened file, not a pathname.
        Rechecking the path after opening and after acquiring the lock makes a
        replacement (including a symlink/reparse-point replacement) fail
        closed instead of silently protecting a different inode.
        """

        try:
            path_stat = path.lstat()
            descriptor_stat = os.fstat(descriptor)
        except OSError as exc:
            raise PixelPathError("CAS lock file cannot be stat'ed safely.") from exc
        if _is_symlink_or_reparse(path):
            raise PixelPathError("CAS lock file must not be a symlink or reparse point.")
        if not stat.S_ISREG(path_stat.st_mode) or not stat.S_ISREG(descriptor_stat.st_mode):
            raise PixelPathError("CAS lock file must be a regular file.")
        if path_stat.st_nlink != 1 or descriptor_stat.st_nlink != 1:
            raise PixelPathError("CAS lock file must have exactly one hard link.")
        path_identity = (path_stat.st_dev, path_stat.st_ino)
        descriptor_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
        if path_identity != descriptor_identity:
            raise PixelPathError("CAS lock file identity changed while opening it.")
        return descriptor_identity

    @staticmethod
    def _acquire_lock_descriptor(descriptor: int) -> None:
        """Acquire one blocking exclusive lock on a platform-native backend."""

        if os.name == "nt":
            import msvcrt

            # ``msvcrt.locking`` locks bytes starting at the current file
            # position.  Keep one byte in the lock file so LK_LOCK has a
            # stable range even on a freshly-created file.
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            return

        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)  # type: ignore[attr-defined]

    @staticmethod
    def _release_lock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)  # type: ignore[attr-defined]

    @contextmanager
    def _root_file_lock(self) -> Iterator[None]:
        """Serialize all CAS mutations sharing this storage root.

        The in-process RLocks remain the first layer of synchronization.  This
        context adds an OS lock on a stable file below the CAS root so two
        independent Python interpreters observe the same parent/graph state.
        """

        root = self._check_root(create=True)
        lock_path = root / _PIXEL_STORE_LOCK_FILENAME
        try:
            existing = lock_path.lstat()
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise PixelPathError("CAS lock file cannot be inspected safely.") from exc
        if existing is not None and (
            _is_symlink_or_reparse(lock_path) or not stat.S_ISREG(existing.st_mode)
        ):
            raise PixelPathError("CAS lock file must be a regular non-link file.")

        flags = os.O_RDWR | os.O_CREAT
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        locked = False
        try:
            try:
                descriptor = os.open(lock_path, flags | nofollow, 0o600)
            except OSError as exc:
                if _is_symlink_or_reparse(lock_path):
                    raise PixelPathError(
                        "CAS lock file must not be a symlink or reparse point."
                    ) from exc
                raise PixelPathError("CAS lock file cannot be opened safely.") from exc
            self._lock_file_identity(lock_path, descriptor)
            self._acquire_lock_descriptor(descriptor)
            locked = True
            self._lock_file_identity(lock_path, descriptor)
            yield
        finally:
            if descriptor is not None:
                try:
                    if locked:
                        self._release_lock_descriptor(descriptor)
                finally:
                    os.close(descriptor)

    def path_for(self, digest: str) -> Path:
        digest_value = _ensure_sha256(digest, "digest")
        # Deriving a path is also the first storage operation for a fresh
        # store.  Creating only the explicitly configured root here keeps the
        # returned path usable while still rejecting a pre-existing symlink.
        root = self._check_root(create=True)
        prefix = root / digest_value[:2]
        path = prefix / digest_value[2:]
        _ensure_inside_root(root, path)
        return path

    def metadata_path_for(self, digest: str) -> Path:
        raw = self.path_for(digest)
        return raw.with_name(raw.name + ".json")

    def occurrence_directory_for(self, digest: str) -> Path:
        """Return the digest-local directory containing immutable occurrences."""

        raw = self.path_for(digest)
        directory = raw.with_name(raw.name + ".occurrences")
        _ensure_inside_root(self._check_root(create=True), directory)
        return directory

    @staticmethod
    def _occurrence_key(
        digest: str,
        source_provenance_id: str,
        session_id: str,
        source_sequence: int,
    ) -> str:
        identity = {
            "pixel_digest": _ensure_sha256(digest, "digest"),
            "source_provenance_id": _ensure_non_empty_str(
                source_provenance_id, "source_provenance_id"
            ),
            "session_id": _ensure_non_empty_str(session_id, "session_id"),
            "source_sequence": _ensure_int(source_sequence, "source_sequence"),
        }
        digest_state = sha256()
        digest_state.update(PIXEL_OCCURRENCE_DOMAIN)
        digest_state.update(canonical_json(identity))
        return digest_state.hexdigest()

    def occurrence_path_for(
        self,
        digest: str,
        source_provenance_id: str,
        session_id: str,
        source_sequence: int,
    ) -> Path:
        key = self._occurrence_key(
            digest,
            source_provenance_id,
            session_id,
            source_sequence,
        )
        return self.occurrence_directory_for(digest) / f"{key}.json"

    def _ensure_occurrence_directory(self, digest: str) -> Path:
        directory = self.occurrence_directory_for(digest)
        if directory.exists() or directory.is_symlink():
            if _is_symlink_or_reparse(directory) or not directory.is_dir():
                raise PixelPathError("CAS occurrence directory must be a real directory.")
        else:
            directory.mkdir()
            if _is_symlink_or_reparse(directory) or not directory.is_dir():
                raise PixelPathError("CAS occurrence directory is not a real directory.")
        return directory

    def _ensure_digest_directory(self, digest: str) -> Path:
        root = self._check_root(create=True)
        prefix = root / digest[:2]
        if prefix.exists() or prefix.is_symlink():
            if _is_symlink_or_reparse(prefix) or not prefix.is_dir():
                raise PixelPathError("CAS digest directory must not be a symlink or file.")
        else:
            prefix.mkdir()
            if _is_symlink_or_reparse(prefix) or not prefix.is_dir():
                raise PixelPathError("CAS digest directory is not a real directory.")
        path = prefix / digest[2:]
        _ensure_inside_root(root, path)
        return path

    def _derivation_edges(self) -> dict[str, set[str]]:
        """Rebuild occurrence derivation edges without following links."""

        root = self._check_root(create=True)
        edges: dict[str, set[str]] = {}
        for prefix in root.iterdir():
            if _is_symlink_or_reparse(prefix):
                raise PixelPathError("CAS derivation scan encountered a symlink/reparse point.")
            if not prefix.is_dir():
                continue
            for entry in prefix.iterdir():
                if _is_symlink_or_reparse(entry):
                    raise PixelPathError("CAS derivation scan encountered a symlink/reparse point.")
                if not entry.is_dir() or not entry.name.endswith(".occurrences"):
                    continue
                for occurrence_path in entry.iterdir():
                    if _is_symlink_or_reparse(occurrence_path):
                        raise PixelPathError(
                            "CAS derivation scan encountered a symlink/reparse point."
                        )
                    if not occurrence_path.is_file() or occurrence_path.suffix != ".json":
                        raise PixelIntegrityError(
                            "CAS occurrence directory contains an unexpected entry."
                        )
                    occurrence = self._read_metadata(occurrence_path, root=self.root)
                    if self._read_bytes(occurrence_path, root=self.root) != (
                        canonical_json(occurrence.to_dict()) + b"\n"
                    ):
                        raise PixelIntegrityError(
                            "CAS occurrence derivation record is not canonical."
                        )
                    if occurrence.parent_pixel_digest is not None:
                        edges.setdefault(occurrence.pixel_digest, set()).add(
                            occurrence.parent_pixel_digest
                        )
        return edges

    @staticmethod
    def _assert_derivation_dag(edges: Mapping[str, set[str]]) -> None:
        """Reject cycles in an occurrence-derived pixel graph iteratively."""

        state: dict[str, int] = {}
        for start in sorted(edges):
            if state.get(start, 0) == 2:
                continue
            state[start] = 1
            path = [start]
            path_index = {start: 0}
            stack: list[tuple[str, Any]] = [(start, iter(sorted(edges.get(start, set()))))]
            while stack:
                node, parents = stack[-1]
                try:
                    parent = next(parents)
                except StopIteration:
                    stack.pop()
                    state[node] = 2
                    path_index.pop(node, None)
                    path.pop()
                    continue
                parent_state = state.get(parent, 0)
                if parent_state == 2:
                    continue
                if parent_state == 1:
                    cycle_start = path_index[parent]
                    cycle = [*path[cycle_start:], parent]
                    raise PixelIntegrityError(
                        "pixel occurrence derivation contains a cycle: " + " -> ".join(cycle)
                    )
                state[parent] = 1
                path_index[parent] = len(path)
                path.append(parent)
                stack.append((parent, iter(sorted(edges.get(parent, set())))))

    def _reject_derivation_cycle(self, child: str, parent: str) -> None:
        edges = self._derivation_edges()
        edges.setdefault(child, set()).add(parent)
        self._assert_derivation_dag(edges)

    def _verify_derivation_dag(self) -> None:
        """Re-scan and verify the complete occurrence graph after a write."""

        self._assert_derivation_dag(self._derivation_edges())

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        if _is_symlink_or_reparse(path):
            raise PixelPathError("CAS object path must not be a symlink.")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    @staticmethod
    def _verify_read_components(path: Path, root: Path) -> None:
        """Reject a root/component swap before descriptor-backed reads."""

        root_path = Path(os.path.abspath(root))
        target = Path(os.path.abspath(path))
        try:
            relative = target.relative_to(root_path)
        except ValueError as exc:
            raise PixelPathError("CAS read path escapes the configured storage root.") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise PixelPathError("CAS read path is not a contained object path.")
        for component in (*reversed(root_path.parents), root_path):
            if _is_symlink_or_reparse(component):
                raise PixelPathError("CAS read path must not contain symlinks/reparse points.")
        current = root_path
        for name in target.relative_to(root_path).parts[:-1]:
            current /= name
            if _is_symlink_or_reparse(current) or not current.is_dir():
                raise PixelPathError("CAS read parent must be a real directory.")

    @staticmethod
    def _read_bytes(path: Path, *, root: Path) -> bytes:
        PixelStore._verify_read_components(path, root)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            # Recheck after open.  Together with path/fd identity below, this
            # rejects an ancestor swap without reading the target.
            PixelStore._verify_read_components(path, root)
            path_stat = path.lstat()
            descriptor_stat = os.fstat(descriptor)
            if _is_symlink_or_reparse(path):
                raise PixelIntegrityError("CAS object is missing or is a symlink/non-file.")
            if not stat.S_ISREG(path_stat.st_mode) or not stat.S_ISREG(descriptor_stat.st_mode):
                raise PixelIntegrityError("CAS object is missing or is a symlink/non-file.")
            if path_stat.st_nlink != 1 or descriptor_stat.st_nlink != 1:
                raise PixelIntegrityError("CAS object must have exactly one hard link.")
            if (path_stat.st_dev, path_stat.st_ino) != (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ):
                raise PixelIntegrityError("CAS object identity changed while opening it.")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        except FileNotFoundError as exc:
            raise PixelIntegrityError("CAS object is missing or is a symlink/non-file.") from exc
        except PixelStoreError:
            raise
        except OSError as exc:
            raise PixelIntegrityError(f"CAS object cannot be read: {path}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _read_metadata(path: Path, *, root: Path) -> PixelArtifact:
        try:
            encoded = PixelStore._read_bytes(path, root=root)
        except PixelStoreError:
            raise
        try:
            value = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json,
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, PixelStoreError) as exc:
            raise PixelIntegrityError("CAS metadata is not strict JSON.") from exc
        if not isinstance(value, Mapping):
            raise PixelIntegrityError("CAS metadata must be a JSON object.")
        try:
            return PixelArtifact.from_dict(value)
        except (TypeError, ValueError, KeyError) as exc:
            raise PixelIntegrityError("CAS metadata failed PixelArtifact validation.") from exc

    def _verified_read(
        self,
        digest: str,
        spec: PixelSpec | None,
    ) -> tuple[bytes, PixelArtifact]:
        digest_value = _ensure_sha256(digest, "digest")
        raw_path = self.path_for(digest_value)
        metadata_path = raw_path.with_name(raw_path.name + ".json")
        if not (metadata_path.exists() or metadata_path.is_symlink()):
            raise PixelIntegrityError("CAS metadata is required to verify the pixel specification.")
        metadata = self._read_metadata(metadata_path, root=self.root)
        metadata_bytes = self._read_bytes(metadata_path, root=self.root)
        canonical_metadata = canonical_json(metadata.to_dict()) + b"\n"
        if metadata_bytes != canonical_metadata:
            raise PixelIntegrityError("CAS metadata must use its canonical byte representation.")
        if metadata.pixel_digest != digest_value:
            raise PixelIntegrityError("CAS metadata digest does not match requested digest.")
        if metadata.path != f"{digest_value[:2]}/{digest_value[2:]}" or metadata.byte_length <= 0:
            raise PixelIntegrityError("CAS metadata path/length is not canonical.")
        if spec is not None and metadata.spec != spec:
            raise PixelIntegrityError("CAS metadata PixelSpec does not match requested spec.")
        spec = metadata.spec
        data = self._read_bytes(raw_path, root=self.root)
        if len(data) != spec.length:
            raise PixelIntegrityError("CAS object length does not match PixelSpec.")
        actual = pixel_digest(spec, data)
        if actual != digest_value:
            raise PixelIntegrityError("CAS object pixel digest mismatch.")
        if metadata.byte_length != len(data):
            raise PixelIntegrityError("CAS metadata byte length mismatch.")
        if metadata.encoded_size != len(data) or metadata.encoded_sha256 != _encoded_sha256(data):
            raise PixelIntegrityError("CAS raw backing encoding hash/size mismatch.")
        # Object metadata is restricted to facts derived from the CAS key and
        # raw backing bytes.  Session, privacy, source-container and derivation
        # assertions live only in occurrence envelopes and are frozen by the
        # outer corpus/candidate evidence hash.  Two re-signed files inside a
        # writable CAS root therefore cannot promote an occurrence assertion
        # into object truth.
        if (
            metadata.source_encoded_sha256 is not None
            or metadata.source_encoded_size is not None
            or metadata.privacy_class != "private"
            or metadata.retention_class != "persistent"
            or metadata.source_provenance_id != "cas-object-v1"
            or metadata.session_id != "cas-object-v1"
            or metadata.source_sequence != 0
            or metadata.parent_pixel_digest is not None
            or metadata.transform_version is not None
            or metadata.calibration_sha256 is not None
        ):
            raise PixelIntegrityError(
                "CAS object metadata must contain immutable object facts only."
            )
        return data, metadata

    def put(
        self,
        spec_or_pixels: PixelSpec | object,
        pixels_or_spec: object | None = None,
        *,
        encoded_bytes: object | None = None,
        encoded_sha256: str | None = None,
        encoded_hash: str | None = None,
        storage_encoding: str = "raw",
        encoded_size: int | None = None,
        privacy_class: str = "private",
        retention_class: str = "persistent",
        source_provenance_id: str = "unknown",
        session_id: str = "unknown",
        source_sequence: int = 0,
        parent_pixel_digest: str | None = None,
        transform_version: str | None = None,
        calibration_sha256: str | None = None,
    ) -> str:
        """Atomically store pixels and return their canonical pixel digest.

        Both ``put(spec, pixels)`` and ``put(pixels, spec)`` are accepted to
        keep adapters readable; ``put(pixels)`` uses the default 1920x1080
        contract.  ``encoded_bytes`` is optional source-container evidence and
        is represented only by its separate hash in metadata.
        """

        if isinstance(spec_or_pixels, PixelSpec):
            spec = spec_or_pixels
            if pixels_or_spec is None:
                raise TypeError("pixels are required when spec is the first argument.")
            pixels = pixels_or_spec
        elif isinstance(pixels_or_spec, PixelSpec):
            spec = pixels_or_spec
            pixels = spec_or_pixels
        elif pixels_or_spec is None:
            spec = DEFAULT_PIXEL_SPEC
            pixels = spec_or_pixels
        else:
            raise TypeError("put expects (PixelSpec, pixels), (pixels, PixelSpec), or (pixels,).")

        data = _pixel_bytes(spec, pixels)
        digest = pixel_digest(spec, data)
        source_encoded_digest: str | None = None
        source_encoded_size: int | None = None
        if encoded_bytes is not None:
            source_encoded_digest = _encoded_sha256(encoded_bytes)
            actual_encoded_size = _buffer_length(encoded_bytes)
            if encoded_size is not None and encoded_size != actual_encoded_size:
                raise ValueError("encoded_size does not match encoded_bytes.")
            source_encoded_size = actual_encoded_size
        if encoded_sha256 is not None:
            supplied = _ensure_sha256(encoded_sha256, "encoded_sha256")
            if source_encoded_digest is not None and supplied != source_encoded_digest:
                raise PixelIntegrityError("encoded_sha256 does not match encoded_bytes.")
            source_encoded_digest = supplied
        if encoded_hash is not None:
            supplied_hash = _ensure_sha256(encoded_hash, "encoded_hash")
            if source_encoded_digest is not None and supplied_hash != source_encoded_digest:
                raise PixelIntegrityError("encoded_hash does not match encoded payload.")
            source_encoded_digest = supplied_hash
        if source_encoded_digest is not None:
            if encoded_size is None and source_encoded_size is None:
                raise ValueError("encoded_size is required when only an encoded hash is supplied.")
            if source_encoded_size is None:
                source_encoded_size = encoded_size
        elif encoded_size is not None:
            raise ValueError("encoded_size requires encoded source bytes or hash.")

        backing_encoded_digest = _encoded_sha256(data)
        backing_encoded_size = len(data)

        occurrence_artifact = PixelArtifact(
            pixel_digest=digest,
            spec=spec,
            byte_length=len(data),
            path=f"{digest[:2]}/{digest[2:]}",
            encoded_sha256=backing_encoded_digest,
            storage_encoding=storage_encoding,
            encoded_size=backing_encoded_size,
            source_encoded_sha256=source_encoded_digest,
            source_encoded_size=source_encoded_size,
            privacy_class=privacy_class,
            retention_class=retention_class,
            source_provenance_id=source_provenance_id,
            session_id=session_id,
            source_sequence=source_sequence,
            parent_pixel_digest=parent_pixel_digest,
            transform_version=transform_version,
            calibration_sha256=calibration_sha256,
        )
        object_artifact = PixelArtifact(
            pixel_digest=digest,
            spec=spec,
            byte_length=len(data),
            path=f"{digest[:2]}/{digest[2:]}",
            encoded_sha256=backing_encoded_digest,
            encoded_size=backing_encoded_size,
            source_encoded_sha256=None,
            source_encoded_size=None,
            storage_encoding=storage_encoding,
            privacy_class="private",
            retention_class="persistent",
            source_provenance_id="cas-object-v1",
            session_id="cas-object-v1",
            source_sequence=0,
            parent_pixel_digest=None,
            transform_version=None,
            calibration_sha256=None,
        )
        object_bytes = canonical_json(object_artifact.to_dict()) + b"\n"
        occurrence_bytes = canonical_json(occurrence_artifact.to_dict()) + b"\n"

        # PixelArtifact normalizes digest text to lowercase.  Use that
        # normalized value for both parent revalidation and graph checks so an
        # uppercase spelling cannot bypass an existing edge.
        parent_digest = occurrence_artifact.parent_pixel_digest
        # The thread locks preserve the original in-process behavior; the root
        # lock extends this exact transaction across interpreters.
        with _PROCESS_PIXEL_WRITE_LOCK, self._write_lock, self._root_file_lock():
            if parent_digest is not None:
                # Revalidate the parent after acquiring the process lock,
                # immediately before scanning the graph and publishing.
                self._verified_read(parent_digest, None)
                self._reject_derivation_cycle(digest, parent_digest)
            raw_path = self._ensure_digest_directory(digest)
            metadata_path = raw_path.with_name(raw_path.name + ".json")
            if raw_path.exists() or raw_path.is_symlink():
                existing, _ = self._verified_read(digest, spec)
                if existing != data:
                    raise PixelIntegrityError("existing CAS object bytes do not match digest.")
            else:
                if metadata_path.exists() or metadata_path.is_symlink():
                    raise PixelIntegrityError("orphan CAS metadata exists without its raw object.")
                self._atomic_write(raw_path, data)
                self._atomic_write(metadata_path, object_bytes)

            occurrence_directory = self._ensure_occurrence_directory(digest)
            occurrence_path = occurrence_directory / (
                self._occurrence_key(
                    digest,
                    source_provenance_id,
                    session_id,
                    source_sequence,
                )
                + ".json"
            )
            if occurrence_path.exists() or occurrence_path.is_symlink():
                existing_occurrence = self._read_bytes(occurrence_path, root=self.root)
                if existing_occurrence != occurrence_bytes:
                    raise PixelIntegrityError(
                        "existing CAS occurrence conflicts with immutable occurrence metadata."
                    )
            else:
                self._atomic_write(occurrence_path, occurrence_bytes)
            if self._read_metadata(occurrence_path, root=self.root) != occurrence_artifact:
                raise PixelIntegrityError("CAS occurrence failed post-write verification.")
            # Re-read and recompute the independently derived object
            # envelope.  Missing metadata is never reconstructed: an
            # orphan raw file is corruption.
            self._verified_read(digest, spec)
            # Only a derived occurrence changes the graph.  Keep its complete
            # check in this same transaction so a second process cannot append
            # a reverse edge after validating stale state.  Parentless live
            # capture objects add no edge and must not turn admission into an
            # O(total-CAS-occurrences) scan on every frame.
            if parent_digest is not None:
                self._verify_derivation_dag()
        return digest

    def put_artifact(
        self,
        spec_or_pixels: PixelSpec | object,
        pixels_or_spec: object | None = None,
        *,
        encoded_bytes: object | None = None,
        encoded_sha256: str | None = None,
        encoded_hash: str | None = None,
        storage_encoding: str = "raw",
        encoded_size: int | None = None,
        privacy_class: str = "private",
        retention_class: str = "persistent",
        source_provenance_id: str = "unknown",
        session_id: str = "unknown",
        source_sequence: int = 0,
        parent_pixel_digest: str | None = None,
        transform_version: str | None = None,
        calibration_sha256: str | None = None,
    ) -> PixelArtifact:
        digest = self.put(
            spec_or_pixels,
            pixels_or_spec,
            encoded_bytes=encoded_bytes,
            encoded_sha256=encoded_sha256,
            encoded_hash=encoded_hash,
            storage_encoding=storage_encoding,
            encoded_size=encoded_size,
            privacy_class=privacy_class,
            retention_class=retention_class,
            source_provenance_id=source_provenance_id,
            session_id=session_id,
            source_sequence=source_sequence,
            parent_pixel_digest=parent_pixel_digest,
            transform_version=transform_version,
            calibration_sha256=calibration_sha256,
        )
        return self.occurrence(
            digest,
            source_provenance_id=source_provenance_id,
            session_id=session_id,
            source_sequence=source_sequence,
        )

    # Readable aliases used by source adapters.
    store = put
    write = put

    def read(self, digest: str, spec: PixelSpec | None = None) -> bytes:
        data, _ = self._verified_read(digest, spec)
        return data

    get = read
    load = read
    read_pixels = read

    def artifact(self, digest: str) -> PixelArtifact:
        digest_value = _ensure_sha256(digest, "digest")
        _, metadata = self._verified_read(digest_value, None)
        return metadata

    get_artifact = artifact
    read_artifact = artifact

    def occurrence(
        self,
        digest: str,
        *,
        source_provenance_id: str,
        session_id: str,
        source_sequence: int,
    ) -> PixelArtifact:
        """Read and verify one immutable frame occurrence for a CAS object."""

        digest_value = _ensure_sha256(digest, "digest")
        _, object_metadata = self._verified_read(digest_value, None)
        path = self.occurrence_path_for(
            digest_value,
            source_provenance_id,
            session_id,
            source_sequence,
        )
        directory = path.parent
        if directory.is_symlink() or not directory.is_dir():
            raise PixelIntegrityError("CAS occurrence ledger is missing or invalid.")
        occurrence = self._read_metadata(path, root=self.root)
        if self._read_bytes(path, root=self.root) != canonical_json(occurrence.to_dict()) + b"\n":
            raise PixelIntegrityError("CAS occurrence must use canonical bytes.")
        if (
            occurrence.pixel_digest != digest_value
            or occurrence.source_provenance_id != source_provenance_id
            or occurrence.session_id != session_id
            or occurrence.source_sequence != source_sequence
            or occurrence.spec != object_metadata.spec
            or occurrence.path != f"{digest_value[:2]}/{digest_value[2:]}"
            or occurrence.byte_length != object_metadata.byte_length
            or occurrence.storage_encoding != object_metadata.storage_encoding
            or occurrence.encoded_sha256 != object_metadata.encoded_sha256
            or occurrence.encoded_size != object_metadata.encoded_size
        ):
            raise PixelIntegrityError("CAS occurrence identity does not match its ledger key.")
        if occurrence.parent_pixel_digest is not None:
            self._verified_read(occurrence.parent_pixel_digest, None)
        return occurrence

    get_occurrence = occurrence
    read_occurrence = occurrence

    def exists(self, digest: str, spec: PixelSpec | None = None) -> bool:
        try:
            self._verified_read(digest, spec)
        except (OSError, PixelStoreError):
            return False
        return True

    has = exists


def _ensure_inside_root(root: Path, candidate: Path) -> None:
    try:
        root_resolved = root.resolve(strict=False)
        candidate_parent = candidate.parent.resolve(strict=False)
    except OSError as exc:
        raise PixelPathError("CAS path cannot be resolved.") from exc
    if candidate_parent != root_resolved / candidate.parent.name:
        raise PixelPathError("CAS path escapes the configured storage root.")


PixelCAS = PixelStore
ContentAddressedPixelStore = PixelStore
CAS = PixelStore


def write_pixel_artifact(
    root: str | Path,
    spec: PixelSpec,
    pixels: object,
    *,
    encoded_bytes: object | None = None,
    encoded_sha256: str | None = None,
    encoded_hash: str | None = None,
    storage_encoding: str = "raw",
    encoded_size: int | None = None,
    privacy_class: str = "private",
    retention_class: str = "persistent",
    source_provenance_id: str = "unknown",
    session_id: str = "unknown",
    source_sequence: int = 0,
    parent_pixel_digest: str | None = None,
    transform_version: str | None = None,
    calibration_sha256: str | None = None,
) -> PixelArtifact:
    return PixelStore(root).put_artifact(
        spec,
        pixels,
        encoded_bytes=encoded_bytes,
        encoded_sha256=encoded_sha256,
        encoded_hash=encoded_hash,
        storage_encoding=storage_encoding,
        encoded_size=encoded_size,
        privacy_class=privacy_class,
        retention_class=retention_class,
        source_provenance_id=source_provenance_id,
        session_id=session_id,
        source_sequence=source_sequence,
        parent_pixel_digest=parent_pixel_digest,
        transform_version=transform_version,
        calibration_sha256=calibration_sha256,
    )


def read_pixel_artifact(
    root: str | Path,
    digest: str,
    spec: PixelSpec | None = None,
) -> bytes:
    return PixelStore(root).read(digest, spec)


def hash_physical_device_fingerprint(value: str | bytes | bytearray | memoryview) -> str:
    """Return a de-identified SHA-256 fingerprint for a physical device ID."""

    if isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        try:
            payload = memoryview(value).cast("B").tobytes()
        except (TypeError, ValueError) as exc:
            raise TypeError("physical device fingerprint must be text or bytes.") from exc
    if not payload:
        raise ValueError("physical device fingerprint must not be empty.")
    return sha256(payload).hexdigest()


device_fingerprint_sha256 = hash_physical_device_fingerprint
physical_device_fingerprint_hash = hash_physical_device_fingerprint
UNKNOWN_DEVICE_FINGERPRINT_SHA256 = hash_physical_device_fingerprint(b"unknown")


def _deidentified_mapping(
    value: PixelSpec | Mapping[str, Any] | None,
    field_name: str,
) -> Mapping[str, Any]:
    if value is None:
        value = DEFAULT_PIXEL_SPEC
    if isinstance(value, PixelSpec):
        # Capture provenance carries the negotiated transport properties in
        # addition to the canonical pixel layout.  The defaults are explicit
        # rather than inferred from a backend: callers can replace them with
        # measured values by passing a mapping.
        data: Mapping[str, Any] = {
            **value.to_dict(),
            "fps": 30.0,
            "fourcc": "BGR8",
            "backend": "unknown",
        }
    elif isinstance(value, Mapping):
        # Work from one stable snapshot.  Contract validation must not observe
        # different key sets when a caller supplies a mutable/custom Mapping.
        data = dict(value)
    else:
        raise TypeError(f"{field_name} must be PixelSpec or a mapping.")
    _ensure_json_value(data, field_name)
    # Source format declarations are not a place for raw device identifiers.
    # Rejecting those keys is safer than silently dropping fields and claiming
    # that the resulting record describes the negotiated source completely.
    sensitive_names = {
        "serial",
        "serial_number",
        "device_id",
        "device_name",
        "hardware_id",
        "physical_device",
        "path",
        "uri",
    }
    for key in data:
        if key.casefold() in sensitive_names:
            raise ValueError(f"{field_name} contains a physical-device identifier.")
    allowed_names = {
        "width",
        "height",
        "fps",
        "fourcc",
        "backend",
        "channels",
        "pixel_format",
        "dtype",
        "stride",
        "length",
    }
    unknown_names = set(data) - allowed_names
    if unknown_names:
        raise ValueError(f"{field_name} has unknown keys: {sorted(unknown_names)!r}.")
    required_names = {"width", "height", "fps", "fourcc", "backend"}
    missing_names = required_names - set(data)
    if missing_names:
        raise ValueError(f"{field_name} missing key: {sorted(missing_names)[0]}.")
    _ensure_int(data["width"], f"{field_name}.width", positive=True)
    _ensure_int(data["height"], f"{field_name}.height", positive=True)
    if type(data["fps"]) not in (int, float) or data["fps"] <= 0:
        raise ValueError(f"{field_name}.fps must be a positive number.")
    _ensure_non_empty_str(data["fourcc"], f"{field_name}.fourcc")
    _ensure_non_empty_str(data["backend"], f"{field_name}.backend")
    layout_names = {"channels", "pixel_format", "dtype", "stride", "length"}
    present_layout_names = layout_names & set(data)
    if present_layout_names and present_layout_names != layout_names:
        missing_layout = sorted(layout_names - present_layout_names)
        raise ValueError(f"{field_name} incomplete pixel layout: {missing_layout!r}.")
    if present_layout_names:
        try:
            PixelSpec(
                width=data["width"],
                height=data["height"],
                channels=data["channels"],
                pixel_format=data["pixel_format"],
                dtype=data["dtype"],
                stride=data["stride"],
                length=data["length"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} pixel layout is inconsistent.") from exc
    frozen = _freeze_json(data)
    return cast(Mapping[str, Any], frozen)


@dataclass(frozen=True, slots=True, init=False)
class CaptureSourceProvenance:
    """De-identified, strict provenance for one capture source session.

    ``physical_device_fingerprint_sha256`` is the only physical-device value
    retained.  The defaults document the current G1 boundary: the upstream
    queue is unknown, Legacy owns physical input, and Core v2 has zero real
    input enabled/calls.
    """

    requested: Mapping[str, Any]
    negotiated: Mapping[str, Any]
    backend: str
    timestamp_origin: str
    upstream_queue: str
    physical_device_fingerprint_sha256: str
    input_owner: str
    real_input_enabled: bool
    real_input_call_count: int
    source_id: str
    session_id: str
    backend_version: str
    tool_artifact_sha256: str
    dependency_lock_sha256: str
    source_artifact_sha256: str
    source_commit: str
    config_sha256: str
    calibration_sha256: str
    schema_version: str

    def __init__(
        self,
        requested: PixelSpec | Mapping[str, Any] | None = None,
        negotiated: PixelSpec | Mapping[str, Any] | None = None,
        backend: str = "unknown",
        timestamp_origin: str = "host_monotonic_post_retrieve",
        upstream_queue: str = "unknown",
        physical_device_fingerprint_sha256: str | None = None,
        input_owner: str = "legacy",
        real_input_enabled: bool = False,
        real_input_call_count: int = 0,
        source_id: str = "source-unknown",
        session_id: str = "session-unknown",
        backend_version: str = "unknown",
        tool_artifact_sha256: str = "0" * 64,
        dependency_lock_sha256: str = "0" * 64,
        source_artifact_sha256: str = "0" * 64,
        source_commit: str = "0" * 40,
        config_sha256: str = "0" * 64,
        calibration_sha256: str = "0" * 64,
        schema_version: str = PIXEL_SCHEMA_VERSION,
        *,
        requested_format: PixelSpec | Mapping[str, Any] | None = None,
        negotiated_format: PixelSpec | Mapping[str, Any] | None = None,
        backend_name: str | None = None,
        timestamp_source: str | None = None,
        upstream_queue_state: str | None = None,
        device_fingerprint_sha256: str | None = None,
        physical_device_fingerprint: str | bytes | bytearray | memoryview | None = None,
        physical_device_fingerprint_hash: str | None = None,
        real_input: int | bool | None = None,
    ) -> None:
        if requested_format is not None:
            if requested is not None and _deidentified_mapping(
                requested,
                "requested",
            ) != _deidentified_mapping(requested_format, "requested_format"):
                raise ValueError("requested and requested_format must match.")
            requested = requested_format
        if negotiated_format is not None:
            if negotiated is not None and _deidentified_mapping(
                negotiated,
                "negotiated",
            ) != _deidentified_mapping(negotiated_format, "negotiated_format"):
                raise ValueError("negotiated and negotiated_format must match.")
            negotiated = negotiated_format
        if backend_name is not None:
            if backend != "unknown" and backend != backend_name:
                raise ValueError("backend and backend_name must match.")
            backend = backend_name
        if timestamp_source is not None:
            if timestamp_origin != "unknown" and timestamp_origin != timestamp_source:
                raise ValueError("timestamp_origin and timestamp_source must match.")
            timestamp_origin = timestamp_source
        if upstream_queue_state is not None:
            if upstream_queue != "unknown" and upstream_queue != upstream_queue_state:
                raise ValueError("upstream_queue and upstream_queue_state must match.")
            upstream_queue = upstream_queue_state

        supplied_fingerprint = [
            value
            for value in (
                physical_device_fingerprint_sha256,
                device_fingerprint_sha256,
                physical_device_fingerprint_hash,
            )
            if value is not None
        ]
        if len({str(value).lower() for value in supplied_fingerprint}) > 1:
            raise ValueError("physical device fingerprint hash aliases must match.")
        if physical_device_fingerprint is not None:
            raw_hash = hash_physical_device_fingerprint(physical_device_fingerprint)
            if (
                supplied_fingerprint
                and _ensure_sha256(
                    supplied_fingerprint[0],
                    "device fingerprint",
                )
                != raw_hash
            ):
                raise ValueError("physical device fingerprint hash does not match raw fingerprint.")
            supplied_fingerprint = [raw_hash]
        fingerprint = (
            UNKNOWN_DEVICE_FINGERPRINT_SHA256
            if not supplied_fingerprint
            else _ensure_sha256(supplied_fingerprint[0], "physical_device_fingerprint_sha256")
        )

        if real_input is not None:
            if real_input is True or real_input == 1:
                raise ValueError("real input must remain disabled (0).")
            if real_input is not False and real_input != 0:
                raise ValueError("real_input must be exactly 0/False.")
            real_input_enabled = False
        if type(real_input_enabled) is not bool or real_input_enabled:
            raise ValueError("real_input_enabled must be exactly False.")
        calls = _ensure_int(real_input_call_count, "real_input_call_count")
        if calls != 0:
            raise ValueError("real_input_call_count must remain zero.")
        owner = _ensure_non_empty_str(input_owner, "input_owner")
        if owner != "legacy":
            raise ValueError("input_owner must be 'legacy'.")

        source = _ensure_portable_token(source_id, "source_id")
        backend_value = _ensure_non_empty_str(backend, "backend")
        timestamp_value = _ensure_non_empty_str(timestamp_origin, "timestamp_origin")
        queue_value = _ensure_non_empty_str(upstream_queue, "upstream_queue")
        version = _ensure_non_empty_str(schema_version, "schema_version")
        if timestamp_value != "host_monotonic_post_retrieve":
            raise ValueError("timestamp_origin must be host_monotonic_post_retrieve.")
        if queue_value != "unknown":
            raise ValueError("upstream_queue must remain unknown.")
        if version != PIXEL_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PIXEL_SCHEMA_VERSION}.")
        requested_value = _deidentified_mapping(requested, "requested")
        negotiated_value = _deidentified_mapping(negotiated, "negotiated")
        normalized_formats: list[Mapping[str, Any]] = []
        for field_name, format_value in (
            ("requested", requested_value),
            ("negotiated", negotiated_value),
        ):
            declared_backend = format_value["backend"]
            if declared_backend not in {"unknown", backend_value}:
                raise ValueError(f"{field_name}.backend must match backend.")
            normalized_formats.append(
                _deidentified_mapping(
                    {**dict(format_value), "backend": backend_value},
                    field_name,
                )
            )
        requested_value, negotiated_value = normalized_formats

        session = _ensure_portable_token(session_id, "session_id")
        backend_version_value = _ensure_non_empty_str(backend_version, "backend_version")
        tool_hash = _ensure_sha256(tool_artifact_sha256, "tool_artifact_sha256")
        lock_hash = _ensure_sha256(dependency_lock_sha256, "dependency_lock_sha256")
        source_hash = _ensure_sha256(source_artifact_sha256, "source_artifact_sha256")
        commit = _ensure_non_empty_str(source_commit, "source_commit")
        if len(commit) != 40:
            raise ValueError("source_commit must be a 40-character lowercase Git commit.")
        try:
            bytes.fromhex(commit)
        except ValueError as exc:
            raise ValueError("source_commit must be hexadecimal.") from exc
        if commit.lower() != commit:
            raise ValueError("source_commit must be lowercase.")
        config_hash = _ensure_sha256(config_sha256, "config_sha256")
        calibration_hash = _ensure_sha256(calibration_sha256, "calibration_sha256")

        object.__setattr__(self, "requested", requested_value)
        object.__setattr__(self, "negotiated", negotiated_value)
        object.__setattr__(self, "backend", backend_value)
        object.__setattr__(self, "timestamp_origin", timestamp_value)
        object.__setattr__(self, "upstream_queue", queue_value)
        object.__setattr__(self, "physical_device_fingerprint_sha256", fingerprint)
        object.__setattr__(self, "input_owner", owner)
        object.__setattr__(self, "real_input_enabled", False)
        object.__setattr__(self, "real_input_call_count", 0)
        object.__setattr__(self, "source_id", source)
        object.__setattr__(self, "session_id", session)
        object.__setattr__(self, "backend_version", backend_version_value)
        object.__setattr__(self, "tool_artifact_sha256", tool_hash)
        object.__setattr__(self, "dependency_lock_sha256", lock_hash)
        object.__setattr__(self, "source_artifact_sha256", source_hash)
        object.__setattr__(self, "source_commit", commit)
        object.__setattr__(self, "config_sha256", config_hash)
        object.__setattr__(self, "calibration_sha256", calibration_hash)
        object.__setattr__(self, "schema_version", version)

    @property
    def requested_format(self) -> Mapping[str, Any]:
        return self.requested

    @property
    def negotiated_format(self) -> Mapping[str, Any]:
        return self.negotiated

    @property
    def backend_name(self) -> str:
        return self.backend

    @property
    def timestamp_source(self) -> str:
        return self.timestamp_origin

    @property
    def upstream_queue_state(self) -> str:
        return self.upstream_queue

    @property
    def upstream_queue_depth(self) -> str:
        return self.upstream_queue

    @property
    def device_fingerprint_sha256(self) -> str:
        return self.physical_device_fingerprint_sha256

    @property
    def physical_device_fingerprint_hash(self) -> str:
        return self.physical_device_fingerprint_sha256

    @property
    def fingerprint_sha256(self) -> str:
        return self.physical_device_fingerprint_sha256

    @property
    def provenance_id(self) -> str:
        digest = sha256()
        digest.update(b"MAPLE_CAPTURE_PROVENANCE_V1\0")
        digest.update(canonical_json(self.to_dict()))
        return digest.hexdigest()

    @property
    def real_input(self) -> int:
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "session_id": self.session_id,
            "requested": _thaw_json(self.requested),
            "negotiated": _thaw_json(self.negotiated),
            "backend": self.backend,
            "backend_version": self.backend_version,
            "timestamp_origin": self.timestamp_origin,
            "upstream_queue": self.upstream_queue,
            "upstream_queue_depth": self.upstream_queue_depth,
            "physical_device_fingerprint_sha256": self.physical_device_fingerprint_sha256,
            "input_owner": self.input_owner,
            "real_input_enabled": self.real_input_enabled,
            "real_input_call_count": self.real_input_call_count,
            "tool_artifact_sha256": self.tool_artifact_sha256,
            "dependency_lock_sha256": self.dependency_lock_sha256,
            "source_artifact_sha256": self.source_artifact_sha256,
            "source_commit": self.source_commit,
            "config_sha256": self.config_sha256,
            "calibration_sha256": self.calibration_sha256,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict()).decode("utf-8")

    @classmethod
    def from_json(cls, value: str) -> CaptureSourceProvenance:
        if not isinstance(value, str):
            raise TypeError("CaptureSourceProvenance JSON must be str.")
        try:
            payload = json.loads(
                value,
                object_pairs_hook=_reject_duplicate_json,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, PixelStoreError) as exc:
            raise ValueError("CaptureSourceProvenance JSON must be strict JSON.") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("CaptureSourceProvenance JSON must be an object.")
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CaptureSourceProvenance:
        if not isinstance(value, Mapping):
            raise ValueError("CaptureSourceProvenance payload must be a mapping.")
        allowed = {
            "schema_version",
            "source_id",
            "session_id",
            "requested",
            "negotiated",
            "backend",
            "backend_version",
            "timestamp_origin",
            "upstream_queue",
            "upstream_queue_depth",
            "physical_device_fingerprint_sha256",
            "input_owner",
            "real_input_enabled",
            "real_input_call_count",
            "tool_artifact_sha256",
            "dependency_lock_sha256",
            "source_artifact_sha256",
            "source_commit",
            "config_sha256",
            "calibration_sha256",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(
                f"CaptureSourceProvenance payload has unknown keys: {sorted(unknown)!r}."
            )
        for key in allowed:
            if key not in value:
                raise ValueError(f"CaptureSourceProvenance payload missing key: {key}.")
        if value["upstream_queue"] != value["upstream_queue_depth"]:
            raise ValueError("upstream_queue and upstream_queue_depth must match.")
        return cls(
            requested=cast(Mapping[str, Any], value["requested"]),
            negotiated=cast(Mapping[str, Any], value["negotiated"]),
            backend=value["backend"],
            timestamp_origin=value["timestamp_origin"],
            upstream_queue=value["upstream_queue"],
            physical_device_fingerprint_sha256=value["physical_device_fingerprint_sha256"],
            input_owner=value["input_owner"],
            real_input_enabled=value["real_input_enabled"],
            real_input_call_count=value["real_input_call_count"],
            source_id=value["source_id"],
            session_id=value["session_id"],
            backend_version=value["backend_version"],
            tool_artifact_sha256=value["tool_artifact_sha256"],
            dependency_lock_sha256=value["dependency_lock_sha256"],
            source_artifact_sha256=value["source_artifact_sha256"],
            source_commit=value["source_commit"],
            config_sha256=value["config_sha256"],
            calibration_sha256=value["calibration_sha256"],
            schema_version=value["schema_version"],
        )


CaptureProvenance = CaptureSourceProvenance
SourceProvenance = CaptureSourceProvenance


__all__ = [
    "CAS",
    "DEFAULT_CHANNELS",
    "DEFAULT_DTYPE",
    "DEFAULT_HEIGHT",
    "DEFAULT_LENGTH",
    "DEFAULT_PIXEL_FORMAT",
    "DEFAULT_PIXEL_SPEC",
    "DEFAULT_STRIDE",
    "DEFAULT_WIDTH",
    "PIXEL_DIGEST_DOMAIN",
    "PIXEL_SCHEMA_VERSION",
    "UNKNOWN_DEVICE_FINGERPRINT_SHA256",
    "CaptureProvenance",
    "CaptureSourceProvenance",
    "ContentAddressedPixelStore",
    "FramePixelArtifact",
    "PixelArtifact",
    "PixelCAS",
    "PixelIntegrityError",
    "PixelPathError",
    "PixelSpec",
    "PixelStore",
    "PixelStoreError",
    "SourceProvenance",
    "canonical_json",
    "canonical_json_bytes",
    "canonical_pixel_digest",
    "cas_path",
    "compute_pixel_digest",
    "derive_cas_path",
    "device_fingerprint_sha256",
    "digest_pixels",
    "encoded_hash",
    "encoded_sha256",
    "hash_physical_device_fingerprint",
    "physical_device_fingerprint_hash",
    "pixel_digest",
    "pixel_sha256",
    "read_pixel_artifact",
    "validate_pixels",
    "verify_pixel_digest",
    "write_pixel_artifact",
]
