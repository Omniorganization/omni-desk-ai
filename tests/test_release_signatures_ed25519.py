from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from scripts import sign_release, verify_release_signatures


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "omnidesk-1.0-py3-none-any.whl").write_bytes(b"wheel-bytes")
    (dist / "release_metadata.json").write_text('{"version": "1.0"}', encoding="utf-8")
    return dist


def _keys() -> tuple[str, str]:
    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "OMNIDESK_RELEASE_SIGNING_KEY",
        "OMNIDESK_RELEASE_SIGNING_ED25519_KEY",
        "OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY",
        "OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES",
    ):
        monkeypatch.delenv(name, raising=False)


def test_ed25519_release_signatures_verify_with_public_key_only(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    private, public = _keys()
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY", private)
    assert sign_release.main(str(dist)) == 0
    assert json.loads((dist / "release_signatures.json").read_text())["algorithm"] == "ed25519"

    # The verifier holds no signing secret at all.
    monkeypatch.delenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY")
    monkeypatch.setenv("OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY", public)
    monkeypatch.setenv("OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES", "1")
    assert verify_release_signatures.main(str(dist)) == 0

    (dist / "omnidesk-1.0-py3-none-any.whl").write_bytes(b"tampered")
    assert verify_release_signatures.main(str(dist)) == 1


def test_ed25519_signature_from_another_key_is_rejected(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    attacker_private, _ = _keys()
    _, trusted_public = _keys()
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY", attacker_private)
    assert sign_release.main(str(dist)) == 0
    monkeypatch.delenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY")
    monkeypatch.setenv("OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY", trusted_public)
    assert verify_release_signatures.main(str(dist)) == 1


def test_hmac_manifest_is_refused_once_ed25519_is_required(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_KEY", "legacy-secret")
    assert sign_release.main(str(dist)) == 0
    assert verify_release_signatures.main(str(dist)) == 0  # legacy mode still works
    monkeypatch.setenv("OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES", "1")
    assert verify_release_signatures.main(str(dist)) == 1


def test_raw_base64_seed_is_accepted_as_signing_key(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    key = ed25519.Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY", base64.b64encode(seed).decode())
    assert sign_release.main(str(dist)) == 0
    monkeypatch.delenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY")
    monkeypatch.setenv("OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY", public)
    assert verify_release_signatures.main(str(dist)) == 0
