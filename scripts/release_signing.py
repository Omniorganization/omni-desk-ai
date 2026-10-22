"""Shared release-signature primitives.

Two algorithms exist:

* ``ed25519`` (default): only the release job holds the private key
  (``OMNIDESK_RELEASE_SIGNING_ED25519_KEY``, a secret of the protected
  ``release`` environment); verifiers use the committed
  ``release/release-signing-public-key.pem`` (or
  ``OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY``) and hold no secret.
* ``hmac-sha256`` (legacy, local development only): every verifier needs the
  signing secret, so any job that verifies could also forge signatures. It is
  used only when ``OMNIDESK_RELEASE_SIGNATURE_ALGORITHM=hmac-sha256`` is set
  explicitly, and refused when ``OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES=1``.

The algorithm always comes from the signer/verifier environment, never from
the unsigned ``release_signatures.json`` manifest, so a manifest cannot
downgrade a verifier.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

ED25519_PRIVATE_KEY_ENV = "OMNIDESK_RELEASE_SIGNING_ED25519_KEY"
ED25519_PUBLIC_KEY_ENV = "OMNIDESK_RELEASE_VERIFY_PUBLIC_KEY"
REQUIRE_ED25519_ENV = "OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES"
ALGORITHM_ENV = "OMNIDESK_RELEASE_SIGNATURE_ALGORITHM"
LEGACY_HMAC_KEY_ENV = "OMNIDESK_RELEASE_SIGNING_KEY"
ED25519 = "ed25519"
LEGACY_HMAC = "hmac-sha256"
COMMITTED_PUBLIC_KEY = Path(__file__).resolve().parent.parent / "release" / "release-signing-public-key.pem"
SIGNATURE_MANIFEST_NAME = "release_signatures.json"
CHECKSUM_MANIFEST_NAME = "checksums.txt"


def signable_artifact_names(dist: Path) -> set[str]:
    """Every top-level file in dist/ except signature sidecars and the signature manifest."""
    return {
        path.name
        for path in dist.iterdir()
        if path.is_file() and not path.name.endswith(".sig") and path.name != SIGNATURE_MANIFEST_NAME
    }


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
    if not value:
        return None
    key = _load_public_key(value)

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


def policy_algorithm() -> str:
    """The signature algorithm this signer/verifier accepts; raises ValueError for a refused choice."""
    requested = os.getenv(ALGORITHM_ENV, "").strip().lower() or ED25519
    if requested == ED25519:
        return ED25519
    if requested == LEGACY_HMAC:
        if ed25519_required():
            raise ValueError(f"legacy {LEGACY_HMAC} release signatures are refused ({REQUIRE_ED25519_ENV} is set)")
        return LEGACY_HMAC
    raise ValueError(f"unsupported {ALGORITHM_ENV}: {requested}")
