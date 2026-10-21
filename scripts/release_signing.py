"""Shared release-signature primitives.

Two algorithms are supported:

* ``hmac-sha256`` (legacy): every verifier needs the signing secret, so any job
  that verifies can also forge signatures.
* ``ed25519``: only the release job holds the private key
  (``OMNIDESK_RELEASE_SIGNING_ED25519_KEY``); verifiers use the public key from
  ``OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY`` or the committed
  ``release/release-signing-public-key.pem``.

Set ``OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES=1`` on verifiers once the
Ed25519 key is provisioned, so a legacy HMAC manifest is refused (no downgrade).
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

ED25519_PRIVATE_KEY_ENV = "OMNIDESK_RELEASE_SIGNING_ED25519_KEY"
ED25519_PUBLIC_KEY_ENV = "OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY"
REQUIRE_ED25519_ENV = "OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES"
COMMITTED_PUBLIC_KEY = Path(__file__).resolve().parent.parent / "release" / "release-signing-public-key.pem"


def _load_private_key(value: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    value = value.strip()
    if value.startswith("-----BEGIN"):
        key = serialization.load_pem_private_key(value.encode("utf-8"), password=None)
    else:
        key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(value))
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError(f"{ED25519_PRIVATE_KEY_ENV} must be an Ed25519 private key")
    return key


def _load_public_key(value: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = serialization.load_pem_public_key(value.strip().encode("utf-8"))
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise ValueError("release verification key must be an Ed25519 public key")
    return key


def ed25519_signer():
    """Returns a sign(bytes) -> str callable, or None when no Ed25519 key is configured."""
    value = os.getenv(ED25519_PRIVATE_KEY_ENV, "")
    if not value:
        return None
    key = _load_private_key(value)
    return lambda data: base64.b64encode(key.sign(data)).decode("ascii")


def ed25519_verifier():
    """Returns a verify(bytes, str) -> bool callable, or None when no public key is available."""
    value = os.getenv(ED25519_PUBLIC_KEY_ENV, "")
    if not value and COMMITTED_PUBLIC_KEY.exists():
        value = COMMITTED_PUBLIC_KEY.read_text(encoding="utf-8")
    if value:
        key = _load_public_key(value)
    elif os.getenv(ED25519_PRIVATE_KEY_ENV, ""):
        # The signing job's own self-check (verifier jobs never hold the private key).
        key = _load_private_key(os.environ[ED25519_PRIVATE_KEY_ENV]).public_key()
    else:
        return None

    def verify(data: bytes, signature: str) -> bool:
        from cryptography.exceptions import InvalidSignature

        try:
            key.verify(base64.b64decode(signature, validate=True), data)
            return True
        except (InvalidSignature, ValueError):
            return False

    return verify


def ed25519_required() -> bool:
    return os.getenv(REQUIRE_ED25519_ENV, "").strip().lower() in {"1", "true", "yes"}
