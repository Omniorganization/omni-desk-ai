#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path


def main(dist_dir: str = "dist") -> int:
    root = Path(dist_dir)
    key = os.getenv("OMNIDESK_RELEASE_SIGNING_KEY", "")
    if not key:
        print("OMNIDESK_RELEASE_SIGNING_KEY is required for release signing", file=sys.stderr)
        return 2
    artifacts = [p for p in sorted(root.iterdir()) if p.is_file() and not p.name.endswith(".sig") and p.name != "release_signatures.json"]
    manifest = {"signed_at": time.time(), "algorithm": "hmac-sha256", "artifacts": []}
    for artifact in artifacts:
        data = artifact.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        signature = hmac.new(key.encode("utf-8"), data, hashlib.sha256).hexdigest()
        (artifact.with_suffix(artifact.suffix + ".sig")).write_text(signature + "\n", encoding="utf-8")
        manifest["artifacts"].append({"name": artifact.name, "sha256": sha256, "signature": signature})
    (root / "release_signatures.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
