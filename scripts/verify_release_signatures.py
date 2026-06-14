#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path


def main(dist_dir: str = "dist") -> int:
    key = os.getenv("OMNIDESK_RELEASE_SIGNING_KEY", "")
    if not key:
        print("OMNIDESK_RELEASE_SIGNING_KEY is required to verify release signatures", file=sys.stderr)
        return 2
    root = Path(dist_dir)
    manifest_path = root / "release_signatures.json"
    if not manifest_path.exists():
        print("release_signatures.json is missing", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
        expected = hmac.new(key.encode("utf-8"), data, hashlib.sha256).hexdigest()
        sig_file = artifact.with_suffix(artifact.suffix + ".sig")
        actual = sig_file.read_text(encoding="utf-8").strip() if sig_file.exists() else ""
        if not hmac.compare_digest(expected, actual) or not hmac.compare_digest(expected, str(item.get("signature", ""))):
            print(f"signature mismatch: {artifact.name}", file=sys.stderr)
            return 1
    print("release signatures verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
