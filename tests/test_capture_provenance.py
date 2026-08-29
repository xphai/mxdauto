from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from maple_automation_core.capture.pixel_store import (
    CaptureSourceProvenance,
    PixelSpec,
    hash_physical_device_fingerprint,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_capture_provenance_is_deidentified_and_zero_input() -> None:
    requested = {"width": 1920, "height": 1080, "fps": 30.0, "fourcc": "MJPG", "backend": "dshow"}
    negotiated = {"width": 1920, "height": 1080, "fps": 30.0, "fourcc": "BGR3", "backend": "dshow"}
    provenance = CaptureSourceProvenance(
        source_id="capture-card-primary",
        requested=requested,
        negotiated=negotiated,
        backend="dshow",
        timestamp_origin="host_monotonic_post_retrieve",
        physical_device_fingerprint="USB\\VID_0000&PID_0000#serial",
    )
    payload = provenance.to_dict()
    assert "USB\\VID" not in json.dumps(payload)
    assert payload["physical_device_fingerprint_sha256"] == hash_physical_device_fingerprint(
        "USB\\VID_0000&PID_0000#serial"
    )
    assert payload["upstream_queue"] == payload["upstream_queue_depth"] == "unknown"
    assert payload["input_owner"] == "legacy"
    assert payload["real_input_enabled"] is False
    assert payload["real_input_call_count"] == 0
    assert CaptureSourceProvenance.from_json(provenance.to_json()) == provenance


def test_capture_provenance_missing_queue_alias_is_stable_value_error() -> None:
    provenance = CaptureSourceProvenance()
    payload = provenance.to_dict()
    del payload["upstream_queue_depth"]
    with pytest.raises(ValueError, match="missing key: upstream_queue_depth"):
        CaptureSourceProvenance.from_dict(payload)


def test_capture_provenance_schema_accepts_pixel_spec_formats() -> None:
    provenance = CaptureSourceProvenance(
        requested=PixelSpec(),
        negotiated=PixelSpec(),
        backend="dshow",
        timestamp_origin="host_monotonic_post_retrieve",
    )
    schema = json.loads(
        (PROJECT_ROOT / "schemas" / "capture-source-provenance.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(schema).iter_errors(provenance.to_dict())) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"input_owner": "core_v2"},
        {"real_input_enabled": True},
        {"real_input_call_count": 1},
        {"physical_device_fingerprint": ""},
        {"source_id": "raw/device"},
        {"requested": {"device_name": "VC-003 Video"}},
    ],
)
def test_capture_provenance_rejects_privileged_or_identifying_values(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        CaptureSourceProvenance(**kwargs)  # type: ignore[arg-type]
