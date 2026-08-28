from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
from pathlib import Path

PINNED_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)==(?P<version>[^\s#]+)$")


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> dict[str, str]:
    """Read an exact-version lock file and return canonical package names to versions."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Could not read dependency lock {path}: {exc}") from exc
    packages: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = PINNED_REQUIREMENT.fullmatch(line)
        if match is None:
            raise ValueError(
                f"{path}:{line_number}: expected an exact 'package==version' requirement"
            )
        name = _canonical_name(match.group("name"))
        if name in packages:
            raise ValueError(f"{path}:{line_number}: duplicate package {name}")
        packages[name] = match.group("version")
    if not packages:
        raise ValueError(f"Dependency lock is empty: {path}")
    return packages


def verify_installed(packages: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for name, expected in sorted(packages.items()):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"{name}: package is not installed (expected {expected})")
            continue
        if actual != expected:
            errors.append(f"{name}: expected {expected}, found {actual}")
    return errors


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and optionally audit an exact dependency lock."
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "configs" / "requirements.lock",
        help="Exact dependency lock file.",
    )
    parser.add_argument(
        "--check-installed",
        action="store_true",
        help="Compare every locked version with the active Python environment.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        packages = parse_lock(args.lock)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    errors = verify_installed(packages) if args.check_installed else []
    if errors:
        print(f"Dependency lock verification failed with {len(errors)} error(s):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    suffix = " and installed versions" if args.check_installed else " syntax"
    print(f"Dependency lock verified ({len(packages)} packages;{suffix}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
