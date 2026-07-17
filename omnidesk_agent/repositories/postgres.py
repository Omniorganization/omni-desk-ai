from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from omnidesk_agent.repositories.base import RepositoryCapabilities
from omnidesk_agent.repositories.postgres_pool import (
    PostgresUnavailable,
    SharedPostgresConnectionPool,
)


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
CREATE INDEX IF NOT EXISTS idx_transactional_outbox_status
  ON transactional_outbox(status, created_at);
"""


class PostgresTransactionalOutboxRepository:
    def __init__(self, dsn_or_pool: str | SharedPostgresConnectionPool):
        self._owns_pool = isinstance(dsn_or_pool, str)
        self.pool = (
            SharedPostgresConnectionPool(dsn_or_pool)
            if isinstance(dsn_or_pool, str)
            else dsn_or_pool
        )
        self._schema_ready = False

    def _connect(self):  # type: ignore[no-untyped-def]
        return self.pool.connection()

    def init_schema(self) -> None:
        if self._schema_ready:
            return
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(POSTGRES_OUTBOX_SCHEMA)
        self._schema_ready = True

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
                    {'id': str(row[0]), 'topic': str(row[1]), 'payload': row[2], 'retry_count': int(row[3])}
                    for row in cur.fetchall()
                ]

    def mark_done(self, event_id: str) -> None:
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE transactional_outbox SET status='done', updated_at=%s, locked_at=NULL WHERE id=%s",
                    (time.time(), event_id),
                )

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

    def close(self) -> None:
        if self._owns_pool:
            self.pool.close()


@dataclass
class PostgresRepositoryFactory:
    dsn: str | None = None
    pool_size: int | None = None
    capabilities: RepositoryCapabilities = RepositoryCapabilities(
        backend='postgres',
        multi_instance_safe=True,
        transactional_outbox=True,
        advisory_locks=True,
        row_level_locking=True,
    )
    _pool: SharedPostgresConnectionPool | None = field(default=None, init=False, repr=False)
    _runtime: Any | None = field(default=None, init=False, repr=False)
    _outbox: PostgresTransactionalOutboxRepository | None = field(default=None, init=False, repr=False)

    def _dsn(self) -> str:
        dsn = self.dsn or os.getenv('OMNIDESK_POSTGRES_DSN', '')
        if not dsn:
            raise PostgresUnavailable('OMNIDESK_POSTGRES_DSN is required for postgres repository backend')
        return dsn

    def _pool_limit(self) -> int:
        raw = self.pool_size or os.getenv('OMNIDESK_CORE_POSTGRES_POOL_SIZE', '12')
        try:
            return max(2, min(int(raw), 64))
        except (TypeError, ValueError) as exc:
            raise PostgresUnavailable('OMNIDESK_CORE_POSTGRES_POOL_SIZE must be an integer') from exc

    def _connection_pool(self) -> SharedPostgresConnectionPool:
        if self._pool is None:
            self._pool = SharedPostgresConnectionPool(
                self._dsn(),
                max_size=self._pool_limit(),
                acquire_timeout_seconds=float(os.getenv('OMNIDESK_CORE_POSTGRES_POOL_TIMEOUT_SECONDS', '5')),
            )
        return self._pool

    def transactional_outbox(self) -> PostgresTransactionalOutboxRepository:
        if self._outbox is None:
            self._outbox = PostgresTransactionalOutboxRepository(self._connection_pool())
        return self._outbox

    def _runtime_state(self):
        if self._runtime is None:
            from omnidesk_agent.repositories.postgres_state import PostgresRuntimeStateStores

            self._runtime = PostgresRuntimeStateStores(self._connection_pool())
        return self._runtime

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

    def side_effect_idempotency_store(self):
        return self._runtime_state().side_effect_idempotency_store()

    def learning_experiments(self):
        return self._runtime_state().learning_experiments()

    def memory_store(self, privacy_config=None):
        return self._runtime_state().memory_store(privacy_config=privacy_config)

    def token_budget_manager(self, config=None):
        return self._runtime_state().token_budget_manager(config=config)

    def model_cost_store(self):
        return self._runtime_state().model_cost_store()

    def readiness_check(self) -> dict[str, Any]:
        return self._connection_pool().ping()

    def health_check(self) -> dict[str, Any]:
        report = self._runtime_state().health_check()
        report['pool'] = self.pool_stats()
        return report

    def pool_stats(self) -> dict[str, Any]:
        return self._connection_pool().stats()

    def close(self) -> None:
        runtime = self._runtime
        if runtime is not None:
            runtime.close()
        if self._pool is not None:
            self._pool.close()
