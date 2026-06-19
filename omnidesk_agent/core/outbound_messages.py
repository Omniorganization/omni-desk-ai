from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.storage.migrations import Migration, apply_migrations
from omnidesk_agent.storage.sqlite import connect_sqlite


TERMINAL_OUTBOUND_STATUSES = {"sent", "dead_letter", "cancelled"}
ACTIVE_OUTBOUND_STATUSES = {"pending", "retry", "running"}
RECONCILIATION_OUTBOUND_STATUSES = {"ambiguous"}


class OutboundMessageStore:
    """SQLite-backed durable queue/audit store for outbound channel sends.

    Channel tools enqueue an outbound message and return quickly. A dispatcher
    owns provider side effects, retries transient failures, records provider
    IDs/request IDs, and moves exhausted messages to a dead-letter state.
    """

    def __init__(self, db_path: Path, *, max_retries: int = 3, base_retry_seconds: int = 30):
        self.db_path = db_path.expanduser()
        self.max_retries = max(0, int(max_retries))
        self.base_retry_seconds = max(1, int(base_retry_seconds))
        self.metrics: Any = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS outbound_messages (
                  id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL,
                  channel TEXT NOT NULL,
                  recipient TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  provider_message_id TEXT,
                  provider_request_id TEXT,
                  retry_count INTEGER NOT NULL DEFAULT 0,
                  max_retries INTEGER NOT NULL DEFAULT 3,
                  next_retry_at REAL NOT NULL DEFAULT 0,
                  locked_at REAL,
                  delivery_deadline_at REAL,
                  last_error TEXT,
                  error_category TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                )
                """
            )
            self._ensure_column(con, "idempotency_key", "TEXT")
            con.execute("UPDATE outbound_messages SET idempotency_key=id WHERE idempotency_key IS NULL OR idempotency_key=''")
            self._ensure_column(con, "max_retries", "INTEGER NOT NULL DEFAULT 3")
            self._ensure_column(con, "next_retry_at", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(con, "locked_at", "REAL")
            self._ensure_column(con, "delivery_deadline_at", "REAL")
            self._ensure_column(con, "error_category", "TEXT")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_idempotency_key ON outbound_messages(idempotency_key)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_outbound_status_retry ON outbound_messages(status, next_retry_at, created_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_outbound_channel_recipient ON outbound_messages(channel, recipient, created_at)")
            apply_migrations(con, [Migration(1, "outbound_messages_schema_baseline", lambda _con: None)])

    @staticmethod
    def _ensure_column(con: Any, column: str, ddl: str) -> None:
        existing = {row[1] for row in con.execute("PRAGMA table_info(outbound_messages)").fetchall()}
        if column not in existing:
            con.execute(f"ALTER TABLE outbound_messages ADD COLUMN {column} {ddl}")

    def create(
        self,
        *,
        channel: str,
        recipient: str,
        payload: dict[str, Any],
        max_retries: Optional[int] = None,
        delivery_deadline_at: Optional[float] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        now = time.time()
        message_id = str(uuid.uuid4())
        idem_key = str(idempotency_key or message_id)
        retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        inserted = False
        with connect_sqlite(self.db_path) as con:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO outbound_messages(
                  id, idempotency_key, channel, recipient, payload_json, status, retry_count, max_retries,
                  next_retry_at, locked_at, delivery_deadline_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, 0, NULL, ?, ?, ?)
                """,
                (
                    message_id,
                    idem_key,
                    channel,
                    recipient,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    retries,
                    delivery_deadline_at,
                    now,
                    now,
                ),
            )
            inserted = cur.rowcount > 0
            row = con.execute("SELECT id FROM outbound_messages WHERE idempotency_key=?", (idem_key,)).fetchone()
            if not row:
                raise RuntimeError("failed to create or locate outbound message by idempotency key")
            message_id = str(row[0])
        self._metric(
            "omnidesk_outbound_messages_total",
            channel=channel,
            status="pending" if inserted else "deduplicated",
        )
        return message_id

    def claim_next(self) -> Optional[dict[str, Any]]:
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    """
                    SELECT id, idempotency_key, channel, recipient, payload_json, status, provider_message_id,
                           provider_request_id, retry_count, max_retries, next_retry_at, locked_at,
                           delivery_deadline_at, last_error, error_category, created_at, updated_at
                    FROM outbound_messages
                    WHERE status IN ('pending', 'retry') AND next_retry_at <= ?
                      AND (delivery_deadline_at IS NULL OR delivery_deadline_at >= ?)
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (now, now),
                ).fetchone()
                if not row:
                    con.execute("COMMIT")
                    return None
                con.execute(
                    "UPDATE outbound_messages SET status='running', locked_at=?, updated_at=? WHERE id=?",
                    (now, now, row[0]),
                )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return self._row(row)

    def recover_stale_running(self, *, lease_seconds: int = 300) -> int:
        now = time.time()
        cutoff = now - max(0, int(lease_seconds))
        recovered = 0
        with connect_sqlite(self.db_path) as con:
            rows = con.execute(
                "SELECT id, retry_count, max_retries FROM outbound_messages WHERE status='running' AND locked_at IS NOT NULL AND locked_at <= ?",
                (cutoff,),
            ).fetchall()
            for message_id, retry_count, max_retries in rows:
                next_count = int(retry_count) + 1
                if next_count > int(max_retries):
                    con.execute(
                        """
                        UPDATE outbound_messages
                        SET status='dead_letter', retry_count=?, next_retry_at=0, locked_at=NULL,
                            updated_at=?, last_error=?
                        WHERE id=?
                        """,
                        (next_count, now, f"stale running outbound recovered after {lease_seconds}s lease and moved to dead_letter", message_id),
                    )
                else:
                    con.execute(
                        """
                        UPDATE outbound_messages
                        SET status='retry', retry_count=?, next_retry_at=?, locked_at=NULL,
                            updated_at=?, last_error=?
                        WHERE id=?
                        """,
                        (
                            next_count,
                            now + self.base_retry_seconds * (2 ** max(0, next_count - 1)),
                            now,
                            f"stale running outbound recovered after {lease_seconds}s lease",
                            message_id,
                        ),
                    )
                recovered += 1
        if recovered:
            self._metric("omnidesk_outbound_stale_recovered_total")
        return recovered

    def mark_sent(self, message_id: str, *, provider_message_id: Optional[str] = None, provider_request_id: Optional[str] = None) -> None:
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                UPDATE outbound_messages
                SET status='sent', provider_message_id=?, provider_request_id=?, locked_at=NULL, updated_at=?, last_error=NULL
                WHERE id=?
                """,
                (provider_message_id, provider_request_id, now, message_id),
            )
        self._metric("omnidesk_outbound_messages_total", status="sent")

    def mark_sent_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        provider_message_id: Optional[str] = None,
        provider_request_id: Optional[str] = None,
    ) -> None:
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                UPDATE outbound_messages
                SET status='sent', provider_message_id=?, provider_request_id=?, locked_at=NULL, updated_at=?, last_error=NULL
                WHERE idempotency_key=?
                """,
                (provider_message_id, provider_request_id, now, idempotency_key),
            )
        self._metric("omnidesk_outbound_messages_total", status="sent")

    def mark_failed(self, message_id: str, error: str, *, dead_letter: bool = False, category: str = "unknown") -> dict[str, Any]:
        now = time.time()
        error_text = str(error)[:4000]
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT retry_count, max_retries FROM outbound_messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                raise KeyError(message_id)
            retry_count = int(row[0]) + 1
            max_retries = int(row[1])
            if dead_letter or retry_count > max_retries:
                status = "dead_letter"
                next_retry_at = 0.0
            else:
                status = "retry"
                next_retry_at = now + self.base_retry_seconds * (2 ** max(0, retry_count - 1))
            con.execute(
                """
                UPDATE outbound_messages
                SET status=?, retry_count=?, next_retry_at=?, locked_at=NULL, last_error=?, error_category=?, updated_at=?
                WHERE id=?
                """,
                (status, retry_count, next_retry_at, error_text, category, now, message_id),
            )
        self._metric("omnidesk_outbound_messages_total", status=status, category=category)
        return {"id": message_id, "status": status, "retry_count": retry_count, "next_retry_at": next_retry_at}


    def mark_ambiguous(
        self,
        message_id: str,
        error: str,
        *,
        category: str = "ambiguous_send",
        provider_request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = time.time()
        error_text = str(error)[:4000]
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT retry_count FROM outbound_messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                raise KeyError(message_id)
            con.execute(
                """
                UPDATE outbound_messages
                SET status='ambiguous', provider_request_id=COALESCE(?, provider_request_id),
                    locked_at=NULL, next_retry_at=0, last_error=?, error_category=?, updated_at=?
                WHERE id=?
                """,
                (provider_request_id, error_text, category, now, message_id),
            )
        self._metric("omnidesk_outbound_ambiguous_send_total", category=category)
        return {"id": message_id, "status": "ambiguous", "retry_count": int(row[0]), "requires_reconciliation": True}

    def requeue(self, message_id: str) -> dict[str, Any]:
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT status FROM outbound_messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                raise KeyError(message_id)
            if row[0] == "sent":
                raise ValueError(f"sent outbound message cannot be retried: {message_id}")
            con.execute(
                """
                UPDATE outbound_messages
                SET status='pending', retry_count=0, next_retry_at=0, locked_at=NULL, last_error=NULL, error_category=NULL, updated_at=?
                WHERE id=?
                """,
                (now, message_id),
            )
        self._metric("omnidesk_outbound_requeued_total")
        return {"id": message_id, "status": "pending"}

    def cancel(self, message_id: str) -> dict[str, Any]:
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT status FROM outbound_messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                raise KeyError(message_id)
            if row[0] in {"sent", "cancelled"}:
                raise ValueError(f"outbound message cannot be cancelled from status {row[0]}: {message_id}")
            con.execute(
                """
                UPDATE outbound_messages
                SET status='cancelled', locked_at=NULL, updated_at=?, last_error=NULL, error_category=NULL
                WHERE id=?
                """,
                (now, message_id),
            )
        self._metric("omnidesk_outbound_cancelled_total")
        return {"id": message_id, "status": "cancelled"}

    def get(self, message_id: str) -> Optional[dict[str, Any]]:
        with connect_sqlite(self.db_path) as con:
            row = con.execute(self._select_sql() + " WHERE id=?", (message_id,)).fetchone()
        return self._row(row) if row else None

    def find_by_idempotency_key(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        with connect_sqlite(self.db_path) as con:
            row = con.execute(self._select_sql() + " WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return self._row(row) if row else None

    def list(self, *, status: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if status:
            sql = self._select_sql() + " WHERE status=? ORDER BY created_at DESC LIMIT ?"
            params: tuple[Any, ...] = (status, limit)
        else:
            sql = self._select_sql() + " ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        with connect_sqlite(self.db_path) as con:
            rows = con.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]


    def list_ambiguous(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.list(status="ambiguous", limit=limit)

    def stats(self) -> dict[str, int]:
        with connect_sqlite(self.db_path) as con:
            rows = con.execute("SELECT status, COUNT(*) FROM outbound_messages GROUP BY status").fetchall()
        return {str(status): int(count) for status, count in rows}

    @staticmethod
    def _select_sql() -> str:
        return """
            SELECT id, idempotency_key, channel, recipient, payload_json, status, provider_message_id,
                   provider_request_id, retry_count, max_retries, next_retry_at, locked_at,
                   delivery_deadline_at, last_error, error_category, created_at, updated_at
            FROM outbound_messages
        """

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        keys = [
            "id", "idempotency_key", "channel", "recipient", "payload_json", "status", "provider_message_id",
            "provider_request_id", "retry_count", "max_retries", "next_retry_at", "locked_at",
            "delivery_deadline_at", "last_error", "error_category", "created_at", "updated_at",
        ]
        return dict(zip(keys, row))

    def _metric(self, name: str, **labels: Any) -> None:
        inc = getattr(getattr(self, "metrics", None), "inc", None)
        if callable(inc):
            inc(name, **labels)
