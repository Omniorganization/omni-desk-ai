#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXCLUDED_SUFFIXES = (
    ".sig",
    ".cosign.pem",
    ".intoto.pem",
)
# Written by scripts/finalize_release_payload.py after Cosign has signed the
# payload; the Ed25519 manifest covers them instead.
SIGNATURE_LAYER_FILES = ("checksums.txt", "release_signatures.json")


def list_payload_artifacts(dist: Path) -> list[Path]:
    """Return release payload files that must have Cosign blob signatures.

    The release job writes the payload (wheel, SBOM, metadata, native bundle,
    locked Helm values, SLSA provenance), signs each file with Cosign, and only
    then writes checksums.txt and the Ed25519 signatures over everything,
    Cosign certificates included. Signature sidecars (``*.sig``), Cosign/in-toto
    certificates and the checksum/signature manifests are therefore not Cosign
    payload.
    """
    if not dist.exists():
        raise FileNotFoundError(dist)
    artifacts = []
    for path in sorted(dist.iterdir(), key=lambda p: p.name):
        if not path.is_file():
            continue
        if path.name.endswith(EXCLUDED_SUFFIXES) or path.name in SIGNATURE_LAYER_FILES:
            continue
        artifacts.append(path)
    return artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List release payload artifacts requiring Cosign signatures.")
    parser.add_argument("dist", nargs="?", default="dist")
    parser.add_argument("--null", action="store_true", help="Emit NUL-delimited paths for shell-safe loops.")
    args = parser.parse_args(argv)
    artifacts = list_payload_artifacts(Path(args.dist))
    if args.null:
        for artifact in artifacts:
            sys.stdout.buffer.write(str(artifact).encode("utf-8") + b"\0")
    else:
        for artifact in artifacts:
            print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
