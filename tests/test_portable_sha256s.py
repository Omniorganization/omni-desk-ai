from __future__ import annotations

from pathlib import Path

from scripts.write_portable_sha256s import main


def test_write_portable_sha256_manifest_uses_relative_paths(tmp_path, capsys) -> None:
    (tmp_path / "one.zip").write_bytes(b"one")
    (tmp_path / "two.zip").write_bytes(b"two")

    assert main(["--base-dir", str(tmp_path), "--output", "SHA256SUMS.txt", "one.zip", "two.zip"]) == 0

    manifest = (tmp_path / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert str(tmp_path) not in manifest
    assert "one.zip" in manifest
    assert "two.zip" in manifest
    assert main(["--base-dir", str(tmp_path), "--output", "SHA256SUMS.txt", "--verify"]) == 0
    assert "portable sha256 manifest ok" in capsys.readouterr().out


def test_verify_portable_sha256_manifest_rejects_absolute_paths(tmp_path, capsys) -> None:
    (tmp_path / "one.zip").write_bytes(b"one")
    (tmp_path / "SHA256SUMS.txt").write_text("00  /tmp/one.zip\n", encoding="utf-8")

    assert main(["--base-dir", str(tmp_path), "--output", "SHA256SUMS.txt", "--verify"]) == 1
    assert "portable and relative" in capsys.readouterr().err
