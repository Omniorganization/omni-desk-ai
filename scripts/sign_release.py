#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_signing import (  # noqa: E402
    ED25519,
    ED25519_PRIVATE_KEY_ENV,
    LEGACY_HMAC_KEY_ENV,
    SIGNATURE_MANIFEST_NAME,
    ed25519_signer,
    policy_algorithm,
    signable_artifact_names,
)


def main(dist_dir: str = "dist") -> int:
    root = Path(dist_dir)
    try:
        algorithm = policy_algorithm()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if algorithm == ED25519:
        signer = ed25519_signer()
        if signer is None:
            print(f"{ED25519_PRIVATE_KEY_ENV} is required for release signing", file=sys.stderr)
            return 2
    else:
        key = os.getenv(LEGACY_HMAC_KEY_ENV, "")
        if not key:
            print(f"{LEGACY_HMAC_KEY_ENV} is required for legacy {algorithm} release signing", file=sys.stderr)
            return 2

        def signer(data: bytes) -> str:
            return hmac.new(key.encode("utf-8"), data, hashlib.sha256).hexdigest()

    artifacts = [root / name for name in sorted(signable_artifact_names(root))]
    if not artifacts:
        print(f"no release artifacts to sign in {root}", file=sys.stderr)
        return 1
    manifest = {"signed_at": time.time(), "algorithm": algorithm, "artifacts": []}
    for artifact in artifacts:
        data = artifact.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        signature = signer(data)
        (artifact.with_suffix(artifact.suffix + ".sig")).write_text(signature + "\n", encoding="utf-8")
        manifest["artifacts"].append({"name": artifact.name, "sha256": sha256, "signature": signature})
    (root / SIGNATURE_MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
