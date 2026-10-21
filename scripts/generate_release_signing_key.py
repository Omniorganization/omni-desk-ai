#!/usr/bin/env python3
"""Create an Ed25519 release signing key pair.

Writes the public key to release/release-signing-public-key.pem (commit it) and
prints the private key PEM to stdout once: store it as the
OMNIDESK_RELEASE_SIGNING_ED25519_KEY secret of the release environment only.
"""
from __future__ import annotations

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def main(public_key_path: str = "release/release-signing-public-key.pem") -> int:
    target = Path(public_key_path)
    if target.exists():
        print(f"{target} already exists; refusing to rotate silently", file=sys.stderr)
        return 1
    key = ed25519.Ed25519PrivateKey.generate()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    sys.stdout.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode("ascii"))
    print(f"wrote public key to {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
