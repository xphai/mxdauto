"""Independently verify a G1-LOC-003C VC-003 live-marker report.

The verifier is intentionally a separate process boundary from the runner.
It treats the report's status and execution bit as annotations, validates the
frozen schema and digests, recomputes bucket selection/accounting/privacy, and
reruns the current ``MinimapMarkerExtractor`` when the restricted rows and
private PixelStore are supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from maple_automation_core.capture.pixel_store import (  # noqa: E402
    PixelStore,
    canonical_json,
    pixel_digest,
)
from maple_automation_core.domain.frame import CaptureHealth, FramePacket  # noqa: E402
from maple_automation_core.localization.minimap_marker import (  # noqa: E402
    MinimapMarkerConfig,
    MinimapMarkerExtractor,
)
from maple_automation_core.replay.vc003_live_marker import (  # noqa: E402
    BUCKET_COUNT,
    BUCKET_DURATION_NS,
    FULL_FRAME_CALIBRATION_SHA256,
    FULL_FRAME_GEOMETRY,
    FULL_FRAME_GEOMETRY_SHA256,
    FULL_FRAME_PIXEL_SPEC,
    GENERATION,
    MAX_AGE_NS,
    ReadOnlyPixelStore,
    default_minimap_marker_config,
)

CONFIG_PATH = ROOT / "configs" / "g1-loc-003c-vc003-readonly-live.json"
REPORT_SCHEMA_PATH = ROOT / "schemas" / "vc003-readonly-localization-report.schema.json"
LEDGER_SCHEMA_PATH = ROOT / "schemas" / "vc003-readonly-localization-ledger-row.schema.json"
SCHEMA_VERSION = "1.0.0"
SCOPE = "G1-LOC-003C"
TRUTH_SCOPE = "live_marker_integration_only"
REPORT_TYPE = "vc003_readonly_localization"
TIMESTAMP_ORIGIN = "host_monotonic_post_retrieve"
CLOCK_DOMAIN = "monotonic"
PIXEL_DIGEST_DOMAIN = "MAPLE_PIXEL_V1"
CHAIN = "VC003Source->accepted FramePacket/CAS->MinimapMarkerExtractor->working-space candidate"
B2_PACKET_SHA256 = "4e21973f66fd5c4480c1417d1509a0e21069551d728bf02607319008cbf74f73"
EXTRACTOR_SHA256 = "508b309fce0988a2b0c1e7f4b2ab13a4702a969be5f0175950cb9f779c18a651"
MARKER_CONFIG_SEMANTIC_SHA256 = "47936cf77e46ebc62fd3d6dae241237307ebb370fd81a197745486812c58f22a"
MARKER_CONFIG_RAW_SHA256 = "2d77fae38f22386a2ab1465a1c837d2b935f26c020c3a10ffd17f086ae8306b5"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
PIXEL_REF_RE = re.compile(r"^external://[^/]+/[^/]+/[^/]+/([a-f0-9]{64})$")
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
STATUS_VALUES = {"candidate", "no_candidate", "rejected", "fault"}
ADMISSION_STATUS_VALUES = (
    "accepted",
    "no_frame",
    "stale",
    "duplicate",
    "out_of_order",
    "timestamp_regression",
    "frame_size_changed",
    "source_mismatch",
    "session_mismatch",
    "clock_domain_mismatch",
    "source_error",
)


class VC003VerificationError(ValueError):
    """Raised for malformed verifier inputs."""


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
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
                raise VC003VerificationError(
                    "artifact path must not contain symlinks/reparse points"
                )
        except FileNotFoundError:
            continue
    return candidate


def load_strict_json(path: Path | str) -> dict[str, Any]:
    target = _lexical_safe_path(path)
    if not target.is_file():
        raise VC003VerificationError("JSON artifact is not a regular file")
    try:
        value = json.loads(
            target.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise VC003VerificationError("invalid strict JSON artifact") from exc
    if not isinstance(value, dict):
        raise VC003VerificationError("JSON artifact must be an object")
    return value


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: Path | str) -> str:
    target = _lexical_safe_path(path)
    if not target.is_file():
        raise VC003VerificationError("artifact is not a regular file")
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise VC003VerificationError(f"{name} is not a SHA-256 digest")
    return value


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.strip().casefold()
    if completed.returncode != 0 or COMMIT_RE.fullmatch(value) is None:
        raise VC003VerificationError("current source commit is unavailable")
    return value


def _strict_jsonl(path: Path | str) -> tuple[list[dict[str, Any]], bytes]:
    target = _lexical_safe_path(path)
    if not target.is_file():
        raise VC003VerificationError("JSONL artifact is not a regular file")
    raw = target.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise VC003VerificationError("JSONL artifact must end with LF")
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        value = json.loads(
            line.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
        if not isinstance(value, dict):
            raise VC003VerificationError("JSONL rows must be objects")
        rows.append(value)
    expected = b"".join(canonical_json(row) + b"\n" for row in rows)
    if raw != expected:
        raise VC003VerificationError("JSONL artifact is not canonical")
    return rows, raw


def _schema_errors(value: Mapping[str, Any], path: Path) -> list[str]:
    try:
        schema = load_strict_json(path)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        return [
            f"schema:{error.json_path}:{error.message}"
            for error in sorted(validator.iter_errors(value), key=lambda item: list(item.path))
        ]
    except Exception as exc:
        return [f"schema_load:{type(exc).__name__}"]


def _config_bindings(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    raw = config.get("expected_bindings")
    if not isinstance(raw, Mapping):
        raise VC003VerificationError("config expected_bindings is missing")
    result: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            raise VC003VerificationError("config binding is malformed")
        kind = value.get("kind")
        digest = value.get("expected_sha256")
        external = value.get("external_ref")
        _sha(digest, f"binding {key}")
        if not isinstance(kind, str) or not kind:
            raise VC003VerificationError(f"binding {key} kind is missing")
        if not isinstance(external, str) or not external.startswith("external://"):
            raise VC003VerificationError(f"binding {key} external ref is invalid")
        result[key] = {
            "kind": kind,
            "expected_sha256": cast(str, digest),
            "external_ref": external,
        }
    return result


def _semantic_digest(key: str, path: Path) -> str:
    if key == "upstream_b2_packet":
        payload = load_strict_json(path)
        declared = payload.get("packet_digest")
        if isinstance(declared, str) and SHA256_RE.fullmatch(declared):
            return declared
        raise VC003VerificationError("upstream B2 packet has no valid packet_digest")
    if key == "loc003b_report_semantic":
        payload = load_strict_json(path)
        declared = payload.get("report_digest")
        if isinstance(declared, str) and SHA256_RE.fullmatch(declared):
            return declared
        return _canonical_digest({k: v for k, v in payload.items() if k != "report_digest"})
    if key in {"base_marker_config_semantic", "marker_config_semantic"}:
        return MinimapMarkerConfig.from_dict(load_strict_json(path)).digest
    if key in {"calibration", "marker_calibration"}:
        payload = load_strict_json(path)
        for field in ("calibration_sha256", "calibration_digest", "digest"):
            value = payload.get(field)
            if isinstance(value, str) and SHA256_RE.fullmatch(value):
                return value
    return sha256_file(path)


def _binding_errors(
    config_bindings: Mapping[str, Mapping[str, str]],
    paths: Mapping[str, Path | str],
    expected: Mapping[str, str] | None,
    *,
    require_all: bool,
) -> list[str]:
    errors: list[str] = []
    expected_map = dict(expected or {})
    for key, binding in config_bindings.items():
        path_value = paths.get(key)
        if path_value is None and key == "loc003b_report_semantic":
            path_value = paths.get("loc003b_report_raw")
        if path_value is None and key == "base_marker_config_semantic":
            path_value = paths.get("base_marker_config_raw")
        if path_value is None:
            if require_all:
                errors.append(f"binding_missing:{key}")
            continue
        try:
            actual = _semantic_digest(key, Path(path_value))
        except Exception as exc:
            errors.append(f"binding_unreadable:{key}:{type(exc).__name__}")
            continue
        required = expected_map.get(key, binding.get("expected_sha256"))
        if not isinstance(required, str) or actual.casefold() != required.casefold():
            errors.append(f"binding_digest_mismatch:{key}")
    for key, required in expected_map.items():
        if key in config_bindings:
            continue
        path_value = paths.get(key)
        if path_value is None:
            errors.append(f"binding_missing:{key}")
            continue
        try:
            actual = _semantic_digest(key, Path(path_value))
        except Exception as exc:
            errors.append(f"binding_unreadable:{key}:{type(exc).__name__}")
            continue
        if actual.casefold() != required.casefold():
            errors.append(f"binding_digest_mismatch:{key}")
    return errors


def _frame_identity_digest(
    *,
    session_id: str,
    source_id: str,
    frame_id: int,
    captured_at_ns: int,
    admitted_at_ns: int,
    pixel_digest_value: str,
) -> str:
    return _canonical_digest(
        {
            "session_id": session_id,
            "source_id": source_id,
            "frame_id": frame_id,
            "captured_at_ns": captured_at_ns,
            "admitted_at_ns": admitted_at_ns,
            "pixel_digest": pixel_digest_value,
        }
    )


def _public_row_errors(rows: object) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    if not isinstance(rows, list):
        return ["public_rows_not_array"], []
    if len(rows) != BUCKET_COUNT:
        errors.append("public_rows_count")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for ordinal, value in enumerate(rows):
        if not isinstance(value, dict):
            errors.append(f"public_row_invalid:{ordinal}")
            continue
        row = dict(value)
        normalized.append(row)
        bucket = row.get("bucket_index")
        if not isinstance(bucket, int) or bucket < 0 or bucket >= BUCKET_COUNT:
            errors.append(f"public_bucket_invalid:{ordinal}")
            continue
        if bucket in seen:
            errors.append(f"public_bucket_duplicate:{bucket}")
        seen.add(bucket)
        if bucket != ordinal:
            errors.append(f"public_bucket_order:{ordinal}")
        if row.get("sample_ordinal", ordinal) != ordinal:
            errors.append(f"public_sample_order:{bucket}")
        if row.get("selected") is not True:
            errors.append(f"public_not_selected:{bucket}")
        offset = row.get("bucket_offset_ns")
        if not isinstance(offset, int) or not 0 <= offset < BUCKET_DURATION_NS:
            errors.append(f"public_bucket_offset:{bucket}")
        status = row.get("status")
        if status not in STATUS_VALUES:
            errors.append(f"public_status:{bucket}")
        candidate = row.get("candidate_digest")
        if status == "candidate" and not isinstance(candidate, str):
            errors.append(f"public_candidate_missing:{bucket}")
        if status in {"no_candidate", "rejected", "fault"} and candidate is not None:
            errors.append(f"public_candidate_forbidden:{bucket}")
        for name in ("frame_digest", "pixel_digest", "evidence_digest", "result_digest"):
            if not isinstance(row.get(name), str) or SHA256_RE.fullmatch(row[name]) is None:
                errors.append(f"public_{name}:{bucket}")
        for name in ("frame_digest", "pixel_digest", "evidence_digest", "result_digest"):
            if isinstance(row.get(name), str):
                row[name] = row[name].lower()
        if isinstance(candidate, str):
            row["candidate_digest"] = candidate.lower()
        supplied = row.get("row_digest")
        expected = _canonical_digest({k: v for k, v in row.items() if k != "row_digest"})
        if supplied != expected:
            errors.append(f"public_row_digest:{bucket}")
        _assert_private_free(row, errors, f"public_row:{ordinal}")
    return errors, normalized


def _assert_private_free(value: object, errors: list[str], where: str) -> None:
    forbidden = {
        "path",
        "paths",
        "absolute_path",
        "raw_pixel",
        "raw_pixels",
        "pixels",
        "image_ref",
        "coordinates",
        "coordinate",
        "bbox",
        "source_bbox",
        "source_centroid",
        "working_bbox",
        "anchor_working",
        "centroid",
        "anchor",
        "device_original_id",
    }

    def walk(node: object, path: str) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                key_text = str(key)
                if key_text.casefold() in forbidden:
                    errors.append(f"privacy_private_key:{where}.{key_text}")
                walk(child, f"{path}.{key_text}")
        elif isinstance(node, list | tuple):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")
        elif isinstance(node, bytes | bytearray | memoryview):
            errors.append(f"privacy_raw_bytes:{where}")
        elif isinstance(node, str) and (
            ":\\" in node or node.startswith("/") or node.startswith("\\\\")
        ):
            errors.append(f"privacy_absolute_path:{where}")

    walk(value, where)


def _result_status(result: object) -> str:
    value = getattr(result, "status", result.get("status") if isinstance(result, Mapping) else None)
    value = getattr(value, "value", value)
    text = str(value).casefold()
    if text in {"candidate", "found", "detected", "accepted"}:
        return "candidate"
    if text in {"no_candidate", "no_marker", "no-marker", "none", "unknown"}:
        return "no_candidate"
    return "fault"


def _result_digest(result: object) -> str:
    value = getattr(result, "result_digest", None)
    if not isinstance(value, str) and isinstance(result, Mapping):
        value = result.get("result_digest")
    if not isinstance(value, str):
        value = getattr(result, "digest", None)
    if isinstance(value, str) and SHA256_RE.fullmatch(value):
        return value
    body = result.to_dict() if callable(getattr(result, "to_dict", None)) else result
    return _canonical_digest(body)


def _nested_digest(result: object, name: str) -> str | None:
    value = result.get(name) if isinstance(result, Mapping) else getattr(result, name, None)
    if isinstance(value, str) and SHA256_RE.fullmatch(value):
        return value
    child = (
        result.get(name.removesuffix("_digest"))
        if isinstance(result, Mapping)
        else getattr(result, name.removesuffix("_digest"), None)
    )
    if child is None:
        return None
    declared = getattr(child, "digest", None)
    if isinstance(declared, str) and SHA256_RE.fullmatch(declared):
        return declared
    body = child.to_dict() if callable(getattr(child, "to_dict", None)) else child
    return _canonical_digest(body)


def _private_pixel_digest(row: Mapping[str, Any]) -> str | None:
    value = row.get("pixel_digest")
    if isinstance(value, str) and SHA256_RE.fullmatch(value):
        return value
    ref = row.get("pixel_ref")
    if not isinstance(ref, str):
        return None
    match = re.search(r"([a-f0-9]{64})$", ref)
    return None if match is None else match.group(1)


def _private_row_errors(
    rows: Sequence[Mapping[str, Any]], public_rows: Sequence[Mapping[str, Any]]
) -> tuple[list[str], dict[int, Mapping[str, Any]]]:
    errors: list[str] = []
    by_bucket: dict[int, Mapping[str, Any]] = {}
    if len(rows) != BUCKET_COUNT:
        errors.append("private_rows_count")
    for ordinal, row in enumerate(rows):
        bucket = row.get("bucket_index")
        if not isinstance(bucket, int) or not 0 <= bucket < BUCKET_COUNT:
            errors.append(f"private_bucket_invalid:{ordinal}")
            continue
        if bucket in by_bucket:
            errors.append(f"private_bucket_duplicate:{bucket}")
        by_bucket[bucket] = row
        if bucket != ordinal:
            errors.append(f"private_bucket_order:{ordinal}")
        if row.get("sample_id") != f"bucket-{bucket:03d}":
            errors.append(f"private_sample_id:{bucket}")
        public = public_rows[bucket] if bucket < len(public_rows) else {}
        for name in ("frame_digest", "candidate_digest", "evidence_digest", "result_digest"):
            if name == "candidate_digest" and public.get(name) is None:
                if row.get(name) is not None:
                    errors.append(f"private_{name}:{bucket}")
            elif row.get(name) != public.get(name):
                errors.append(f"private_{name}:{bucket}")
        if row.get("row_digest") != public.get("row_digest"):
            errors.append(f"private_row_digest:{bucket}")
        if row.get("status") != public.get("status"):
            errors.append(f"private_status:{bucket}")
        if row.get("generation") != GENERATION:
            errors.append(f"private_generation:{bucket}")
        if not isinstance(row.get("artifact_ref"), str) or not row["artifact_ref"].startswith(
            "external://"
        ):
            errors.append(f"private_artifact_ref:{bucket}")
        if row.get("privacy_class") != "restricted" or row.get("retention_class") != "candidate":
            errors.append(f"private_retention:{bucket}")
        if not isinstance(row.get("source_provenance_id"), str) or not row["source_provenance_id"]:
            errors.append(f"private_provenance:{bucket}")
        occurrence_digest = row.get("occurrence_artifact_sha256")
        if not isinstance(occurrence_digest, str) or SHA256_RE.fullmatch(occurrence_digest) is None:
            errors.append(f"private_occurrence_digest:{bucket}")
        digest = _private_pixel_digest(row)
        if digest is None:
            errors.append(f"private_pixel_ref:{bucket}")
        elif digest != public.get("pixel_digest"):
            errors.append(f"private_pixel_digest:{bucket}")
        identity = (
            row.get("session_id"),
            row.get("source_id"),
            row.get("frame_id"),
            row.get("captured_at_ns"),
            row.get("received_at_ns"),
        )
        if (
            digest is not None
            and isinstance(identity[0], str)
            and isinstance(identity[1], str)
            and isinstance(identity[2], int)
            and isinstance(identity[3], int)
            and isinstance(identity[4], int)
        ):
            expected_frame = _frame_identity_digest(
                session_id=identity[0],
                source_id=identity[1],
                frame_id=identity[2],
                captured_at_ns=identity[3],
                admitted_at_ns=identity[4],
                pixel_digest_value=digest,
            )
            if (
                row.get("frame_digest") != expected_frame
                or public.get("frame_digest") != expected_frame
            ):
                errors.append(f"private_frame_identity_digest:{bucket}")
            if row.get("source_sequence") != identity[2]:
                errors.append(f"private_source_sequence:{bucket}")
        else:
            errors.append(f"private_frame_identity:{bucket}")
        for name in ("frame_digest", "evidence_digest", "result_digest"):
            if not isinstance(row.get(name), str) or SHA256_RE.fullmatch(row[name]) is None:
                errors.append(f"private_{name}_format:{bucket}")
    for index in range(BUCKET_COUNT):
        if index not in by_bucket:
            errors.append(f"private_bucket_missing:{index}")
    return errors, by_bucket


def _make_packet(row: Mapping[str, Any], pixel: str) -> FramePacket:
    session_id = row.get("session_id")
    source_id = row.get("source_id")
    frame_id = row.get("frame_id")
    captured = row.get("captured_at_ns")
    received = row.get("received_at_ns")
    if not all(isinstance(item, str) for item in (session_id, source_id)):
        raise VC003VerificationError("private row identity is malformed")
    if not all(isinstance(item, int) for item in (frame_id, captured, received)):
        raise VC003VerificationError("private row timestamp is malformed")
    health = CaptureHealth(
        session_id=session_id,
        frame_id=frame_id,
        source_id=source_id,
        content_hash=pixel,
        clock_domain=CLOCK_DOMAIN,
        captured_at_ns=captured,
        received_at_ns=received,
        transform_version="capture-v1",
        max_age_ns=MAX_AGE_NS,
    )
    return FramePacket(
        source_id=source_id,
        session_id=session_id,
        frame_id=frame_id,
        captured_at_ns=captured,
        received_at_ns=received,
        transform_version="capture-v1",
        clock_domain=CLOCK_DOMAIN,
        content_hash=pixel,
        source_geometry=FULL_FRAME_GEOMETRY,
        image_ref=f"cas://sha256/{pixel}",
        capture_health=health,
        image_metadata={
            "pixel_spec": FULL_FRAME_PIXEL_SPEC.to_dict(),
            "geometry_sha256": FULL_FRAME_GEOMETRY_SHA256,
            "calibration_sha256": FULL_FRAME_CALIBRATION_SHA256,
            "transform_version": "capture-v1",
            "content_hash": pixel,
            "pixel_digest": pixel,
        },
    )


def _rerun_errors(
    private_rows: Mapping[int, Mapping[str, Any]],
    public_rows: Sequence[Mapping[str, Any]],
    cas_root: Path | str | None,
    *,
    metadata_only: bool,
) -> list[str]:
    errors: list[str] = []
    if cas_root is None:
        return ["private_cas_missing"]
    try:
        root = _lexical_safe_path(cas_root)
        if not root.is_dir():
            raise VC003VerificationError("private CAS root is not a directory")
        store = PixelStore(root)
        marker_cache: dict[tuple[str, str, str], MinimapMarkerExtractor] = {}
        expected_pixel_digests: set[str] = set()
        for bucket, row in sorted(private_rows.items()):
            pixel = _private_pixel_digest(row)
            if pixel is None:
                continue
            expected_pixel_digests.add(pixel)
            try:
                data = store.read(pixel, FULL_FRAME_PIXEL_SPEC)
                if pixel_digest(FULL_FRAME_PIXEL_SPEC, data) != pixel:
                    errors.append(f"cas_digest:{bucket}")
                    continue
                artifact = store.artifact(pixel)
                if artifact.pixel_digest != pixel:
                    errors.append(f"cas_artifact:{bucket}")
                    continue
                provenance = row.get("source_provenance_id")
                session = row.get("session_id")
                sequence = row.get("source_sequence")
                if (
                    not isinstance(provenance, str)
                    or not isinstance(session, str)
                    or not isinstance(sequence, int)
                ):
                    errors.append(f"cas_occurrence_identity:{bucket}")
                    continue
                occurrence = store.occurrence(
                    pixel,
                    source_provenance_id=provenance,
                    session_id=session,
                    source_sequence=sequence,
                )
                if (
                    occurrence.privacy_class != "restricted"
                    or occurrence.retention_class != "candidate"
                    or occurrence.artifact_sha256 != row.get("occurrence_artifact_sha256")
                ):
                    errors.append(f"cas_occurrence:{bucket}")
                    continue
            except Exception as exc:
                errors.append(f"cas_missing:{bucket}:{type(exc).__name__}")
                continue
            if metadata_only:
                continue
            try:
                packet = _make_packet(row, pixel)
                key = (str(row["session_id"]), str(row["source_id"]), CLOCK_DOMAIN)
                extractor = marker_cache.get(key)
                if extractor is None:
                    config = default_minimap_marker_config(
                        session_id=key[0], source_id=key[1], clock_domain=key[2]
                    )
                    extractor = MinimapMarkerExtractor(config, ReadOnlyPixelStore(store))
                    marker_cache[key] = extractor
                observed = row.get("received_at_ns")
                checked = row.get("observed_at_ns", observed)
                if not isinstance(observed, int) or not isinstance(checked, int):
                    raise VC003VerificationError("private timestamps are malformed")
                result = extractor.extract(
                    packet,
                    now_ns=checked,
                    observed_at_ns=observed,
                    generation=GENERATION,
                )
                public = public_rows[bucket]
                if _result_status(result) != public.get("status"):
                    errors.append(f"rerun_status:{bucket}")
                if _result_digest(result) != public.get("result_digest"):
                    errors.append(f"rerun_result_digest:{bucket}")
                if _nested_digest(result, "evidence_digest") != public.get("evidence_digest"):
                    errors.append(f"rerun_evidence_digest:{bucket}")
                if _nested_digest(result, "candidate_digest") != public.get("candidate_digest"):
                    errors.append(f"rerun_candidate_digest:{bucket}")
            except Exception as exc:
                errors.append(f"rerun_error:{bucket}:{type(exc).__name__}")
        root = _lexical_safe_path(cas_root)
        occurrence_files = [
            path for path in root.rglob("*.json") if path.parent.name.endswith(".occurrences")
        ]
        object_metadata = [
            path for path in root.rglob("*.json") if not path.parent.name.endswith(".occurrences")
        ]
        raw_objects = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.name != ".pixel-store.lock"
            and path.suffix == ""
            and re.fullmatch(r"[a-f0-9]{62}", path.name) is not None
        ]
        if len(occurrence_files) != BUCKET_COUNT:
            errors.append("cas_occurrence_count")
        if len(object_metadata) != len(expected_pixel_digests):
            errors.append("cas_object_metadata_count")
        if len(raw_objects) != len(expected_pixel_digests):
            errors.append("cas_raw_object_count")
    except Exception as exc:
        errors.append(f"cas_root:{type(exc).__name__}")
    return errors


def _accepted_ledger_errors(
    path: Path | str | None,
    report: Mapping[str, Any],
    private_rows: Mapping[int, Mapping[str, Any]],
) -> list[str]:
    if path is None:
        return ["accepted_ledger_missing"]
    try:
        rows, _raw = _strict_jsonl(path)
    except Exception as exc:
        return [f"accepted_ledger_invalid:{type(exc).__name__}"]
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    last_received = -1
    last_captured = -1
    last_frame_id = -1
    allowed_fields = {
        "status",
        "frame_id",
        "captured_at_ns",
        "received_at_ns",
        "session_id",
        "source_id",
        "pixel_digest",
        "frame_digest",
    }
    capture = report.get("capture")
    expected_source_id = capture.get("source_id") if isinstance(capture, Mapping) else None
    selected_sessions = {
        row.get("session_id")
        for row in private_rows.values()
        if isinstance(row.get("session_id"), str)
    }
    for index, row in enumerate(rows):
        unexpected = sorted(set(row) - allowed_fields)
        if unexpected:
            errors.append(f"accepted_ledger_extra_fields:{index}:{','.join(unexpected)}")
        _assert_private_free(row, errors, f"accepted_ledger:{index}")
        for name in ("frame_id", "captured_at_ns", "received_at_ns"):
            value = row.get(name)
            if not isinstance(value, int) or value < 0:
                errors.append(f"accepted_ledger_{name}:{index}")
        for name in ("session_id", "source_id"):
            if not isinstance(row.get(name), str) or not row[name]:
                errors.append(f"accepted_ledger_{name}:{index}")
        for name in ("pixel_digest", "frame_digest"):
            if not isinstance(row.get(name), str) or SHA256_RE.fullmatch(row[name]) is None:
                errors.append(f"accepted_ledger_{name}:{index}")
        if (
            isinstance(row.get("session_id"), str)
            and isinstance(row.get("source_id"), str)
            and isinstance(row.get("frame_id"), int)
            and isinstance(row.get("captured_at_ns"), int)
            and isinstance(row.get("received_at_ns"), int)
            and isinstance(row.get("pixel_digest"), str)
            and SHA256_RE.fullmatch(row["pixel_digest"]) is not None
        ):
            expected_frame = _frame_identity_digest(
                session_id=row["session_id"],
                source_id=row["source_id"],
                frame_id=row["frame_id"],
                captured_at_ns=row["captured_at_ns"],
                admitted_at_ns=row["received_at_ns"],
                pixel_digest_value=row["pixel_digest"],
            )
            if row.get("frame_digest") != expected_frame:
                errors.append(f"accepted_ledger_frame_identity_digest:{index}")
        if row.get("status") not in {None, "accepted"}:
            errors.append(f"accepted_ledger_status:{index}")
        received = row.get("received_at_ns")
        captured = row.get("captured_at_ns")
        frame_id = row.get("frame_id")
        if not isinstance(received, int) or received < last_received:
            errors.append(f"accepted_ledger_order:{index}")
        if isinstance(received, int):
            last_received = received
        if not isinstance(captured, int) or captured < last_captured:
            errors.append(f"accepted_ledger_capture_order:{index}")
        if isinstance(captured, int):
            last_captured = captured
        if not isinstance(frame_id, int) or frame_id <= last_frame_id:
            errors.append(f"accepted_ledger_frame_order:{index}")
        if isinstance(frame_id, int):
            last_frame_id = frame_id
        if isinstance(expected_source_id, str) and row.get("source_id") != expected_source_id:
            errors.append(f"accepted_ledger_source:{index}")
        if selected_sessions and row.get("session_id") not in selected_sessions:
            errors.append(f"accepted_ledger_session:{index}")
        normalized.append({key: value for key, value in row.items() if key != "status"})
    expected_digest = report.get("admission", {}).get("accepted_frame_ledger_sha256")
    if _canonical_digest(normalized) != expected_digest:
        errors.append("accepted_ledger_digest")
    admission = report.get("admission", {})
    if admission.get("accepted_count") != len(rows):
        errors.append("accepted_ledger_count")
    if admission.get("accepted_packet_count") not in {None, len(rows)}:
        errors.append("accepted_packet_count")
    if admission.get("accepted_frame_ledger_sha256") != report.get("lineage", {}).get(
        "accepted_frame_ledger_sha256"
    ):
        errors.append("accepted_ledger_lineage_digest")
    # Derive the bucket origin from the selected rows and require one common
    # origin.  This makes the first-accepted check independent of report text.
    public_rows = report.get("public_selected_rows")
    public_by_bucket = (
        {
            row.get("bucket_index"): row
            for row in public_rows
            if isinstance(public_rows, list) and isinstance(row, Mapping)
        }
        if isinstance(public_rows, list)
        else {}
    )
    origins: set[int] = set()
    for bucket, row in private_rows.items():
        received = row.get("received_at_ns")
        public = public_by_bucket.get(bucket)
        offset = public.get("bucket_offset_ns") if isinstance(public, Mapping) else None
        if isinstance(received, int) and isinstance(offset, int):
            origins.add(received - bucket * BUCKET_DURATION_NS - offset)
        else:
            errors.append(f"bucket_origin_fields:{bucket}")
    if len(origins) != 1:
        errors.append("bucket_origin_inconsistent")
        return errors
    origin = next(iter(origins))
    first_by_bucket: dict[int, Mapping[str, Any]] = {}
    for row in normalized:
        received = row.get("received_at_ns")
        if not isinstance(received, int) or received < origin:
            continue
        bucket = (received - origin) // BUCKET_DURATION_NS
        if 0 <= bucket < BUCKET_COUNT and bucket not in first_by_bucket:
            first_by_bucket[bucket] = row
    for bucket, private in private_rows.items():
        first = first_by_bucket.get(bucket)
        if first is None:
            errors.append(f"bucket_first_missing:{bucket}")
            continue
        if (
            first.get("frame_id") != private.get("frame_id")
            or first.get("received_at_ns") != private.get("received_at_ns")
            or first.get("captured_at_ns") != private.get("captured_at_ns")
            or first.get("session_id") != private.get("session_id")
            or first.get("source_id") != private.get("source_id")
            or first.get("pixel_digest") != _private_pixel_digest(private)
        ):
            errors.append(f"bucket_first_mismatch:{bucket}")
    return errors


def _public_ledger_errors(
    path: Path | str | None,
    public_rows: Sequence[Mapping[str, Any]],
    schema_path: Path,
) -> list[str]:
    if path is None:
        return []
    try:
        rows, _raw = _strict_jsonl(path)
    except Exception as exc:
        return [f"public_ledger_invalid:{type(exc).__name__}"]
    errors = [
        f"public_ledger_schema:{index}:{item}"
        for index, row in enumerate(rows)
        for item in _schema_errors(row, schema_path)
    ]
    if rows != list(public_rows):
        errors.append("public_ledger_mismatch")
    return errors


def _report_digest_errors(report: Mapping[str, Any]) -> list[str]:
    supplied = report.get("report_digest")
    try:
        expected = _canonical_digest(
            {
                key: value
                for key, value in report.items()
                if key not in {"report_digest", "report_sha256"}
            }
        )
    except Exception as exc:
        return [f"report_digest_compute:{type(exc).__name__}"]
    return [] if supplied == expected else ["report_digest"]


def _strict_structure_errors(report: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("scope_excluded") != SCOPE_EXCLUDED:
        errors.append("scope_excluded")
    if report.get("scope") != SCOPE or report.get("truth_scope") != TRUTH_SCOPE:
        errors.append("scope")
    if report.get("report_type") != REPORT_TYPE:
        errors.append("report_type")
    if report.get("source_commit") and COMMIT_RE.fullmatch(str(report["source_commit"])) is None:
        errors.append("source_commit")
    if report.get("config_sha256") and SHA256_RE.fullmatch(str(report["config_sha256"])) is None:
        errors.append("config_sha256")
    if report.get("lineage", {}).get("chain") != CHAIN:
        errors.append("lineage_chain")
    if report.get("lineage", {}).get("upstream_b2_packet_sha256") != B2_PACKET_SHA256:
        errors.append("lineage_upstream")
    if report.get("lineage", {}).get("pixel_digest_domain") != PIXEL_DIGEST_DOMAIN:
        errors.append("lineage_pixel_domain")
    if report.get("lineage", {}).get("cas_required") is not True:
        errors.append("lineage_cas_required")
    if report.get("lineage", {}).get("candidate_output") != "working_space_candidate":
        errors.append("lineage_candidate_output")
    for path in (
        ("capture", "geometry"),
        ("marker", "geometry"),
    ):
        if report.get(path[0], {}).get(path[1]) != FULL_FRAME_GEOMETRY.to_dict():
            errors.append(f"{path[0]}_geometry")
    timing = report.get("timing", {})
    frozen_timing = {
        "warmup_seconds": 30,
        "measurement_seconds": 300,
        "bucket_count": 100,
        "bucket_seconds": 3,
        "bucket_clock": "FramePacket.received_at_ns",
        "bucket_boundary": "half_open",
        "generation": GENERATION,
        "timestamp_origin": TIMESTAMP_ORIGIN,
        "clock_domain": CLOCK_DOMAIN,
        "monotonic": True,
    }
    for key, value in frozen_timing.items():
        if timing.get(key) != value:
            errors.append(f"timing_{key}")
    for branch in (report.get("zero_input", {}), report.get("privacy", {})):
        if branch.get("scope") != SCOPE or branch.get("reused_from_b2") is not False:
            errors.append("run_specific_scope")
    return errors


def _public_report_privacy_errors(report: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    banned_keys = {
        "raw_bytes",
        "pixels",
        "mask",
        "absolute_path",
        "device_original_id",
        "working_candidate",
        "source_bbox",
        "source_centroid",
        "private_cas_path",
    }

    def walk(value: object, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                if key_text.casefold() in banned_keys:
                    errors.append(f"privacy_banned_key:{path}.{key_text}")
                walk(child, f"{path}.{key_text}")
        elif isinstance(value, list | tuple):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, bytes | bytearray | memoryview):
            errors.append(f"privacy_raw_bytes:{path}")
        elif isinstance(value, str):
            if (
                value.startswith("/")
                or value.startswith("\\\\")
                or (len(value) >= 3 and value[1] == ":" and value[2] in "\\/")
            ):
                errors.append(f"privacy_absolute_path:{path}")
            if re.search(r"(?i)(?:USB\\|VID_[0-9A-F]{4}|PID_[0-9A-F]{4})", value):
                errors.append(f"privacy_raw_device_id:{path}")

    walk(report, "report")
    return errors


def _strict_config_errors(config: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_window = {
        "warmup_seconds": 30,
        "measurement_seconds": 300,
        "bucket_count": BUCKET_COUNT,
        "bucket_seconds": 3,
        "target_admission_hz": 15.0,
        "poll_timeout_seconds": 0.05,
        "bucket_clock": "FramePacket.received_at_ns",
        "bucket_boundary": "half_open",
        "generation": GENERATION,
    }
    if config.get("measurement_window") != expected_window:
        errors.append("config_measurement_window")
    marker = config.get("marker")
    if not isinstance(marker, Mapping):
        errors.append("config_marker")
    else:
        expected_marker = {
            "extractor_artifact_sha256": EXTRACTOR_SHA256,
            "base_config_raw_sha256": MARKER_CONFIG_RAW_SHA256,
            "base_config_semantic_sha256": MARKER_CONFIG_SEMANTIC_SHA256,
            "calibration_sha256": FULL_FRAME_CALIBRATION_SHA256,
        }
        for key, value in expected_marker.items():
            if marker.get(key) != value:
                errors.append(f"config_marker_{key}")
        base = marker.get("base_config")
        try:
            parsed = MinimapMarkerConfig.from_dict(base)
            if parsed.digest != MARKER_CONFIG_SEMANTIC_SHA256:
                errors.append("config_marker_semantic")
        except Exception:
            errors.append("config_marker_base")
    if config.get("input_policy") != {
        "input_owner": "legacy",
        "real_input_enabled": False,
        "real_input_call_count": 0,
        "core_v2_real_input_call_count": 0,
        "double_write_event_count": 0,
    }:
        errors.append("config_input_policy")
    output = config.get("output_policy")
    if not isinstance(output, Mapping) or any(
        output.get(key) != value
        for key, value in {
            "public_row_kind": "hash_only",
            "public_selected_row_count": BUCKET_COUNT,
            "one_row_per_bucket": True,
            "allow_duplicate_pixel_digest": True,
            "include_coordinates": False,
            "include_raw_bytes": False,
            "include_absolute_paths": False,
            "include_device_original_id": False,
        }.items()
    ):
        errors.append("config_output_policy")
    if config.get("run_specific_audits") is not True:
        errors.append("config_run_specific_audits")
    return errors


def _verify_report_impl(
    report: Mapping[str, Any] | Path | str,
    *,
    config_path: Path | str = CONFIG_PATH,
    schema_path: Path | str = REPORT_SCHEMA_PATH,
    ledger_schema_path: Path | str = LEDGER_SCHEMA_PATH,
    private_cas_root: Path | str | None = None,
    cas_root: Path | str | None = None,
    private_rows_path: Path | str | None = None,
    restricted_rows_path: Path | str | None = None,
    accepted_ledger_path: Path | str | None = None,
    public_ledger_path: Path | str | None = None,
    binding_paths: Mapping[str, Path | str] | None = None,
    expected_bindings: Mapping[str, str] | None = None,
    expected_source_commit: str | None = None,
    require_external_bindings: bool = True,
    metadata_only: bool = False,
) -> list[str]:
    """Return all independent verification failures.

    A missing private rows file or CAS is always a failure.  ``metadata_only``
    only skips the expensive extractor rerun after the private bytes have
    already been proven present and hash-correct.
    """

    errors: list[str] = []
    report_path: Path | None = None
    if isinstance(report, str | Path):
        report_path = _lexical_safe_path(report)
        try:
            payload = load_strict_json(report_path)
        except Exception as exc:
            return [f"report_invalid:{type(exc).__name__}"]
        report_value: Mapping[str, Any] = payload
        try:
            if report_path.read_bytes() != canonical_json(payload) + b"\n":
                errors.append("report_not_canonical")
        except OSError:
            errors.append("report_unreadable")
    elif isinstance(report, Mapping):
        report_value = report
    else:
        return ["report_type"]
    errors.extend(_schema_errors(report_value, Path(schema_path)))
    errors.extend(_report_digest_errors(report_value))
    errors.extend(_strict_structure_errors(report_value))
    errors.extend(_public_report_privacy_errors(report_value))

    config: Mapping[str, Any] | None = None
    try:
        config = load_strict_json(config_path)
        config_bindings = _config_bindings(config)
        errors.extend(_strict_config_errors(config))
        if report_value.get("config_sha256") != sha256_file(config_path):
            errors.append("config_digest")
        if report_value.get("expected_bindings") != config_bindings:
            errors.append("expected_bindings")
        errors.extend(
            _binding_errors(
                config_bindings,
                binding_paths or {},
                expected_bindings,
                require_all=require_external_bindings,
            )
        )
    except Exception as exc:
        errors.append(f"config_invalid:{type(exc).__name__}")
        config_bindings = {}
    effective_source_commit = (
        _git_head() if expected_source_commit is None else expected_source_commit.casefold()
    )
    if (
        COMMIT_RE.fullmatch(effective_source_commit) is None
        or report_value.get("source_commit") != effective_source_commit
    ):
        errors.append("source_commit_expected")
    rows_errors, public_rows = _public_row_errors(report_value.get("public_selected_rows"))
    errors.extend(rows_errors)
    if report_value.get("selected_row_count") != BUCKET_COUNT:
        errors.append("selected_row_count")
    selector = report_value.get("selector")
    if not isinstance(selector, Mapping):
        errors.append("selector_missing")
    else:
        if selector.get("selected_count") != BUCKET_COUNT:
            errors.append("selector_selected_count")
        if selector.get("selected_rows_digest") != _canonical_digest(public_rows):
            errors.append("selector_rows_digest")
        marker = report_value.get("marker", {})
        counts = Counter(row.get("status") for row in public_rows)
        for key, status in (
            ("candidate_count", "candidate"),
            ("no_candidate_count", "no_candidate"),
            ("fault_count", "fault"),
        ):
            if marker.get(key) != counts.get(status, 0) or selector.get(key) != counts.get(
                status, 0
            ):
                errors.append(f"marker_count:{status}")
        if selector.get("rejected_count") != counts.get("rejected", 0):
            errors.append("selector_count:rejected")
        if counts.get("candidate", 0) < 1:
            errors.append("candidate_threshold")
        if counts.get("fault", 0) > 0:
            errors.append("marker_fault_threshold")
        coverage = [
            {
                "bucket_index": index,
                "selected": True,
                "status": public_rows[index].get("status") if index < len(public_rows) else None,
            }
            for index in range(BUCKET_COUNT)
        ]
        if report_value.get("timing", {}).get("bucket_coverage_digest") != _canonical_digest(
            coverage
        ):
            errors.append("bucket_coverage_digest")
    failure = report_value.get("failure")
    if not isinstance(failure, Mapping):
        errors.append("failure_missing")
    else:
        codes = failure.get("codes")
        if not isinstance(codes, list) or len(codes) != len(set(codes)):
            errors.append("failure_codes")
        if not isinstance(codes, list) or failure.get("total_count") != len(codes):
            errors.append("failure_total_count")

    private_path = private_rows_path if private_rows_path is not None else restricted_rows_path
    private_rows: dict[int, Mapping[str, Any]] = {}
    if private_path is None:
        errors.append("private_rows_missing")
    else:
        try:
            raw_private, private_raw = _strict_jsonl(private_path)
            artifact = report_value.get("restricted_rows_artifact", {})
            if sha256_file(private_path) != artifact.get("sha256"):
                errors.append("private_rows_digest")
            if artifact.get("row_count") != len(raw_private):
                errors.append("private_rows_artifact_count")
            for index, row in enumerate(raw_private):
                errors.extend(
                    f"ledger_schema:{index}:{item}"
                    for item in _schema_errors(row, Path(ledger_schema_path))
                )
            private_errors, private_rows = _private_row_errors(raw_private, public_rows)
            errors.extend(private_errors)
        except Exception as exc:
            errors.append(f"private_rows_invalid:{type(exc).__name__}")
    selected_cas = private_cas_root if private_cas_root is not None else cas_root
    errors.extend(_public_ledger_errors(public_ledger_path, public_rows, Path(ledger_schema_path)))
    errors.extend(_accepted_ledger_errors(accepted_ledger_path, report_value, private_rows))
    errors.extend(
        _rerun_errors(
            private_rows,
            public_rows,
            selected_cas,
            metadata_only=metadata_only,
        )
    )
    zero_input = report_value.get("zero_input", {})
    privacy = report_value.get("privacy", {})
    for name, branch in (("zero_input", zero_input), ("privacy", privacy)):
        if not isinstance(branch, Mapping):
            errors.append(f"{name}_missing")
        elif branch.get("run_index") != 1 or branch.get("reused_from_b2") is not False:
            errors.append(f"{name}_run_specific")
        else:
            expected = (
                {
                    "scope": SCOPE,
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
                if name == "zero_input"
                else {
                    "scope": SCOPE,
                    "raw_artifacts_public": False,
                    "coordinates_present": False,
                    "absolute_paths_present": False,
                    "raw_bytes_present": False,
                    "device_original_id_present": False,
                    "private_artifacts_external_only": True,
                    "finding_count": 0,
                }
            )
            for key, value in expected.items():
                if branch.get(key) != value:
                    errors.append(f"{name}_{key}")
    errors = list(dict.fromkeys(errors))
    return errors


def verify_report(*args: Any, **kwargs: Any) -> list[str]:
    """Fail closed with structured errors for every malformed input shape."""

    try:
        return _verify_report_impl(*args, **kwargs)
    except Exception as exc:
        return [f"verifier_structure:{type(exc).__name__}"]


def verify_vc003_readonly_localization(*args: Any, **kwargs: Any) -> list[str]:
    return verify_report(*args, **kwargs)


verify_vc003_report = verify_vc003_readonly_localization
verify_localization_report = verify_vc003_readonly_localization
verify = verify_vc003_readonly_localization


def _parse_bindings(values: Iterable[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for raw in values:
        if "=" not in raw:
            raise VC003VerificationError("--binding expects NAME=PATH")
        name, value = raw.split("=", 1)
        if not name or name in result:
            raise VC003VerificationError("binding names must be unique")
        path = _lexical_safe_path(value)
        if not path.is_file():
            raise VC003VerificationError("binding path is not a regular file")
        result[name] = path
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--schema", type=Path, default=REPORT_SCHEMA_PATH)
    parser.add_argument("--ledger-schema", type=Path, default=LEDGER_SCHEMA_PATH)
    parser.add_argument("--private-cas-root", "--cas-root", dest="private_cas_root", type=Path)
    parser.add_argument("--private-rows", "--restricted-rows", dest="private_rows", type=Path)
    parser.add_argument(
        "--accepted-ledger",
        "--accepted-frame-ledger",
        dest="accepted_ledger",
        type=Path,
    )
    parser.add_argument("--public-ledger", dest="public_ledger", type=Path)
    parser.add_argument("--binding", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--lock", "--dependency-lock", dest="lock", type=Path)
    parser.add_argument(
        "--device-environment",
        "--device-env",
        dest="device_environment",
        type=Path,
    )
    parser.add_argument("--expected-source-commit", "--source-commit")
    parser.add_argument("--expected-wheel-sha256")
    parser.add_argument("--expected-lock-sha256")
    parser.add_argument("--expected-device-env-sha256")
    parser.add_argument("--allow-missing-bindings", action="store_true")
    parser.add_argument("--metadata-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        paths = _parse_bindings(args.binding)
        for key, value in (
            ("wheel", args.wheel),
            ("dependency_lock", args.lock),
            ("device_environment", args.device_environment),
        ):
            if value is not None:
                paths[key] = _lexical_safe_path(value)
        expected: dict[str, str] = {}
        for key, value in (
            ("wheel", args.expected_wheel_sha256),
            ("dependency_lock", args.expected_lock_sha256),
            ("device_environment", args.expected_device_env_sha256),
        ):
            if value is not None:
                expected[key] = _sha(value, f"--expected-{key}-sha256")
        errors = verify_report(
            args.report,
            config_path=args.config,
            schema_path=args.schema,
            ledger_schema_path=args.ledger_schema,
            private_cas_root=args.private_cas_root,
            private_rows_path=args.private_rows,
            accepted_ledger_path=args.accepted_ledger,
            public_ledger_path=args.public_ledger,
            binding_paths=paths,
            expected_bindings=expected,
            expected_source_commit=args.expected_source_commit,
            require_external_bindings=not args.allow_missing_bindings,
            metadata_only=args.metadata_only,
        )
        payload = {
            "execution_valid": not errors,
            "status": "PASS" if not errors else "FAIL",
            "error_count": len(errors),
            "errors": errors,
        }
        print(canonical_json(payload).decode("utf-8"))
        return 0 if not errors else 1
    except (OSError, TypeError, ValueError, VC003VerificationError) as exc:
        print(
            canonical_json(
                {
                    "execution_valid": False,
                    "status": "FAIL",
                    "error_count": 1,
                    "errors": [type(exc).__name__],
                }
            ).decode("utf-8")
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "VC003VerificationError",
    "load_strict_json",
    "main",
    "sha256_file",
    "verify",
    "verify_localization_report",
    "verify_report",
    "verify_vc003_readonly_localization",
    "verify_vc003_report",
]
