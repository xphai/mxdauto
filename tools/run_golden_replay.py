"""Run the minimal deterministic Golden Replay smoke and emit its report."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
try:
    import maple_automation_core as _runtime_package
except ModuleNotFoundError:  # local source checkout invocation
    sys.path.insert(0, str(SRC))
    import maple_automation_core as _runtime_package

from report_binding import (  # noqa: E402
    bind_report_to_manifest,
    canonical_report_digest,
    sha256_file,
    write_report,
)

from maple_automation_core.replay import GoldenReplayRunner  # noqa: E402


def _default_fixture_path() -> Path:
    return ROOT / "fixtures" / "golden" / "pilot_minimal_v1.json"


def _parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=_default_fixture_path(),
        help="Golden fixture JSON path.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of deterministic replay repetitions (default: 3).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional path to write the machine-readable JSON report.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Candidate runtime manifest used to bind this replay result.",
    )
    parser.add_argument(
        "--require-installed",
        action="store_true",
        help="Require the runtime package to resolve outside this source checkout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    package_path = Path(_runtime_package.__file__).resolve()
    if args.require_installed and package_path.is_relative_to(ROOT):
        raise RuntimeError(
            f"Runtime package resolved from checkout instead of wheel: {package_path}"
        )
    report = GoldenReplayRunner(args.fixture).run_repeated(args.runs)
    payload = report.to_dict()
    payload["fixture_file_sha256"] = sha256_file(args.fixture.resolve())
    payload["report_digest"] = canonical_report_digest(payload)
    if args.manifest is not None:
        payload = bind_report_to_manifest(
            payload,
            manifest_path=args.manifest,
            repo_root=ROOT,
            report_kind="replay",
        )
    if args.report is not None:
        write_report(args.report, payload)
    print(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))
    return 0 if report.deterministic else 1


if __name__ == "__main__":
    raise SystemExit(main())
