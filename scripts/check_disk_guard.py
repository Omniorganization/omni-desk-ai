#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail if disk usage exceeds production guardrails.")
    parser.add_argument("--path", default=".", type=Path)
    parser.add_argument("--max-used-percent", type=float, default=85.0)
    parser.add_argument("--min-free-mb", type=float, default=1024.0)
    args = parser.parse_args(argv)
    usage = shutil.disk_usage(args.path)
    used_percent = (usage.used / usage.total) * 100 if usage.total else 100.0
    free_mb = usage.free / (1024 * 1024)
    ok = used_percent <= args.max_used_percent and free_mb >= args.min_free_mb
    payload = {
        "ok": ok,
        "path": str(args.path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round(used_percent, 3),
        "max_used_percent": args.max_used_percent,
        "free_mb": round(free_mb, 3),
        "min_free_mb": args.min_free_mb,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
