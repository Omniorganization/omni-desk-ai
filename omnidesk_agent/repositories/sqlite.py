from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from omnidesk_agent.repositories.base import RepositoryCapabilities
from omnidesk_agent.storage.sqlite import connect_sqlite


class SQLiteTransactionalOutboxRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS transactional_outbox (
                  id TEXT PRIMARY KEY,
                  dedupe_key TEXT UNIQUE,
                  topic TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  retry_count INTEGER NOT NULL DEFAULT 0,
                  locked_at REAL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  last_error TEXT
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_transactional_outbox_status ON transactional_outbox(status, created_at)")

    def enqueue(self, *, topic: str, payload: dict[str, Any], dedupe_key: str | None = None) -> str:
        now = time.time()
        event_id = str(uuid.uuid4())
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                INSERT OR IGNORE INTO transactional_outbox(id, dedupe_key, topic, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (event_id, dedupe_key, topic, json.dumps(payload, ensure_ascii=False, default=str), now, now),
            )
            if dedupe_key:
                row = con.execute("SELECT id FROM transactional_outbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()
                if row:
                    return str(row[0])
        return event_id

    def claim_batch(self, *, limit: int = 10, lease_seconds: int = 60) -> list[dict[str, Any]]:
        now = time.time()
        cutoff = now - max(0, int(lease_seconds))
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """
                SELECT id, topic, payload_json, retry_count FROM transactional_outbox
                WHERE status='pending' OR (status='running' AND locked_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (cutoff, int(limit)),
            ).fetchall()
            ids = [str(row[0]) for row in rows]
            if ids:
                con.executemany("UPDATE transactional_outbox SET status='running', locked_at=?, updated_at=? WHERE id=?", [(now, now, event_id) for event_id in ids])
            con.commit()
        return [
            {"id": str(row[0]), "topic": str(row[1]), "payload": json.loads(row[2]), "retry_count": int(row[3])}
            for row in rows
        ]

    def mark_done(self, event_id: str) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute("UPDATE transactional_outbox SET status='done', updated_at=?, locked_at=NULL WHERE id=?", (time.time(), event_id))

    def mark_failed(self, event_id: str, error: str) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                UPDATE transactional_outbox
                SET status='pending', retry_count=retry_count+1, updated_at=?, locked_at=NULL, last_error=?
                WHERE id=?
                """,
                (time.time(), str(error)[:1000], event_id),
            )


class SQLiteRepositoryFactory:
    capabilities = RepositoryCapabilities(
        backend="sqlite",
        multi_instance_safe=False,
        transactional_outbox=True,
        advisory_locks=False,
        row_level_locking=False,
    )

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.workspace_root = Path(db_path).parent

    def transactional_outbox(self) -> SQLiteTransactionalOutboxRepository:
        return SQLiteTransactionalOutboxRepository(self.db_path)

    # Runtime state stores stay local-only for sqlite deployments. Production HA
    # must use the matching methods on PostgresRepositoryFactory instead.
    def dual_approval_store(self):
        from omnidesk_agent.security.dual_approval import DualApprovalStore
        return DualApprovalStore(self.workspace_root / "dual_approvals.sqlite3")

    def approval_store(self, *, ttl_seconds: int, dual_approval_store=None):
        from omnidesk_agent.security.approval_store import ApprovalStore
        return ApprovalStore(self.workspace_root / "approvals.sqlite3", ttl_seconds=ttl_seconds, dual_approval_store=dual_approval_store)

    def break_glass_store(self, *, audit_log: Path):
        from omnidesk_agent.security.break_glass import BreakGlassStore
        return BreakGlassStore(self.workspace_root / "break_glass.sqlite3", audit_log=audit_log)

    def webhook_security(self):
        from omnidesk_agent.security.webhook_security import WebhookSecurity
        return WebhookSecurity(self.workspace_root / "webhooks.sqlite3")

    def job_queue(self):
        from omnidesk_agent.core.job_queue import JobQueue
        return JobQueue(self.workspace_root / "jobs.sqlite3")

    def outbound_messages(self):
        from omnidesk_agent.core.outbound_messages import OutboundMessageStore
        return OutboundMessageStore(self.workspace_root / "outbound_messages.sqlite3")

    def run_store(self):
        from omnidesk_agent.core.run_store import RunStore
        return RunStore(self.workspace_root / "runs.sqlite3")

    def agent_run_idempotency_store(self):
        from omnidesk_agent.security.agent_run_idempotency import SQLiteAgentRunIdempotencyStore
        return SQLiteAgentRunIdempotencyStore(self.workspace_root / "agent_run_idempotency.sqlite3")

    def side_effect_idempotency_store(self):
        from omnidesk_agent.security.idempotency import SQLiteSideEffectIdempotencyStore
        return SQLiteSideEffectIdempotencyStore(self.workspace_root / "side_effect_idempotency.sqlite3")

    def learning_experiments(self):
        from omnidesk_agent.self_learning.experiments.experiment_manager import ExperimentManager
        return ExperimentManager(self.workspace_root / "learning_experiments.sqlite3")

    def memory_store(self, privacy_config=None):
        from omnidesk_agent.memory.experience import ExperienceStore
        return ExperienceStore(self.workspace_root / "memory.sqlite3", privacy_config)

    def token_budget_manager(self, config=None):
        from omnidesk_agent.core.token_budget import TokenBudgetManager
        return TokenBudgetManager(self.workspace_root / "token_budget.sqlite3", config)

    def model_cost_store(self):
        from omnidesk_agent.models.cost_store import ModelCostStore
        return ModelCostStore(self.workspace_root / "model_costs.sqlite3")

    def health_check(self) -> dict:
        return {"ok": True, "backend": "sqlite", "multi_instance_safe": False}
