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
        "OMNIDESK_RELEASE_SIGNATURE_ALGORITHM",
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


def test_legacy_hmac_needs_explicit_opt_in_and_is_refused_once_ed25519_is_required(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_KEY", "legacy-secret")
    assert sign_release.main(str(dist)) == 2  # HMAC is never picked implicitly
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNATURE_ALGORITHM", "hmac-sha256")
    assert sign_release.main(str(dist)) == 0
    assert verify_release_signatures.main(str(dist)) == 0  # explicit legacy mode still works locally
    monkeypatch.setenv("OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES", "1")
    assert verify_release_signatures.main(str(dist)) == 1
    assert sign_release.main(str(dist)) == 2


def test_manifest_cannot_downgrade_a_default_verifier_to_hmac(tmp_path, monkeypatch):
    # Whoever holds the legacy HMAC secret signs a release; a verifier that still
    # has the secret in its environment must not follow the manifest's algorithm.
    dist = _dist(tmp_path)
    _, trusted_public = _keys()
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_KEY", "legacy-secret")
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNATURE_ALGORITHM", "hmac-sha256")
    assert sign_release.main(str(dist)) == 0
    monkeypatch.delenv("OMNIDESK_RELEASE_SIGNATURE_ALGORITHM")
    monkeypatch.setenv("OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY", trusted_public)
    assert verify_release_signatures.main(str(dist)) == 1


@pytest.mark.parametrize("algorithm", ["ed25519", "hmac-sha256"])
def test_empty_signature_manifest_is_rejected(tmp_path, monkeypatch, algorithm):
    dist = _dist(tmp_path)
    (dist / "release_signatures.json").write_text(json.dumps({"algorithm": algorithm, "artifacts": []}), encoding="utf-8")
    _, public = _keys()
    monkeypatch.setenv("OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY", public)
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_KEY", "legacy-secret")
    if algorithm == "hmac-sha256":
        monkeypatch.setenv("OMNIDESK_RELEASE_SIGNATURE_ALGORITHM", algorithm)
    assert verify_release_signatures.main(str(dist)) == 1


def test_file_added_after_signing_is_rejected(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    private, public = _keys()
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY", private)
    assert sign_release.main(str(dist)) == 0
    monkeypatch.delenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY")
    monkeypatch.setenv("OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY", public)
    assert verify_release_signatures.main(str(dist)) == 0
    (dist / "values.production.locked.yaml").write_text("image: unsigned\n", encoding="utf-8")
    assert verify_release_signatures.main(str(dist)) == 1


def test_manifest_entries_must_be_plain_file_names(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    private, public = _keys()
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY", private)
    assert sign_release.main(str(dist)) == 0
    manifest = json.loads((dist / "release_signatures.json").read_text())
    manifest["artifacts"][0]["name"] = "../" + manifest["artifacts"][0]["name"]
    (dist / "release_signatures.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.delenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY")
    monkeypatch.setenv("OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY", public)
    assert verify_release_signatures.main(str(dist)) == 1


def test_signing_requires_an_ed25519_key_and_a_payload(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    assert sign_release.main(str(dist)) == 2
    private, _ = _keys()
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY", private)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert sign_release.main(str(empty)) == 1


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


def test_signing_job_self_check_uses_the_committed_public_key(tmp_path, monkeypatch):
    # A release secret that does not match the committed public key must fail the
    # release job itself, not only the verifiers; deriving the public key from the
    # private key would only prove the secret is self-consistent.
    import sys

    dist = _dist(tmp_path)
    private, public = _keys()
    committed = tmp_path / "release-signing-public-key.pem"
    monkeypatch.setattr(sys.modules["release_signing"], "COMMITTED_PUBLIC_KEY", committed)
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_ED25519_KEY", private)
    assert sign_release.main(str(dist)) == 0
    assert verify_release_signatures.main(str(dist)) == 2  # no committed key: no self-derived fallback

    _, other_public = _keys()
    committed.write_text(other_public, encoding="utf-8")
    assert verify_release_signatures.main(str(dist)) == 1

    committed.write_text(public, encoding="utf-8")
    assert verify_release_signatures.main(str(dist)) == 0


def test_committed_release_public_key_is_an_ed25519_public_key():
    from scripts.release_signing import COMMITTED_PUBLIC_KEY, _load_public_key

    text = COMMITTED_PUBLIC_KEY.read_text(encoding="ascii")
    assert "PRIVATE" not in text
    _load_public_key(text)
