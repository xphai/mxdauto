from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

from tools.normalize_sdist import normalize_sdist


def _write_sdist(path: Path, *, gzip_mtime: int, member_mtime: int) -> None:
    with (
        path.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=gzip_mtime) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for name, payload in (("sample/a.txt", b"a"), ("sample/b.txt", b"b")):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.uid = member_mtime
            member.gid = member_mtime
            member.uname = "builder"
            member.gname = "builder"
            member.mtime = member_mtime
            archive.addfile(member, io.BytesIO(payload))


def test_normalized_sdist_is_byte_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    _write_sdist(first, gzip_mtime=1, member_mtime=2)
    _write_sdist(second, gzip_mtime=10, member_mtime=20)

    normalize_sdist(first, source_date_epoch=1_787_961_600)
    normalize_sdist(second, source_date_epoch=1_787_961_600)

    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )
    with tarfile.open(first, mode="r:gz") as archive:
        assert [member.name for member in archive.getmembers()] == [
            "sample/a.txt",
            "sample/b.txt",
        ]
        assert {member.mtime for member in archive.getmembers()} == {1_787_961_600}
