#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore an OmniDesk PostgreSQL logical backup.")
    parser.add_argument("--dsn-env", default="OMNIDESK_POSTGRES_DSN")
    parser.add_argument("--input", required=True)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args(argv)
    dsn = os.getenv(args.dsn_env, "")
    if not dsn:
        print(f"missing PostgreSQL DSN env: {args.dsn_env}", file=sys.stderr)
        return 2
    src = Path(args.input).expanduser()
    if not src.exists():
        print(f"backup file not found: {src}", file=sys.stderr)
        return 2
    cmd = ["pg_restore", "--no-owner", "--no-acl", "--dbname", dsn]
    if args.clean:
        cmd += ["--clean", "--if-exists"]
    cmd.append(str(src))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
