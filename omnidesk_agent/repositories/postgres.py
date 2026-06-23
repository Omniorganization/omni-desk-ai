from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from omnidesk_agent.repositories.base import RepositoryCapabilities


POSTGRES_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactional_outbox (
  id TEXT PRIMARY KEY,
  dedupe_key TEXT UNIQUE,
  topic TEXT NOT NULL,
  payload_json JSONB NOT NULL,
  status TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  locked_at DOUBLE PRECISION,
  created_at DOUBLE PRECISION NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_transactional_outbox_status ON transactional_outbox(status, created_at);
"""


class PostgresUnavailable(RuntimeError):
    pass


class PostgresTransactionalOutboxRepository:
    """PostgreSQL transactional outbox skeleton for multi-instance deployments.

    The implementation uses psycopg when installed. Import is delayed so the
    local-first package can run without a PostgreSQL client dependency.
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.getenv("OMNIDESK_POSTGRES_DSN", "")
        if not self.dsn:
            raise PostgresUnavailable("OMNIDESK_POSTGRES_DSN is required for postgres repository backend")

    def _connect(self):  # type: ignore[no-untyped-def]
        try:
            import psycopg  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise PostgresUnavailable("Install psycopg to use postgres repository backend") from exc
        return psycopg.connect(self.dsn)

    def init_schema(self) -> None:
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(POSTGRES_OUTBOX_SCHEMA)

    def enqueue(self, *, topic: str, payload: dict[str, Any], dedupe_key: str | None = None) -> str:
        now = time.time()
        event_id = str(uuid.uuid4())
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO transactional_outbox(id, dedupe_key, topic, payload_json, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, 'pending', %s, %s)
                    ON CONFLICT (dedupe_key) DO UPDATE SET updated_at=transactional_outbox.updated_at
                    RETURNING id
                    """,
                    (event_id, dedupe_key, topic, json.dumps(payload, ensure_ascii=False, default=str), now, now),
                )
                row = cur.fetchone()
                return str(row[0])

    def claim_batch(self, *, limit: int = 10, lease_seconds: int = 60) -> list[dict[str, Any]]:
        now = time.time()
        cutoff = now - max(0, int(lease_seconds))
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    WITH claimed AS (
                      SELECT id FROM transactional_outbox
                      WHERE status='pending' OR (status='running' AND locked_at <= %s)
                      ORDER BY created_at ASC
                      FOR UPDATE SKIP LOCKED
                      LIMIT %s
                    )
                    UPDATE transactional_outbox o
                    SET status='running', locked_at=%s, updated_at=%s
                    FROM claimed
                    WHERE o.id = claimed.id
                    RETURNING o.id, o.topic, o.payload_json, o.retry_count
                    """,
                    (cutoff, int(limit), now, now),
                )
                return [
                    {"id": str(row[0]), "topic": str(row[1]), "payload": row[2], "retry_count": int(row[3])}
                    for row in cur.fetchall()
                ]

    def mark_done(self, event_id: str) -> None:
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute("UPDATE transactional_outbox SET status='done', updated_at=%s, locked_at=NULL WHERE id=%s", (time.time(), event_id))

    def mark_failed(self, event_id: str, error: str) -> None:
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    UPDATE transactional_outbox
                    SET status='pending', retry_count=retry_count+1, updated_at=%s, locked_at=NULL, last_error=%s
                    WHERE id=%s
                    """,
                    (time.time(), str(error)[:1000], event_id),
                )


@dataclass
class PostgresRepositoryFactory:
    dsn: str | None = None
    capabilities: RepositoryCapabilities = RepositoryCapabilities(
        backend="postgres",
        multi_instance_safe=True,
        transactional_outbox=True,
        advisory_locks=True,
        row_level_locking=True,
    )

    def _dsn(self) -> str:
        dsn = self.dsn or os.getenv("OMNIDESK_POSTGRES_DSN", "")
        if not dsn:
            raise PostgresUnavailable("OMNIDESK_POSTGRES_DSN is required for postgres repository backend")
        return dsn

    def transactional_outbox(self) -> PostgresTransactionalOutboxRepository:
        return PostgresTransactionalOutboxRepository(self._dsn())

    def _runtime_state(self):
        from omnidesk_agent.repositories.postgres_state import PostgresRuntimeStateStores
        return PostgresRuntimeStateStores(self._dsn())

    def dual_approval_store(self):
        return self._runtime_state().dual_approval_store()

    def approval_store(self, *, ttl_seconds: int, dual_approval_store=None):
        return self._runtime_state().approval_store(ttl_seconds=ttl_seconds, dual_approval_store=dual_approval_store)

    def break_glass_store(self, *, audit_log):
        return self._runtime_state().break_glass_store(audit_log=audit_log)

    def webhook_security(self):
        return self._runtime_state().webhook_security()

    def job_queue(self):
        return self._runtime_state().job_queue()

    def outbound_messages(self):
        return self._runtime_state().outbound_messages()

    def run_store(self):
        return self._runtime_state().run_store()

    def agent_run_idempotency_store(self):
        return self._runtime_state().agent_run_idempotency_store()


    def learning_experiments(self):
        return self._runtime_state().learning_experiments()

    def memory_store(self, privacy_config=None):
        return self._runtime_state().memory_store(privacy_config=privacy_config)

    def token_budget_manager(self, config=None):
        return self._runtime_state().token_budget_manager(config=config)

    def model_cost_store(self):
        return self._runtime_state().model_cost_store()

    def health_check(self) -> dict:
        self.transactional_outbox().init_schema()
        return self._runtime_state().health_check()
