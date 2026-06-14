#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a compressed PostgreSQL logical backup for OmniDesk.")
    parser.add_argument("--dsn-env", default="OMNIDESK_POSTGRES_DSN")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    dsn = os.getenv(args.dsn_env, "")
    if not dsn:
        print(f"missing PostgreSQL DSN env: {args.dsn_env}", file=sys.stderr)
        return 2
    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-acl", dsn, "--file", str(out)]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
