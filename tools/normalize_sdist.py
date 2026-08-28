"""Rewrite gzip-compressed source distributions into deterministic tar archives."""

from __future__ import annotations

import argparse
import copy
import gzip
import io
import os
import tarfile
import tempfile
from pathlib import Path


def normalize_sdist(path: Path, *, source_date_epoch: int) -> Path:
    """Normalize member order/identity/time and the gzip header in-place."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"Source distribution does not exist: {resolved}")
    if not resolved.name.endswith(".tar.gz"):
        raise ValueError(f"Expected a .tar.gz source distribution: {resolved}")
    if source_date_epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must be non-negative.")

    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(resolved, mode="r:gz") as source:
        for member in source.getmembers():
            stream = source.extractfile(member) if member.isfile() else None
            members.append((member, stream.read() if stream is not None else None))

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=resolved.parent,
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            with (
                gzip.GzipFile(
                    filename="",
                    mode="wb",
                    compresslevel=9,
                    fileobj=temporary,
                    mtime=source_date_epoch,
                ) as compressed,
                tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as output,
            ):
                for original, data in sorted(members, key=lambda item: item[0].name):
                    member = copy.copy(original)
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    member.mtime = source_date_epoch
                    member.pax_headers = {}
                    output.addfile(
                        member,
                        io.BytesIO(data) if data is not None else None,
                    )
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, resolved)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return resolved


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=int(os.environ.get("SOURCE_DATE_EPOCH", "0")),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    for path in args.paths:
        normalized = normalize_sdist(path, source_date_epoch=args.source_date_epoch)
        print(f"Normalized sdist: {normalized}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
