#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_signing import REQUIRE_ED25519_ENV, ed25519_required, ed25519_verifier  # noqa: E402


def main(dist_dir: str = "dist") -> int:
    root = Path(dist_dir)
    manifest_path = root / "release_signatures.json"
    if not manifest_path.exists():
        print("release_signatures.json is missing", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    algorithm = str(manifest.get("algorithm") or "hmac-sha256")
    if algorithm == "ed25519":
        verify = ed25519_verifier()
        if verify is None:
            print("an Ed25519 release verification public key is required to verify release signatures", file=sys.stderr)
            return 2
    elif algorithm == "hmac-sha256":
        if ed25519_required():
            print(f"legacy hmac-sha256 release signatures are refused ({REQUIRE_ED25519_ENV} is set)", file=sys.stderr)
            return 1
        key = os.getenv("OMNIDESK_RELEASE_SIGNING_KEY", "")
        if not key:
            print("OMNIDESK_RELEASE_SIGNING_KEY is required to verify release signatures", file=sys.stderr)
            return 2

        def verify(data: bytes, signature: str) -> bool:
            expected = hmac.new(key.encode("utf-8"), data, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
    else:
        print(f"unsupported release signature algorithm: {algorithm}", file=sys.stderr)
        return 1
    for item in manifest.get("artifacts", []):
        artifact = root / item["name"]
        if not artifact.exists():
            print(f"signed artifact missing: {artifact.name}", file=sys.stderr)
            return 1
        data = artifact.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest, str(item.get("sha256", ""))):
            print(f"sha256 mismatch: {artifact.name}", file=sys.stderr)
            return 1
        sig_file = artifact.with_suffix(artifact.suffix + ".sig")
        actual = sig_file.read_text(encoding="utf-8").strip() if sig_file.exists() else ""
        listed = str(item.get("signature", ""))
        if not actual or not hmac.compare_digest(actual, listed) or not verify(data, actual):
            print(f"signature mismatch: {artifact.name}", file=sys.stderr)
            return 1
    print(f"release signatures verified ({algorithm})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
