#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_signing import (  # noqa: E402
    ED25519,
    LEGACY_HMAC_KEY_ENV,
    SIGNATURE_MANIFEST_NAME,
    ed25519_verifier,
    policy_algorithm,
    signable_artifact_names,
)


def _fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def main(dist_dir: str = "dist") -> int:
    root = Path(dist_dir)
    manifest_path = root / SIGNATURE_MANIFEST_NAME
    if not manifest_path.exists():
        return _fail(f"{SIGNATURE_MANIFEST_NAME} is missing")
    try:
        # The verifier's own policy decides the algorithm; the manifest only has to agree.
        algorithm = policy_algorithm()
    except ValueError as exc:
        return _fail(str(exc))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return _fail(f"{SIGNATURE_MANIFEST_NAME} is not valid JSON: {exc}")
    if not isinstance(manifest, dict):
        return _fail(f"{SIGNATURE_MANIFEST_NAME} must be a JSON object")
    if manifest.get("algorithm") != algorithm:
        return _fail(f"{SIGNATURE_MANIFEST_NAME} declares {manifest.get('algorithm')!r}; this verifier only accepts {algorithm}")
    if algorithm == ED25519:
        verify = ed25519_verifier()
        if verify is None:
            return _fail("an Ed25519 release verification public key is required to verify release signatures", 2)
    else:
        key = os.getenv(LEGACY_HMAC_KEY_ENV, "")
        if not key:
            return _fail(f"{LEGACY_HMAC_KEY_ENV} is required to verify legacy {algorithm} release signatures", 2)

        def verify(data: bytes, signature: str) -> bool:
            expected = hmac.new(key.encode("utf-8"), data, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return _fail(f"{SIGNATURE_MANIFEST_NAME} lists no signed artifacts")
    names: list[str] = []
    for item in artifacts:
        name = str(item.get("name") or "") if isinstance(item, dict) else ""
        if not name or name in {".", ".."} or Path(name).name != name:
            return _fail(f"{SIGNATURE_MANIFEST_NAME} contains an invalid artifact name: {name!r}")
        names.append(name)
    if len(set(names)) != len(names):
        return _fail(f"{SIGNATURE_MANIFEST_NAME} lists an artifact more than once")
    signable = signable_artifact_names(root)
    if set(names) != signable:
        return _fail(f"signed artifact set mismatch: unsigned={sorted(signable - set(names))} missing={sorted(set(names) - signable)}")
    for item in artifacts:
        artifact = root / item["name"]
        data = artifact.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest, str(item.get("sha256", ""))):
            return _fail(f"sha256 mismatch: {artifact.name}")
        sig_file = artifact.with_suffix(artifact.suffix + ".sig")
        actual = sig_file.read_text(encoding="utf-8").strip() if sig_file.exists() else ""
        listed = str(item.get("signature", ""))
        if not actual or not hmac.compare_digest(actual, listed) or not verify(data, actual):
            return _fail(f"signature mismatch: {artifact.name}")
    print(f"release signatures verified ({algorithm}, {len(names)} artifacts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*(sys.argv[1:] or [])))
