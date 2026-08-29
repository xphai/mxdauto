"""Build a privacy-safe frame-ingestion corpus from a private raw-BGR import plan."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from argparse import ArgumentParser
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
try:
    import maple_automation_core as _runtime_package
except ModuleNotFoundError:
    sys.path.insert(0, str(SRC))
    import maple_automation_core as _runtime_package

from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402

from maple_automation_core.capture.pixel_store import (  # noqa: E402
    PixelSpec,
    PixelStore,
    canonical_json,
)
from maple_automation_core.replay.frame_corpus import (  # noqa: E402
    FRAME_CORPUS_SCHEMA_VERSION,
    TRUTH_SCOPE,
    FrameCorpusError,
    canonical_digest,
    corpus_source_provenance_id,
    load_strict_json,
    public_privacy_summary,
    verify_corpus_file,
)


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FrameCorpusError(f"{field_name} must be an object")
    return cast(Mapping[str, Any], value)


def _array(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise FrameCorpusError(f"{field_name} must be an array")
    return value


def _exact(value: Mapping[str, Any], keys: set[str], field_name: str) -> None:
    if set(value) != keys:
        raise FrameCorpusError(
            f"{field_name} keys mismatch: expected {sorted(keys)!r}, got {sorted(value)!r}"
        )


def _path(value: object, field_name: str, *, private_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise FrameCorpusError(f"{field_name} must be a non-empty path")
    lexical_root = private_root.expanduser().absolute()
    lexical = Path(value).expanduser()
    if not lexical.is_absolute():
        lexical = lexical_root / lexical
    lexical = lexical.absolute()
    try:
        relative_parts = lexical.relative_to(lexical_root).parts
    except ValueError as exc:
        raise FrameCorpusError(f"{field_name} must remain inside private_root") from exc
    current = lexical_root
    for part in relative_parts:
        current = current / part
        if current.exists() or current.is_symlink():
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
            if current.is_symlink() or attributes & getattr(
                stat,
                "FILE_ATTRIBUTE_REPARSE_POINT",
                0x400,
            ):
                raise FrameCorpusError(f"{field_name} path contains a symlink/reparse point")
    path = lexical.resolve(strict=True)
    if not path.is_file():
        raise FrameCorpusError(f"{field_name} must resolve to a regular non-symlink file")
    return path


def _guard_separate_trees(output_root: Path, cas_root: Path) -> tuple[Path, Path]:
    public = output_root.expanduser().absolute().resolve(strict=False)
    private = cas_root.expanduser().absolute().resolve(strict=False)
    if public == private or public in private.parents or private in public.parents:
        raise FrameCorpusError("public output and private CAS trees must not overlap")
    return public, private


def _validate_schema(payload: dict[str, Any], schema_path: Path, field_name: str) -> None:
    schema = load_strict_json(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        detail = "; ".join(error.message for error in errors[:5])
        raise FrameCorpusError(f"{field_name} schema validation failed: {detail}")


def _write_canonical(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(payload) + b"\n")


def _build_corpus_into(
    plan_path: Path,
    output_root: Path,
    cas_root: Path,
    *,
    truth_schema: Path,
    manifest_schema: Path,
    source_commit: str | None = None,
    private_root: Path | None = None,
) -> Path:
    """Import raw byte fixtures and return the verified public manifest path."""

    plan = load_strict_json(plan_path)
    lexical_private_root = (
        plan_path.parent.absolute()
        if private_root is None
        else private_root.expanduser().absolute()
    )
    private_attributes = getattr(lexical_private_root.lstat(), "st_file_attributes", 0)
    if lexical_private_root.is_symlink() or private_attributes & getattr(
        stat,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        0x400,
    ):
        raise FrameCorpusError("private_root must not be a symlink/reparse point")
    resolved_private_root = lexical_private_root.resolve(strict=True)
    if not resolved_private_root.is_dir():
        raise FrameCorpusError("private_root must be an existing non-symlink directory")
    _exact(
        plan,
        {
            "schema_version",
            "corpus_id",
            "created_at",
            "source_commit",
            "sources",
            "sessions",
            "samples",
            "limitations",
        },
        "plan",
    )
    if plan["schema_version"] != FRAME_CORPUS_SCHEMA_VERSION:
        raise FrameCorpusError("plan schema_version mismatch")
    if not isinstance(plan["limitations"], list):
        raise FrameCorpusError("plan limitations must be an array")

    source_rows: list[dict[str, Any]] = []
    source_private: dict[str, tuple[Path, str]] = {}
    source_keys = {
        "source_id",
        "source_path",
        "expected_sha256",
        "locator_kind",
        "license_id",
        "privacy_class",
        "timing_truth",
    }
    for index, raw_source in enumerate(_array(plan["sources"], "sources")):
        source = _mapping(raw_source, f"sources[{index}]")
        _exact(source, source_keys, f"sources[{index}]")
        source_id = source["source_id"]
        if not isinstance(source_id, str) or not source_id or source_id in source_private:
            raise FrameCorpusError("source_id must be non-empty and unique")
        source_path = _path(
            source["source_path"],
            "source_path",
            private_root=resolved_private_root,
        )
        actual_sha = _sha256_file(source_path)
        if source["expected_sha256"] != actual_sha:
            raise FrameCorpusError(f"source artifact hash mismatch: {source_id}")
        source_private[source_id] = (source_path, actual_sha)
        source_rows.append(
            {
                "source_id": source_id,
                "artifact_sha256": actual_sha,
                "artifact_size": source_path.stat().st_size,
                "locator_kind": source["locator_kind"],
                "license_id": source["license_id"],
                "privacy_class": source["privacy_class"],
                "timing_truth": source["timing_truth"],
            }
        )

    session_keys = {"session_id", "source_id", "split", "independent"}
    session_private: dict[str, Mapping[str, Any]] = {}
    for index, raw_session in enumerate(_array(plan["sessions"], "sessions")):
        session = _mapping(raw_session, f"sessions[{index}]")
        _exact(session, session_keys, f"sessions[{index}]")
        session_id = session["session_id"]
        if not isinstance(session_id, str) or not session_id or session_id in session_private:
            raise FrameCorpusError("session_id must be non-empty and unique")
        if session["source_id"] not in source_private:
            raise FrameCorpusError("session references an unknown source")
        if session["split"] not in {"train", "validation", "test"}:
            raise FrameCorpusError("session split is unsupported")
        if type(session["independent"]) is not bool:
            raise FrameCorpusError("session independent must be boolean")
        session_private[session_id] = session

    sample_keys = {
        "sample_id",
        "truth_id",
        "session_id",
        "sequence",
        "raw_path",
        "pixel_spec",
        "expected_admission",
        "expected_status",
        "expected_reason_code",
        "category",
        "wrong_size_negative",
        "source_locator",
        "privacy_class",
        "retention_class",
        "transform_version",
        "calibration_sha256",
        "primary_reviewer_id",
        "independent_reviewer_id",
        "independent_decision",
        "adjudication_id",
    }
    store = PixelStore(cas_root)
    output_root.mkdir(parents=True, exist_ok=True)
    sample_rows: list[dict[str, Any]] = []
    sample_ids: set[str] = set()
    truth_ids: set[str] = set()
    session_counts = {session_id: 0 for session_id in session_private}
    independent_reviewed = 0
    truth_records: list[dict[str, Any]] = []
    for index, raw_sample in enumerate(_array(plan["samples"], "samples")):
        sample = _mapping(raw_sample, f"samples[{index}]")
        _exact(sample, sample_keys, f"samples[{index}]")
        sample_id = sample["sample_id"]
        truth_id = sample["truth_id"]
        if not isinstance(sample_id, str) or not sample_id or sample_id in sample_ids:
            raise FrameCorpusError("sample_id must be non-empty and unique")
        if not isinstance(truth_id, str) or not truth_id or truth_id in truth_ids:
            raise FrameCorpusError("truth_id must be non-empty and unique")
        sample_ids.add(sample_id)
        truth_ids.add(truth_id)
        session_id = sample["session_id"]
        if session_id not in session_private:
            raise FrameCorpusError("sample references an unknown session")
        session = session_private[session_id]
        source_id = cast(str, session["source_id"])
        raw_path = _path(
            sample["raw_path"],
            "raw_path",
            private_root=resolved_private_root,
        )
        raw_bytes = raw_path.read_bytes()
        try:
            spec = PixelSpec.from_dict(_mapping(sample["pixel_spec"], "pixel_spec"))
        except (TypeError, ValueError) as exc:
            raise FrameCorpusError("sample pixel_spec failed Pixel V1 validation") from exc
        if len(raw_bytes) != spec.length:
            raise FrameCorpusError("raw sample length does not match PixelSpec")
        sequence = sample["sequence"]
        if type(sequence) is not int or sequence < 0:
            raise FrameCorpusError("sample sequence must be a non-negative integer")
        source_provenance_id = corpus_source_provenance_id(
            source_id=source_id,
            session_id=cast(str, session_id),
            source_artifact_sha256=source_private[source_id][1],
        )
        pixel_artifact = store.put_artifact(
            spec,
            raw_bytes,
            privacy_class=sample["privacy_class"],
            retention_class=sample["retention_class"],
            source_provenance_id=source_provenance_id,
            session_id=cast(str, session_id),
            source_sequence=sequence,
        )
        source_locator = _mapping(sample["source_locator"], "source_locator")
        review = {
            "primary_reviewer_id": sample["primary_reviewer_id"],
            "primary_decision": "confirmed",
            "independent_reviewer_id": sample["independent_reviewer_id"],
            "independent_decision": sample["independent_decision"],
            "adjudication_id": sample["adjudication_id"],
        }
        truth: dict[str, Any] = {
            "schema_version": FRAME_CORPUS_SCHEMA_VERSION,
            "truth_id": truth_id,
            "truth_scope": TRUTH_SCOPE,
            "sample_id": sample_id,
            "source_id": source_id,
            "session_id": session_id,
            "sequence": sequence,
            "source_locator": dict(source_locator),
            "pixel_spec": spec.to_dict(),
            "pixel_digest": pixel_artifact.pixel_digest,
            "pixel_artifact_sha256": pixel_artifact.artifact_sha256,
            "source_provenance_id": source_provenance_id,
            "cas_ref": pixel_artifact.ref,
            "expected_admission": sample["expected_admission"],
            "expected_status": sample["expected_status"],
            "expected_reason_code": sample["expected_reason_code"],
            "category": sample["category"],
            "wrong_size_negative": sample["wrong_size_negative"],
            "derivation": {
                "source_artifact_sha256": source_private[source_id][1],
                "extraction_artifact_sha256": _sha256_file(raw_path),
                "extraction_tool_sha256": _sha256_file(Path(__file__).resolve()),
                "extraction_tool_version": "import-frame-corpus-v1",
                "parent_pixel_digest": None,
                "transform_version": sample["transform_version"],
                "calibration_sha256": sample["calibration_sha256"],
                "redaction_mode": "not_applicable_hash_only",
                "redaction_artifact_sha256": None,
                "deidentified_derivative_sha256": None,
            },
            "privacy": {
                "class": sample["privacy_class"],
                "retention": sample["retention_class"],
                "license_id": next(
                    row["license_id"] for row in source_rows if row["source_id"] == source_id
                ),
            },
            "review": review,
        }
        truth["record_digest"] = canonical_digest(truth)
        _validate_schema(truth, truth_schema, "truth")
        truth_records.append(truth)
        truth_relative = Path("truths") / f"{truth_id}.json"
        truth_path = output_root / truth_relative
        _write_canonical(truth_path, truth)
        sample_rows.append(
            {
                "sample_id": sample_id,
                "truth_id": truth_id,
                "truth_path": truth_relative.as_posix(),
                "truth_sha256": _sha256_file(truth_path),
                "session_id": session_id,
                "sequence": sequence,
                "pixel_digest": pixel_artifact.pixel_digest,
                "pixel_artifact_sha256": pixel_artifact.artifact_sha256,
                "source_provenance_id": source_provenance_id,
                "cas_ref": pixel_artifact.ref,
                "category": sample["category"],
                "wrong_size_negative": sample["wrong_size_negative"],
            }
        )
        session_counts[cast(str, session_id)] += 1
        if sample["independent_reviewer_id"] is not None:
            independent_reviewed += 1

    session_rows = [
        {
            "session_id": session_id,
            "source_id": session["source_id"],
            "split": session["split"],
            "independent": session["independent"],
            "sample_count": session_counts[session_id],
        }
        for session_id, session in session_private.items()
    ]
    if any(row["sample_count"] == 0 for row in session_rows):
        raise FrameCorpusError("every declared session must contribute at least one sample")
    splits = {
        split: [row["session_id"] for row in session_rows if row["split"] == split]
        for split in ("train", "validation", "test")
    }
    sample_count = len(sample_rows)
    if sample_count == 0:
        raise FrameCorpusError("the import plan must contain at least one sample")
    effective_source_commit = plan["source_commit"] if source_commit is None else source_commit
    if (
        not isinstance(effective_source_commit, str)
        or len(effective_source_commit) != 40
        or any(character not in "0123456789abcdef" for character in effective_source_commit)
    ):
        raise FrameCorpusError("source_commit must be 40 lowercase hexadecimal characters")
    manifest: dict[str, Any] = {
        "schema_version": FRAME_CORPUS_SCHEMA_VERSION,
        "corpus_id": plan["corpus_id"],
        "truth_scope": TRUTH_SCOPE,
        "created_at": plan["created_at"],
        "source_commit": effective_source_commit,
        "sources": source_rows,
        "sessions": session_rows,
        "samples": sample_rows,
        "splits": splits,
        "review_summary": {
            "primary_reviewed": sample_count,
            "independent_reviewed": independent_reviewed,
            "independent_fraction_ppm": independent_reviewed * 1_000_000 // sample_count,
        },
        "limitations": plan["limitations"],
    }
    manifest["privacy_summary"] = public_privacy_summary(manifest, truth_records)
    if manifest["privacy_summary"]["pii_findings"] != 0:
        raise FrameCorpusError("public corpus privacy scan found host-path or PII-like values")
    manifest["corpus_digest"] = canonical_digest(manifest)
    _validate_schema(manifest, manifest_schema, "manifest")
    manifest_path = output_root / "frame-corpus-manifest.json"
    _write_canonical(manifest_path, manifest)
    verify_corpus_file(manifest_path, truth_root=output_root, cas_root=cas_root)
    return manifest_path


def build_corpus(
    plan_path: Path,
    output_root: Path,
    cas_root: Path,
    *,
    truth_schema: Path,
    manifest_schema: Path,
    source_commit: str | None = None,
    private_root: Path | None = None,
) -> Path:
    """Transactionally publish a scanned JSON tree separate from the private CAS."""

    final_output, private_cas = _guard_separate_trees(output_root, cas_root)
    if final_output.exists() or final_output.is_symlink():
        raise FrameCorpusError("public output root must not already exist")
    final_output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{final_output.name}.staging-",
            dir=final_output.parent,
        )
    )
    try:
        staged_manifest = _build_corpus_into(
            plan_path,
            staging,
            private_cas,
            truth_schema=truth_schema,
            manifest_schema=manifest_schema,
            source_commit=source_commit,
            private_root=private_root,
        )
        relative_manifest = staged_manifest.relative_to(staging)
        os.replace(staging, final_output)
        return final_output / relative_manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _parse_args(argv: list[str] | None = None) -> Any:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--cas-root", type=Path, required=True)
    parser.add_argument(
        "--private-root",
        type=Path,
        help="Base directory for relative private source/raw paths (defaults to plan directory).",
    )
    parser.add_argument(
        "--truth-schema",
        type=Path,
        default=ROOT / "schemas" / "frame-truth.schema.json",
    )
    parser.add_argument(
        "--manifest-schema",
        type=Path,
        default=ROOT / "schemas" / "frame-corpus-manifest.schema.json",
    )
    parser.add_argument("--require-installed", action="store_true")
    parser.add_argument(
        "--source-commit",
        help="Override the private plan commit with the exact checkout commit being evidenced.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    package_path = Path(_runtime_package.__file__).resolve()
    if args.require_installed and package_path.is_relative_to(ROOT):
        raise RuntimeError(f"runtime package resolved from checkout: {package_path}")
    manifest = build_corpus(
        args.plan.resolve(strict=True),
        args.output_root.resolve(strict=False),
        args.cas_root.resolve(strict=False),
        truth_schema=args.truth_schema.resolve(strict=True),
        manifest_schema=args.manifest_schema.resolve(strict=True),
        source_commit=args.source_commit,
        private_root=args.private_root,
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FrameCorpusError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
