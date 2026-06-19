#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXCLUDED_SUFFIXES = (
    ".cosign.sig",
    ".cosign.pem",
    ".intoto.sig",
    ".intoto.pem",
)


def list_payload_artifacts(dist: Path) -> list[Path]:
    """Return release payload files that must have Cosign blob signatures.

    HMAC sidecars (``*.sig``) are included because they are part of the release
    payload. Cosign-generated signature/certificate files and in-toto sidecars
    are excluded to avoid recursively signing signatures.
    """
    if not dist.exists():
        raise FileNotFoundError(dist)
    artifacts = []
    for path in sorted(dist.iterdir(), key=lambda p: p.name):
        if not path.is_file():
            continue
        if path.name.endswith(EXCLUDED_SUFFIXES):
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
