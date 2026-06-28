from __future__ import annotations

import base64

import pytest

from omnidesk_agent.models.providers import _image_part_from_path


def test_image_input_requires_bound_upload_root(tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    with pytest.raises(PermissionError, match="upload root"):
        _image_part_from_path(str(image), {})


def test_image_input_allows_file_inside_upload_root(tmp_path):
    image = tmp_path / "image.png"
    raw = b"\x89PNG\r\n\x1a\n"
    image.write_bytes(raw)

    part = _image_part_from_path(
        str(image),
        {"allowed_image_roots": [str(tmp_path)]},
    )

    assert part["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(part["inline_data"]["data"]) == raw


def test_image_input_rejects_file_outside_upload_root(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")

    with pytest.raises(PermissionError, match="outside"):
        _image_part_from_path(
            str(outside),
            {"allowed_image_roots": [str(root)]},
        )
