from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.storage.migrations import Migration, apply_migrations
from omnidesk_agent.storage.sqlite import connect_sqlite


class ModelCostStore:
    """Durable SQLite ledger for model usage and estimated cost attribution."""

    def __init__(self, db_path: Path):
        self.db_path = db_path.expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS model_cost_events (
                  id TEXT PRIMARY KEY,
                  task_id TEXT,
                  run_id TEXT,
                  actor TEXT,
                  provider TEXT NOT NULL,
                  model TEXT NOT NULL,
                  profile TEXT NOT NULL,
                  task TEXT,
                  input_tokens INTEGER NOT NULL DEFAULT 0,
                  output_tokens INTEGER NOT NULL DEFAULT 0,
                  estimated_cost_usd REAL NOT NULL DEFAULT 0,
                  cache_hit INTEGER NOT NULL DEFAULT 0,
                  created_at REAL NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_model_cost_created ON model_cost_events(created_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_model_cost_provider ON model_cost_events(provider, model, created_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_model_cost_actor ON model_cost_events(actor, created_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_model_cost_task ON model_cost_events(task_id, created_at)")
            apply_migrations(con, [Migration(1, "model_cost_events_schema_baseline", lambda _con: None)])

    def record(self, **kwargs: Any) -> str:
        now = float(kwargs.get("created_at") or time.time())
        event_id = str(kwargs.get("id") or uuid.uuid4())
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                INSERT INTO model_cost_events(
                  id, task_id, run_id, actor, provider, model, profile, task,
                  input_tokens, output_tokens, estimated_cost_usd, cache_hit, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    kwargs.get("task_id"),
                    kwargs.get("run_id"),
                    kwargs.get("actor"),
                    str(kwargs.get("provider") or "unknown"),
                    str(kwargs.get("model") or "unknown"),
                    str(kwargs.get("profile") or "unknown"),
                    kwargs.get("task"),
                    int(kwargs.get("input_tokens") or 0),
                    int(kwargs.get("output_tokens") or 0),
                    float(kwargs.get("estimated_cost_usd") or kwargs.get("estimated_cost") or 0.0),
                    1 if kwargs.get("cache_hit") else 0,
                    now,
                ),
            )
        return event_id

    def summary(self, *, days: int = 7, group_by: Optional[str] = None) -> dict[str, Any]:
        days = max(1, int(days))
        since = time.time() - days * 86400
        allowed_groups = {"provider", "actor", "task", "profile", "model"}
        with connect_sqlite(self.db_path) as con:
            row = con.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                       COALESCE(SUM(estimated_cost_usd),0), COALESCE(SUM(cache_hit),0)
                FROM model_cost_events WHERE created_at >= ?
                """,
                (since,),
            ).fetchone()
            grouped: dict[str, dict[str, Any]] = {}
            if group_by in allowed_groups:
                column = {"task": "task_id"}.get(group_by, group_by)
                for item in con.execute(
                    f"""
                    SELECT COALESCE({column}, ''), COUNT(*), COALESCE(SUM(input_tokens),0),
                           COALESCE(SUM(output_tokens),0), COALESCE(SUM(estimated_cost_usd),0)
                    FROM model_cost_events WHERE created_at >= ? GROUP BY COALESCE({column}, '')
                    """,
                    (since,),
                ).fetchall():
                    grouped[str(item[0] or "unknown")] = {
                        "calls": int(item[1]),
                        "input_tokens": int(item[2]),
                        "output_tokens": int(item[3]),
                        "estimated_cost_usd": float(item[4]),
                    }
        return {
            "days": days,
            "calls": int(row[0]),
            "input_tokens": int(row[1]),
            "output_tokens": int(row[2]),
            "estimated_cost_usd": float(row[3]),
            "cache_hits": int(row[4]),
            "group_by": group_by,
            "groups": grouped,
        }

    def close(self) -> None:
        return None
