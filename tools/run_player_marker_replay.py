"""Run the deterministic G1-LOC-003B player-marker replay.

The command is intentionally a read-only composition of the verified replay
inputs.  It loads the frozen marker policy, wraps :class:`PixelStore` with a
reader-only surface, runs the package's actual ``MinimapMarkerExtractor``
three times, verifies the resulting public report, and writes that report only
when an explicit output path is supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
# A globally installed wheel may predate the LOC-003B modules.  Prefer this
# checkout's source tree so the replay hashes and executes the requested code.
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import maple_automation_core.localization.minimap_marker as _marker_module  # noqa: E402
from maple_automation_core.capture.pixel_store import (  # noqa: E402
    PixelSpec,
    PixelStore,
    canonical_json,
)
from maple_automation_core.localization.minimap_marker import (  # noqa: E402
    MinimapMarkerConfig,
    MinimapMarkerExtractor,
)
from maple_automation_core.replay.frame_corpus import load_strict_json  # noqa: E402
from maple_automation_core.replay.player_marker import (  # noqa: E402
    PlayerMarkerReplayError,
    PlayerMarkerReplayRunner,
    verify_player_marker_replay_report,
)


class ReadOnlyPixelStore:
    """Reader-only view of a :class:`PixelStore` for the marker extractor.

    Keeping the backing store private is deliberate: the B2 replay boundary
    accepts a store with ``read`` only, so the detector cannot accidentally
    publish or mutate CAS objects.
    """

    __slots__ = ("_store",)

    def __init__(self, root: str | Path | PixelStore) -> None:
        self._store = root if isinstance(root, PixelStore) else PixelStore(root)

    def read(self, digest: str, spec: PixelSpec | None = None) -> bytes:
        return self._store.read(digest, spec)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"could not resolve git HEAD for replay root: {root}") from exc
    commit = completed.stdout.strip()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("git HEAD is not a lowercase 40-character commit")
    return commit


def _validate_checkout(replay_source_commit: str, *, allow_dirty: bool) -> str:
    head = _git_head(ROOT)
    if replay_source_commit != head:
        raise ValueError("--replay-source-commit must equal the current git HEAD")
    if allow_dirty:
        return head
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain=v1", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("could not inspect tracked checkout state") from exc
    if completed.stdout.strip():
        raise ValueError("tracked checkout is dirty; pass --allow-dirty only for tests")
    return head


def _require_file(value: Path, field_name: str) -> Path:
    path = value.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{field_name} must be an existing regular file: {path}")
    return path


def _require_directory(value: Path, field_name: str) -> Path:
    path = value.expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{field_name} must be an existing directory: {path}")
    return path


def _read_mapping(path: Path, field_name: str) -> Mapping[str, Any]:
    try:
        payload = load_strict_json(path)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"could not read {field_name}: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field_name} must be a JSON object: {path}")
    return payload


def _default_marker_source() -> Path:
    source = inspect.getsourcefile(MinimapMarkerExtractor)
    if source is not None:
        return Path(source).resolve()
    module_file = getattr(_marker_module, "__file__", None)
    if not isinstance(module_file, str):
        raise ValueError("could not resolve the marker extractor source file")
    return Path(module_file).resolve()


def _marker_source_path(value: Path | None) -> Path:
    actual = _require_file(_default_marker_source(), "marker source")
    if value is None:
        return actual
    supplied = _require_file(value, "marker source")
    if supplied != actual:
        raise ValueError("--marker-source must name the imported marker source")
    return supplied


def _event_sort_key(path: Path) -> tuple[str, str]:
    rendered = path.as_posix()
    return rendered.casefold(), rendered


def _event_paths(values: Sequence[Path], event_root: Path | None) -> tuple[Path, ...]:
    """Expand optional directories and return a stable, duplicate-free list."""

    candidates: list[Path] = list(values)
    if event_root is not None:
        candidates.extend(event_root.glob("*.jsonl"))

    expanded: list[Path] = []
    for value in candidates:
        path = value.expanduser().resolve()
        if path.is_dir():
            expanded.extend(path.glob("*.jsonl"))
        else:
            expanded.append(path)

    paths = sorted((path.resolve() for path in expanded), key=_event_sort_key)
    if not paths:
        raise ValueError("at least one Event Tape path is required")
    if any(not path.is_file() for path in paths):
        missing = next(path for path in paths if not path.is_file())
        raise ValueError(f"event tape must be an existing regular file: {missing}")
    if len(set(paths)) != len(paths):
        raise ValueError("event tape paths must be unique")
    return tuple(paths)


def _zero(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be 0") from exc
    if parsed != 0:
        raise argparse.ArgumentTypeError("value is fixed at 0 for LOC-003B replay")
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", required=True, type=Path, help="Verified corpus manifest."
    )
    parser.add_argument(
        "--truth-root", required=True, type=Path, help="Private truth-artifact root."
    )
    parser.add_argument(
        "--private-cas-root",
        "--cas-root",
        dest="private_cas_root",
        required=True,
        type=Path,
        help="Private PixelStore/CAS root (read-only).",
    )
    parser.add_argument(
        "--event-tape",
        "--event-tape-path",
        "--event-tapes",
        dest="event_tapes",
        action="append",
        type=Path,
        default=[],
        help="Event Tape path; repeat for each tape, or pass a tape directory.",
    )
    parser.add_argument(
        "--event-tapes-root",
        type=Path,
        help="Directory from which all *.jsonl Event Tapes are loaded.",
    )
    parser.add_argument(
        "--accepted-ledger",
        "--accepted-frame-ledger",
        dest="accepted_ledger",
        required=True,
        type=Path,
        help="Accepted-frame ledger JSONL.",
    )
    parser.add_argument(
        "--calibration", required=True, type=Path, help="Calibration artifact JSON."
    )
    parser.add_argument(
        "--zero-input-audit",
        dest="zero_input_audit",
        required=True,
        type=Path,
        help="Passing zero-input audit artifact JSON.",
    )
    parser.add_argument(
        "--marker-config",
        "--config",
        dest="marker_config",
        required=True,
        type=Path,
        help="Frozen minimap marker configuration JSON.",
    )
    parser.add_argument(
        "--marker-source",
        type=Path,
        help="Marker source file to hash (default: imported MinimapMarkerExtractor source).",
    )
    parser.add_argument(
        "--replay-source-commit",
        required=True,
        help="40-character commit for the replay implementation.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Skip the tracked-checkout cleanliness check for tests only.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional output report path; omitted means no filesystem output.",
    )
    parser.add_argument(
        "--as-of-offset-ns",
        "--as-of-ns",
        dest="as_of_offset_ns",
        type=_zero,
        default=0,
        help="Replay timing offset; fixed at 0 for this gate.",
    )
    parser.add_argument(
        "--generation",
        type=_zero,
        default=0,
        help="Candidate generation; fixed at 0 for this gate.",
    )
    return parser.parse_args(argv)


def _write_atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write canonical UTF-8 JSON with exactly one trailing LF atomically."""

    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json(payload) + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("replay report has no runs")
    samples = runs[0].get("samples") if isinstance(runs[0], Mapping) else None
    if not isinstance(samples, list):
        raise ValueError("replay report has no samples")
    counts = Counter(
        str(sample.get("status"))
        for sample in samples
        if isinstance(sample, Mapping)
    )
    return {
        "detected": counts.get("detected", 0),
        "fault": counts.get("fault", 0),
        "no_marker": counts.get("no_marker", 0),
        "rejected": counts.get("rejected", 0),
        "sample_count": payload.get("sample_count", len(samples)),
        "repeat_count": payload.get("repeat_count", len(runs)),
        "status": payload.get("status"),
        "extractor_artifact_digest": payload.get("extractor_artifact_digest"),
        "zero_input_audit_artifact_digest": payload.get("zero_input_audit_artifact_digest"),
        "report_digest": payload.get("report_digest"),
    }


def _verify_report(
    payload: Mapping[str, Any],
    *,
    runner: PlayerMarkerReplayRunner,
    marker_config: MinimapMarkerConfig,
    replay_source_commit: str,
    as_of_offset_ns: int,
    extractor_artifact_digest: str,
    accepted_ledger_digest: str,
    calibration_artifact_digest: str,
    zero_input_audit_artifact_digest: str,
) -> None:
    """Call the verifier with every independent B2 expectation it exposes.

    The signature filter keeps this CLI usable with the pre-strengthening
    replay package while passing the full expectation set to the hardened
    verifier used by the gate.
    """

    report = payload
    expected: dict[str, Any] = {
        "source_commit": runner.corpus_source_commit,
        "corpus_source_commit": runner.corpus_source_commit,
        "replay_source_commit": replay_source_commit,
        "verification_profile": "b2_gate",
        "generation": 0,
        "manifest": runner.config.manifest,
        "manifest_digest": report.get("manifest_digest"),
        "config_digest": marker_config.digest,
        "extractor_artifact_digest": extractor_artifact_digest,
        "event_tape_digest": report.get("event_tape_digest"),
        "accepted_ledger_digest": accepted_ledger_digest,
        "calibration_artifact_digest": calibration_artifact_digest,
        "zero_input_audit_artifact_digest": zero_input_audit_artifact_digest,
        "as_of_ns": 0,
        "as_of_offset_ns": as_of_offset_ns,
        "sample_order": runner.sample_order,
    }
    # The hardened B2 verifier names its independent expectations explicitly.
    # Keep the legacy names above for older package revisions while selecting
    # only parameters actually exposed by the imported verifier.
    expected.update(
        {
            "expected_verification_profile": "b2_gate",
            "expected_extractor_artifact_digest": extractor_artifact_digest,
            "expected_event_tape_digest": report.get("event_tape_digest"),
            "expected_accepted_ledger_digest": accepted_ledger_digest,
            "expected_calibration_artifact_digest": calibration_artifact_digest,
            "expected_zero_input_audit_artifact_digest": zero_input_audit_artifact_digest,
            "expected_generation": 0,
        }
    )
    parameters = inspect.signature(verify_player_marker_replay_report).parameters
    accepts_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs = expected if accepts_var_kwargs else {
        name: value for name, value in expected.items() if name in parameters
    }
    verify_player_marker_replay_report(payload, **kwargs)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _validate_checkout(args.replay_source_commit, allow_dirty=args.allow_dirty)
    manifest_path = _require_file(args.manifest, "manifest")
    truth_root = _require_directory(args.truth_root, "truth root")
    cas_root = _require_directory(args.private_cas_root, "private CAS root")
    accepted_ledger = _require_file(args.accepted_ledger, "accepted-frame ledger")
    calibration = _require_file(args.calibration, "calibration")
    zero_input_audit = _require_file(args.zero_input_audit, "zero-input audit")
    marker_config_path = _require_file(args.marker_config, "marker config")
    marker_source = _marker_source_path(args.marker_source)
    event_root = (
        _require_directory(args.event_tapes_root, "Event Tape root")
        if args.event_tapes_root is not None
        else None
    )
    event_tapes = _event_paths(args.event_tapes, event_root)

    input_paths = {
        manifest_path,
        accepted_ledger,
        calibration,
        zero_input_audit,
        marker_config_path,
        marker_source,
        *event_tapes,
    }
    report_path = None if args.report is None else args.report.expanduser().resolve()
    if report_path is not None and report_path in input_paths:
        raise ValueError("report path must be distinct from every replay input")

    marker_data = _read_mapping(marker_config_path, "marker config")
    marker_config = MinimapMarkerConfig.from_dict(marker_data)
    extractor_artifact_digest = _sha256_file(marker_source)
    accepted_ledger_digest = _sha256_file(accepted_ledger)
    calibration_artifact_digest = _sha256_file(calibration)
    zero_input_audit_sha256 = _sha256_file(zero_input_audit)

    # The wrapper intentionally has no write/put/store aliases.  The actual
    # detector consumes the same frozen config that is bound into the report.
    pixel_store = ReadOnlyPixelStore(cas_root)
    extractor = MinimapMarkerExtractor(config=marker_config, pixel_store=pixel_store)
    runner = PlayerMarkerReplayRunner(
        manifest_path,
        verification_profile="b2_gate",
        truth_root=truth_root,
        cas_root=cas_root,
        event_tapes=event_tapes,
        extractor=extractor,
        replay_source_commit=args.replay_source_commit,
        config=marker_config.to_dict(),
        config_digest=marker_config.digest,
        extractor_artifact_digest=extractor_artifact_digest,
        accepted_frame_ledger=accepted_ledger,
        calibration=calibration,
        max_age_ns=marker_config.max_age_ns,
        zero_input_audit=zero_input_audit,
        zero_input_audit_artifact_sha256=zero_input_audit_sha256,
        as_of_ns=0,
        as_of_offset_ns=args.as_of_offset_ns,
        generation=args.generation,
    )
    report = runner.run_three_times()
    payload = report.to_dict()
    _verify_report(
        payload,
        runner=runner,
        marker_config=marker_config,
        replay_source_commit=args.replay_source_commit,
        as_of_offset_ns=args.as_of_offset_ns,
        extractor_artifact_digest=extractor_artifact_digest,
        accepted_ledger_digest=accepted_ledger_digest,
        calibration_artifact_digest=calibration_artifact_digest,
        zero_input_audit_artifact_digest=zero_input_audit_sha256,
    )
    if report_path is not None:
        _write_atomic_json(report_path, payload)
    print(json.dumps(_summary(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, PlayerMarkerReplayError, TypeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


__all__ = ["ReadOnlyPixelStore", "main"]
