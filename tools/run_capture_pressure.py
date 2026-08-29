"""Run the offline deterministic raw-latest-slot pressure report.

Examples::

    python tools/run_capture_pressure.py
    python tools/run_capture_pressure.py --publish-take-operations 512 \
        --lifecycle-races 16 --report out/capture-pressure.json

The second form is a small smoke run.  The default scale is the B1 evidence
scale (100,000 publish operations and 1,000 lifecycle races).
"""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
REQUIRE_INSTALLED = "--require-installed" in sys.argv
if not REQUIRE_INSTALLED and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import maple_automation_core.capture.stress as stress_module  # noqa: E402
from maple_automation_core.capture.stress import (  # noqa: E402
    CapturePressureConfig,
    CapturePressureError,
    CapturePressureReport,
    run_capture_pressure,
    verify_capture_pressure_report,
)


def _default_schema_path() -> Path:
    return ROOT / "schemas" / "capture-pressure-report.schema.json"


def _parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish-take-operations",
        "--publish-operations",
        dest="publish_take_operations",
        type=int,
        default=100_000,
        help="Number of lightweight publish operations (small values are useful for CI smoke).",
    )
    parser.add_argument(
        "--lifecycle-races",
        "--races",
        dest="lifecycle_races",
        type=int,
        default=1_000,
        help="Number of deterministic lifecycle/reset/stop races.",
    )
    parser.add_argument(
        "--runs",
        "--repetitions",
        dest="repetitions",
        type=int,
        default=3,
        help="Deterministic repetitions; at least three are required.",
    )
    parser.add_argument("--seed", type=int, default=0xC0FFEE)
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=2.0,
        help="Per-operation race timeout in seconds.",
    )
    parser.add_argument(
        "--require-minimums",
        action="store_true",
        help="Return FAIL for a small run instead of recording it as SMOKE coverage.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Write the strict JSON report to this path in addition to stdout.",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=_default_schema_path(),
        help="JSON Schema used for report shape validation.",
    )
    parser.add_argument(
        "--require-installed",
        action="store_true",
        help="Require the installed package and reject checkout/src imports (clean-wheel CI).",
    )
    parser.add_argument(
        "--source-commit",
        help="Override the source commit binding with a 40-character SHA-1.",
    )
    return parser.parse_args(argv)


def _verify_import_origin(require_installed: bool) -> None:
    if not require_installed:
        return
    module_file = getattr(stress_module, "__file__", None)
    if not isinstance(module_file, str):
        raise CapturePressureError("installed capture stress module has no file origin")
    try:
        Path(module_file).resolve().relative_to(ROOT.resolve())
    except ValueError:
        return
    raise CapturePressureError("--require-installed resolved capture stress from checkout")


def _validate_schema(payload: dict[str, Any], schema_path: Path) -> None:
    """Validate shape when jsonschema is installed, then always verify semantics."""

    try:
        schema = json.loads(schema_path.resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapturePressureError(f"could not read report schema: {schema_path}") from exc
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:
        # The package has no runtime dependency on jsonschema.  The semantic
        # verifier below still checks all report fields when the optional
        # validator is absent.
        schema = schema
    else:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
        if errors:
            formatted = "; ".join(error.message for error in errors[:5])
            raise CapturePressureError(f"capture pressure schema validation failed: {formatted}")
    verify_capture_pressure_report(payload, repo_root=ROOT)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _verify_import_origin(args.require_installed)
    config = CapturePressureConfig(
        publish_take_operations=args.publish_take_operations,
        lifecycle_races=args.lifecycle_races,
        repetitions=args.repetitions,
        seed=args.seed,
        timeout_s=args.timeout_s,
        enforce_minimums=args.require_minimums,
    )
    report: CapturePressureReport = run_capture_pressure(
        config,
        source_commit=args.source_commit,
        repo_root=ROOT,
    )
    payload = report.to_dict()
    _validate_schema(payload, args.schema.resolve())
    if args.report is not None:
        report.write_json(args.report.resolve())
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CapturePressureError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
