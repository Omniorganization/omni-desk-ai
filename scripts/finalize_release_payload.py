#!/usr/bin/env python3
"""Write checksums.txt for the complete release payload, sign it, and verify the result.

Run this once every file that ships in dist/ exists: wheel, SBOM, release
metadata, native bundle, locked Helm values, SLSA provenance and the Cosign
signatures/certificates. Nothing may be added to dist/ afterwards (GitHub
artifact attestation only reads it), so
``verify_release_artifact.py --require-signatures`` holds for the published
artifact exactly as it does here.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sign_release  # noqa: E402
import verify_release_artifact  # noqa: E402
import verify_release_signatures  # noqa: E402
from release_signing import CHECKSUM_MANIFEST_NAME, signable_artifact_names  # noqa: E402


def write_checksums(dist: Path) -> list[str]:
    names = sorted(signable_artifact_names(dist) - {CHECKSUM_MANIFEST_NAME})
    lines = [f"{hashlib.sha256((dist / name).read_bytes()).hexdigest()}  {name}" for name in names]
    (dist / CHECKSUM_MANIFEST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dist_dir", nargs="?", default="dist")
    parser.add_argument("--expected-version")
    parser.add_argument("--expected-image-digest")
    parser.add_argument(
        "--checksums-only",
        action="store_true",
        help="Local development only (OMNIDESK_ALLOW_UNSIGNED_RELEASE=1): write checksums.txt without signing.",
    )
    args = parser.parse_args(argv)

    dist = Path(args.dist_dir)
    if not dist.is_dir():
        print(f"dist directory missing: {dist}", file=sys.stderr)
        return 1
    if not write_checksums(dist):
        print(f"no release artifacts in {dist}", file=sys.stderr)
        return 1
    verify_args = [str(dist), "--require-metadata"]
    if args.expected_version:
        verify_args += ["--expected-version", args.expected_version]
    if args.expected_image_digest:
        verify_args += ["--expected-image-digest", args.expected_image_digest]
    if args.checksums_only:
        return verify_release_artifact.main(verify_args)
    status = sign_release.main(str(dist))
    if status:
        return status
    status = verify_release_signatures.main(str(dist))
    if status:
        return status
    return verify_release_artifact.main([*verify_args, "--require-signatures"])


if __name__ == "__main__":
    raise SystemExit(main())
