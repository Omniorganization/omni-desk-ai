#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from omnidesk_agent.core.run_store import RunStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find or fail stuck approval/resume transitions.")
    parser.add_argument("--run-db", required=True, type=Path, help="Path to runs.sqlite3.")
    parser.add_argument("--older-than-seconds", type=float, default=300)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--mark-failed", action="store_true", help="Mark matching resuming runs as resume_failed.")
    args = parser.parse_args(argv)

    store = RunStore(args.run_db)
    stuck = store.list_resuming(older_than_seconds=args.older_than_seconds, limit=args.limit)
    if args.mark_failed:
        for run in stuck:
            store.mark_resume_failed(run["id"], f"stuck in resuming for more than {args.older_than_seconds:g}s")
    print(json.dumps({"ok": True, "count": len(stuck), "marked_failed": bool(args.mark_failed), "run_ids": [run["id"] for run in stuck]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
