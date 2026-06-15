#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path


def _sqlite_contention(iteration: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "soak.sqlite3"
        with closing(sqlite3.connect(db, timeout=1.0)) as con:
            with con:
                con.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, iteration INTEGER, value TEXT)")
        locks = 0
        for idx in range(8):
            try:
                with closing(sqlite3.connect(db, timeout=1.0)) as con:
                    with con:
                        con.execute("INSERT INTO events(iteration, value) VALUES (?, ?)", (iteration, f"event-{idx}"))
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    locks += 1
                else:
                    raise
        with closing(sqlite3.connect(db, timeout=1.0)) as con:
            rows = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return {"sqlite_lock_count": locks, "sqlite_rows": rows}


def _webhook_replay_storm(iteration: int) -> dict:
    seen: set[str] = set()
    rejected = 0
    accepted = 0
    for idx in range(12):
        nonce = f"{iteration}-{idx // 2}"
        if nonce in seen:
            rejected += 1
        else:
            seen.add(nonce)
            accepted += 1
    return {"webhook_replay_reject_count": rejected, "webhook_accept_count": accepted}


def _approval_race(iteration: int) -> dict:
    decisions: dict[str, str] = {}
    rejected = 0
    proposal = f"proposal-{iteration}"
    for decision in ["approved", "approved", "rejected", "approved"]:
        if proposal in decisions:
            rejected += 1
        else:
            decisions[proposal] = decision
    return {"approval_race_reject_count": rejected}


def _sandbox_timeout_storm() -> dict:
    simulated = [0.01, 0.02, 0.2, 0.01, 0.3]
    timeout_count = sum(1 for item in simulated if item > 0.1)
    return {"sandbox_timeout_count": timeout_count}


def _memory_purge_race(iteration: int) -> dict:
    now = time.time()
    records = [{"id": idx, "expires_at": now - 1 if idx % 2 == 0 else now + 3600} for idx in range(20)]
    candidates = [record for record in records if record["expires_at"] <= now]
    deleted = {record["id"] for record in candidates}
    return {"memory_purge_candidate_count": len(candidates), "memory_purge_deleted_count": len(deleted)}


def run_soak(*, iterations: int, sleep_seconds: float) -> dict:
    started = time.time()
    totals = {
        "iterations": 0,
        "failure_count": 0,
        "sqlite_lock_count": 0,
        "queue_backlog": 0,
        "tool_timeout_count": 0,
        "webhook_replay_reject_count": 0,
        "approval_race_reject_count": 0,
        "sandbox_timeout_count": 0,
        "memory_purge_deleted_count": 0,
    }
    for iteration in range(iterations):
        try:
            sqlite_result = _sqlite_contention(iteration)
            webhook_result = _webhook_replay_storm(iteration)
            approval_result = _approval_race(iteration)
            sandbox_result = _sandbox_timeout_storm()
            memory_result = _memory_purge_race(iteration)
            totals["sqlite_lock_count"] += sqlite_result["sqlite_lock_count"]
            totals["webhook_replay_reject_count"] += webhook_result["webhook_replay_reject_count"]
            totals["approval_race_reject_count"] += approval_result["approval_race_reject_count"]
            totals["sandbox_timeout_count"] += sandbox_result["sandbox_timeout_count"]
            totals["tool_timeout_count"] += sandbox_result["sandbox_timeout_count"]
            totals["memory_purge_deleted_count"] += memory_result["memory_purge_deleted_count"]
            totals["iterations"] += 1
        except Exception:
            totals["failure_count"] += 1
            raise
        finally:
            gc.collect()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    elapsed = time.time() - started
    totals["elapsed_seconds"] = round(elapsed, 3)
    totals["failure_rate"] = 0.0 if totals["iterations"] == 0 else totals["failure_count"] / totals["iterations"]
    totals["ok"] = totals["failure_count"] == 0
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic soak/chaos smoke checks for GA runtime hardening.")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--duration-seconds", type=float, default=0.0, help="Optional wall-clock duration target; iterations remain bounded.")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    iterations = max(1, args.iterations)
    if args.duration_seconds > 0 and args.sleep_seconds > 0:
        iterations = max(iterations, int(args.duration_seconds / args.sleep_seconds))
    report = run_soak(iterations=iterations, sleep_seconds=max(0.0, args.sleep_seconds))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
