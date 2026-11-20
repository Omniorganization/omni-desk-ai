"""Shadow evaluation of a candidate policy on real traffic, without side effects.

For a sample of real runs, the candidate policy plans the same request in the
background. The shadow plan is never executed, never shown to the requester
and never written to memory; only a comparison with the production plan is
stored (tool/action sequence, risk, approvals, planning latency, errors). When
the production run finishes, its outcome is joined: if production failed at
step k and the shadow plan is identical up to and including step k, the shadow
plan "would_fail_same"; if it differs before step k it "diverged_before_failure"
(no claim that it would have succeeded). Message text is not stored.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from omnidesk_agent.self_learning.policy_hook import PlannerPolicy, RunOutcome
from omnidesk_agent.self_learning.replay.engine import args_digest
from omnidesk_agent.storage.sqlite import connect_sqlite

PlanOnlyFn = Callable[[Any, PlannerPolicy], Awaitable[Any]]
_RISK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _steps(plan: Any) -> list[Any]:
    return list(getattr(plan, "steps", None) or (plan.get("steps") if isinstance(plan, dict) else []) or [])


def _get(step: Any, name: str, default: Any = None) -> Any:
    return step.get(name, default) if isinstance(step, dict) else getattr(step, name, default)


def plan_signature(plan: Any) -> list[tuple[str, str, str]]:
    return [(str(_get(s, "tool")), str(_get(s, "action")), args_digest(_get(s, "args") or {})) for s in _steps(plan)]


def compare_plans(production: Any, shadow: Any) -> dict[str, Any]:
    prod_sig, shadow_sig = plan_signature(production), plan_signature(shadow)
    prefix = 0
    for a, b in zip(prod_sig, shadow_sig):
        if a != b:
            break
        prefix += 1

    def max_risk(plan: Any) -> int:
        return max((_RISK.get(str(_get(s, "risk", "medium")), 1) for s in _steps(plan)), default=0)

    def approvals(plan: Any) -> int:
        return sum(1 for s in _steps(plan) if _get(s, "requires_approval"))

    return {
        "identical": prod_sig == shadow_sig,
        "common_prefix": prefix,
        "production_steps": len(prod_sig),
        "shadow_steps": len(shadow_sig),
        "risk_delta": max_risk(shadow) - max_risk(production),
        "approval_delta": approvals(shadow) - approvals(production),
        "production_tools": [f"{t}.{a}" for t, a, _ in prod_sig],
        "shadow_tools": [f"{t}.{a}" for t, a, _ in shadow_sig],
    }


class ShadowRunner:
    def __init__(
        self,
        db_path: Path,
        plan_only: PlanOnlyFn,
        *,
        sample_rate: float = 0.1,
        timeout_seconds: float = 30.0,
        max_in_flight: int = 2,
        max_shadow_runs_per_day: int = 500,
        rng: Optional[random.Random] = None,
    ):
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError("sample_rate must be in [0, 1]")
        self.db_path = Path(db_path).expanduser()
        self.plan_only = plan_only
        self.sample_rate = float(sample_rate)
        self.timeout_seconds = float(timeout_seconds)
        self.max_in_flight = max(1, int(max_in_flight))
        self.max_per_day = max(0, int(max_shadow_runs_per_day))
        # Sampling only; not used for anything security relevant.
        self.rng = rng or random.Random()  # nosec B311
        self.candidate: Optional[PlannerPolicy] = None
        self._in_flight: set[asyncio.Task] = set()
        self._scheduled: dict[str, float] = {}  # run keys that got a shadow plan
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = connect_sqlite(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS learning_shadow_runs (
                  shadow_id TEXT PRIMARY KEY,
                  at REAL NOT NULL,
                  candidate_version TEXT NOT NULL,
                  status TEXT NOT NULL,
                  join_result TEXT,
                  payload_json TEXT NOT NULL,
                  production_json TEXT
                )""")
            con.execute("CREATE INDEX IF NOT EXISTS learning_shadow_candidate ON learning_shadow_runs(candidate_version)")

    def set_candidate(self, policy: Optional[PlannerPolicy]) -> None:
        self.candidate = policy

    def after_plan(self, msg: Any, production_plan: Any, *, run_key: str) -> Optional[asyncio.Task]:
        """Schedule a shadow plan for this run. Never raises, never blocks."""
        try:
            candidate = self.candidate
            if candidate is None or self.rng.random() >= self.sample_rate:
                return None
            if len(self._in_flight) >= self.max_in_flight or self._count_today() >= self.max_per_day:
                return None
            loop = asyncio.get_running_loop()
            self._scheduled[self._id(run_key)] = time.time()
            while len(self._scheduled) > 10_000:
                self._scheduled.pop(next(iter(self._scheduled)))
            task = loop.create_task(self._shadow(msg, production_plan, candidate, run_key))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
            return task
        except Exception:
            return None

    async def _shadow(self, msg: Any, production_plan: Any, candidate: PlannerPolicy, run_key: str) -> None:
        started = time.perf_counter()
        payload: dict[str, Any] = {"candidate_version": candidate.version}
        try:
            plan = await asyncio.wait_for(self.plan_only(msg, candidate), timeout=self.timeout_seconds)
            payload.update(status="planned", comparison=compare_plans(production_plan, plan))
        except asyncio.TimeoutError:
            payload.update(status="timeout")
        except Exception as exc:  # the candidate planner refused or failed
            payload.update(status="planner_error", error_class=type(exc).__name__)
        payload["planning_ms"] = round((time.perf_counter() - started) * 1000, 3)
        try:
            with self._connect() as con:
                # Keep a production outcome that arrived first.
                con.execute(
                    """
                    INSERT INTO learning_shadow_runs(shadow_id, at, candidate_version, status, join_result, payload_json)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(shadow_id) DO UPDATE SET at=excluded.at, candidate_version=excluded.candidate_version,
                      status=excluded.status, payload_json=excluded.payload_json
                    """,
                    (self._id(run_key), time.time(), candidate.version, payload["status"], None, json.dumps(payload)),
                )
                self._join(con, self._id(run_key))
        except Exception:
            pass

    def observe_production(self, run_key: str, outcome: RunOutcome, *, failed_step_index: Optional[int] = None) -> Optional[str]:
        """Record the production outcome; join now, or when the shadow plan arrives."""
        if self._id(run_key) not in self._scheduled:
            return None  # this run was not shadowed (not sampled, or over the caps)
        self._scheduled.pop(self._id(run_key), None)
        production = {"status": outcome.status, "success": outcome.success, "failed_step_index": failed_step_index}
        try:
            with self._connect() as con:
                con.execute(
                    """
                    INSERT INTO learning_shadow_runs(shadow_id, at, candidate_version, status, join_result, payload_json, production_json)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(shadow_id) DO UPDATE SET production_json=excluded.production_json
                    """,
                    (self._id(run_key), time.time(), "", "pending_shadow", None, "{}", json.dumps(production)),
                )
                return self._join(con, self._id(run_key))
        except Exception:
            return None

    @staticmethod
    def _join(con: sqlite3.Connection, shadow_id: str) -> Optional[str]:
        row = con.execute("SELECT status, payload_json, production_json FROM learning_shadow_runs WHERE shadow_id=?", (shadow_id,)).fetchone()
        if row is None or row["status"] == "pending_shadow" or not row["production_json"]:
            return None
        payload = json.loads(row["payload_json"])
        production = json.loads(row["production_json"])
        comparison = payload.get("comparison") or {}
        failed = production.get("failed_step_index")
        if payload.get("status") != "planned":
            result = "no_shadow_plan"
        elif comparison.get("identical"):
            result = "identical_plan"
        elif production.get("success"):
            result = "production_succeeded_shadow_diverged"
        elif failed is not None and comparison.get("common_prefix", 0) > failed:
            result = "would_fail_same"
        elif failed is not None:
            result = "diverged_before_failure"
        else:
            result = "production_failed_unknown_step"
        con.execute("UPDATE learning_shadow_runs SET join_result=? WHERE shadow_id=?", (result, shadow_id))
        return result

    def report(self, candidate_version: str) -> dict[str, Any]:
        with self._connect() as con:
            rows = con.execute("SELECT status, join_result, payload_json FROM learning_shadow_runs WHERE candidate_version=? AND status != 'pending_shadow'", (candidate_version,)).fetchall()
        statuses = Counter(r["status"] for r in rows)
        joins = Counter(r["join_result"] for r in rows if r["join_result"])
        planned = [json.loads(r["payload_json"]) for r in rows if r["status"] == "planned"]
        comparisons = [p["comparison"] for p in planned]
        latencies = sorted(p["planning_ms"] for p in planned)
        return {
            "candidate_version": candidate_version,
            "shadow_runs": len(rows),
            "statuses": dict(statuses),
            "identical_rate": round(sum(1 for c in comparisons if c["identical"]) / len(comparisons), 6) if comparisons else None,
            "risk_increased": sum(1 for c in comparisons if c["risk_delta"] > 0),
            "approvals_added": sum(1 for c in comparisons if c["approval_delta"] > 0),
            "joins": dict(joins),
            "planning_ms_p50": latencies[len(latencies) // 2] if latencies else None,
            "note": "shadow plans are never executed; divergence is not evidence of success",
        }

    def _count_today(self) -> int:
        since = time.time() - 86400
        with self._connect() as con:
            return int(con.execute("SELECT COUNT(*) FROM learning_shadow_runs WHERE at >= ? AND status != 'pending_shadow'", (since,)).fetchone()[0])

    @staticmethod
    def _id(run_key: str) -> str:
        return "sh_" + hashlib.sha256(str(run_key).encode("utf-8")).hexdigest()[:24]

    async def drain(self) -> None:
        """Wait for in-flight shadow plans (tests and shutdown)."""
        if self._in_flight:
            await asyncio.gather(*list(self._in_flight), return_exceptions=True)
