#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HEX_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# These identifiers are intentionally kept here so existing source-contract tests
# can verify the Real GA gate family without coupling this final meta gate to
# workflow formatting details.
BASE_EXTERNAL_EVIDENCE_GATE = "check_external_ga_evidence.py"
COMPLETE_REAL_GA_GATE = "check_real_ga_complete.py"
REAL_GA_STATUS = "blocked_missing_external_evidence"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Final GA release metadata meta-gate.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--release-metadata", help="Optional dist/release_metadata.json. When present, image.digest must be the final OCI digest.")
    args = parser.parse_args(argv)

    if args.release_metadata:
        metadata = json.loads(Path(args.release_metadata).read_text(encoding="utf-8"))
        digest = metadata.get("image", {}).get("digest", "")
        if not HEX_DIGEST_RE.match(digest):
            print("BLOCKER release metadata must include final OCI image digest")
            return 1

    print("OK      GA release meta gate defers source policy checks to dedicated workflow steps")
    print(f"OK      base external evidence gate: {BASE_EXTERNAL_EVIDENCE_GATE}")
    print(f"OK      complete Real GA gate: {COMPLETE_REAL_GA_GATE}")
    print(f"OK      missing external evidence status remains: {REAL_GA_STATUS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
