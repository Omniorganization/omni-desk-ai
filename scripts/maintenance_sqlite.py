#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def maintain_one(path: Path, *, integrity_check: bool, wal_checkpoint: bool, vacuum: bool) -> dict:
    path = path.expanduser().resolve()
    result: dict = {"path": str(path), "ok": False, "started_at": time.time()}
    if not path.exists():
        result["error"] = "missing"
        return result
    try:
        with closing(sqlite3.connect(path, timeout=30)) as con:
            con.execute("PRAGMA busy_timeout=30000")
            if integrity_check:
                result["integrity_check"] = str(con.execute("PRAGMA integrity_check").fetchone()[0])
                if result["integrity_check"].lower() != "ok":
                    result["error"] = "integrity_check_failed"
                    return result
            if wal_checkpoint:
                result["wal_checkpoint"] = list(con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            if vacuum:
                con.execute("VACUUM")
                result["vacuum"] = True
        result["ok"] = True
        result["bytes"] = path.stat().st_size
    except Exception as exc:  # script boundary: serialize failure for operators
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["finished_at"] = time.time()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run SQLite integrity, WAL checkpoint and VACUUM maintenance.")
    parser.add_argument("--integrity-check", action="store_true")
    parser.add_argument("--wal-checkpoint", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("db", nargs="+", type=Path)
    args = parser.parse_args(argv)
    results = [maintain_one(db, integrity_check=args.integrity_check, wal_checkpoint=args.wal_checkpoint, vacuum=args.vacuum) for db in args.db]
    payload = {"ok": all(item.get("ok") for item in results), "results": results}
    print(json.dumps(payload, sort_keys=True) if args.json else json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
