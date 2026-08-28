from __future__ import annotations

import math
from pathlib import Path

import pytest

from tools.bundle_common import read_json, safe_relative_path, write_json


@pytest.mark.parametrize(
    "value",
    [
        "folder/file:stream",
        "folder//file.json",
        "folder/NUL.txt",
        "folder/COM1",
        "folder/trailing. ",
    ],
)
def test_safe_relative_path_rejects_windows_ambiguous_paths(value: str) -> None:
    with pytest.raises(ValueError):
        safe_relative_path(value)


@pytest.mark.parametrize(
    "payload",
    ['{"key": 1, "key": 2}', '{"value": NaN}', '{"value": Infinity}'],
)
def test_read_json_rejects_ambiguous_or_nonstandard_json(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "payload.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        read_json(path)


def test_write_json_rejects_nan_without_replacing_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "payload.json"
    path.write_text('{"state":"original"}\n', encoding="utf-8")

    with pytest.raises(ValueError):
        write_json(path, {"value": math.nan})

    assert path.read_text(encoding="utf-8") == '{"state":"original"}\n'
