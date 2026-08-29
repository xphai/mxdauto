"""Run deterministic G1 Frame Admission evidence against the current checkout."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
try:
    import maple_automation_core as _runtime_package
except ModuleNotFoundError:  # local source checkout invocation
    sys.path.insert(0, str(SRC))
    import maple_automation_core as _runtime_package

from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402

from maple_automation_core.replay.frame_admission import (  # noqa: E402
    FrameAdmissionFixture,
    FrameAdmissionReplayError,
    FrameAdmissionReplayRunner,
    verify_frame_admission_report,
)


def _default_fixture_path() -> Path:
    return ROOT / "fixtures" / "g1" / "frame_admission_v1.json"


def _default_schema_path() -> Path:
    return ROOT / "schemas" / "frame-admission-report.schema.json"


def _parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=_default_fixture_path())
    parser.add_argument("--schema", type=Path, default=_default_schema_path())
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--report", type=Path, help="Write the machine-readable report here.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=ROOT,
        help="Repository root used to resolve and bind git HEAD.",
    )
    parser.add_argument(
        "--require-installed",
        action="store_true",
        help="Require the runtime package to resolve outside this source checkout.",
    )
    return parser.parse_args(argv)


def _validate_report(
    payload: dict[str, Any],
    schema_path: Path,
    fixture: FrameAdmissionFixture,
) -> None:
    try:
        schema = json.loads(schema_path.resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrameAdmissionReplayError(f"could not read report schema: {schema_path}") from exc
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        formatted = "; ".join(error.message for error in errors[:5])
        raise FrameAdmissionReplayError(
            f"Frame Admission report schema validation failed: {formatted}"
        )
    verify_frame_admission_report(payload, fixture)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    package_path = Path(_runtime_package.__file__).resolve()
    if args.require_installed and package_path.is_relative_to(ROOT):
        raise RuntimeError(
            f"Runtime package resolved from checkout instead of wheel: {package_path}"
        )
    runner = FrameAdmissionReplayRunner(
        args.fixture.resolve(),
        repo_root=args.repo_root.resolve(),
    )
    report = runner.run_repeated(args.runs)
    payload = report.to_dict()
    _validate_report(payload, args.schema.resolve(), runner.fixture)
    if args.report is not None:
        report.write_json(args.report.resolve())
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FrameAdmissionReplayError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
