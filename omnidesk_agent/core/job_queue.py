from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Union

from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.storage.sqlite import connect_sqlite
from omnidesk_agent.storage.migrations import Migration, apply_migrations


TERMINAL_STATUSES = {"completed", "dead_letter"}
ACTIVE_STATUSES = {"pending", "retry", "running"}


class JobQueue:
    """SQLite-backed async job queue for webhook ingestion.

    Webhooks should acknowledge quickly after signature/replay checks. The worker
    then executes the orchestrator outside the provider request lifecycle, with
    retry/dead-letter state persisted locally.
    """

    def __init__(self, db_path: Path, *, max_retries: int = 3, base_retry_seconds: int = 30):
        self.db_path = db_path.expanduser()
        self.max_retries = max_retries
        self.base_retry_seconds = base_retry_seconds
        self.metrics: Any = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,
                  dedupe_key TEXT NOT NULL UNIQUE,
                  channel TEXT NOT NULL,
                  message_id TEXT,
                  source_key TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  retry_count INTEGER NOT NULL DEFAULT 0,
                  max_retries INTEGER NOT NULL DEFAULT 3,
                  next_retry_at REAL NOT NULL DEFAULT 0,
                  locked_at REAL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  last_error TEXT,
                  result_json TEXT
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_retry ON jobs(status, next_retry_at, created_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_channel_source ON jobs(channel, source_key, created_at)")
            apply_migrations(con, [Migration(1, "job_queue_schema_baseline", lambda _con: None)])

    def enqueue(self, message: ChannelMessage, *, source_key: Optional[str] = None) -> dict[str, Any]:
        now = time.time()
        source = source_key or message.thread_id or message.sender_id or "unknown"
        payload_json = self._message_to_json(message)
        dedupe_key = self._dedupe_key(message.channel, source, message.message_id, payload_json)
        job_id = str(uuid.uuid4())
        with connect_sqlite(self.db_path) as con:
            try:
                con.execute(
                    """
                    INSERT INTO jobs (
                      id, dedupe_key, channel, message_id, source_key, payload_json,
                      status, retry_count, max_retries, next_retry_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, 0, ?, ?)
                    """,
                    (job_id, dedupe_key, message.channel, message.message_id, source, payload_json, self.max_retries, now, now),
                )
                created = True
            except sqlite3.IntegrityError:
                row = con.execute("SELECT id, status FROM jobs WHERE dedupe_key=?", (dedupe_key,)).fetchone()
                if not row:
                    raise
                job_id = row[0]
                created = False
        self._metric("omnidesk_jobs_enqueued_total", channel=message.channel, created=str(created).lower())
        return {"job_id": job_id, "created": created, "dedupe_key": dedupe_key}

    def claim_next(self) -> Optional[dict[str, Any]]:
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    """
                    SELECT id, channel, message_id, source_key, payload_json, status,
                           retry_count, max_retries, created_at, updated_at
                    FROM jobs
                    WHERE status IN ('pending', 'retry') AND next_retry_at <= ?
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if not row:
                    con.execute("COMMIT")
                    return None
                con.execute(
                    "UPDATE jobs SET status='running', locked_at=?, updated_at=? WHERE id=?",
                    (now, now, row[0]),
                )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return {
            "id": row[0],
            "channel": row[1],
            "message_id": row[2],
            "source_key": row[3],
            "payload_json": row[4],
            "status": row[5],
            "retry_count": row[6],
            "max_retries": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }


    def recover_stale_running(self, *, lease_seconds: int = 300) -> int:
        """Recover worker-crashed jobs whose running lease has expired.

        Jobs claimed by a worker are marked running with locked_at. If the
        process dies before complete/fail, they would otherwise remain stuck.
        Recovery increments retry_count and either schedules a retry or moves
        the job to dead_letter when max_retries is exhausted.
        """
        now = time.time()
        cutoff = now - max(0, int(lease_seconds))
        recovered = 0
        dead_lettered = 0
        with connect_sqlite(self.db_path) as con:
            rows = con.execute(
                "SELECT id, retry_count, max_retries FROM jobs WHERE status='running' AND locked_at IS NOT NULL AND locked_at <= ?",
                (cutoff,),
            ).fetchall()
            for job_id, retry_count, max_retries in rows:
                next_count = int(retry_count) + 1
                if next_count > int(max_retries):
                    con.execute(
                        """
                        UPDATE jobs
                        SET status='dead_letter', retry_count=?, next_retry_at=0, locked_at=NULL,
                            updated_at=?, last_error=?
                        WHERE id=?
                        """,
                        (next_count, now, f"stale running job recovered after {lease_seconds}s lease and moved to dead_letter", job_id),
                    )
                    dead_lettered += 1
                else:
                    con.execute(
                        """
                        UPDATE jobs
                        SET status='retry', retry_count=?, next_retry_at=?, locked_at=NULL,
                            updated_at=?, last_error=?
                        WHERE id=?
                        """,
                        (
                            next_count,
                            now + self.base_retry_seconds * (2 ** max(0, next_count - 1)),
                            now,
                            f"stale running job recovered after {lease_seconds}s lease",
                            job_id,
                        ),
                    )
                recovered += 1
        if recovered:
            self._metric("omnidesk_jobs_stale_recovered_total", dead_lettered=str(dead_lettered).lower())
        return recovered

    def complete(self, job_id: str, result: Any = None) -> None:
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            con.execute(
                "UPDATE jobs SET status='completed', result_json=?, locked_at=NULL, updated_at=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False, default=str), now, job_id),
            )
        self._metric("omnidesk_jobs_completed_total")

    def fail(self, job_id: str, error: Union[BaseException, str]) -> dict[str, Any]:
        now = time.time()
        error_text = str(error)[:4000]
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT retry_count, max_retries FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            retry_count = int(row[0]) + 1
            max_retries = int(row[1])
            if retry_count > max_retries:
                status = "dead_letter"
                next_retry_at = 0.0
            else:
                status = "retry"
                next_retry_at = now + self.base_retry_seconds * (2 ** max(0, retry_count - 1))
            con.execute(
                """
                UPDATE jobs
                SET status=?, retry_count=?, next_retry_at=?, locked_at=NULL, updated_at=?, last_error=?
                WHERE id=?
                """,
                (status, retry_count, next_retry_at, now, error_text, job_id),
            )
        self._metric("omnidesk_jobs_failed_total", status=status)
        if status == "dead_letter":
            self._metric("omnidesk_jobs_dead_lettered_total")
        return {"job_id": job_id, "status": status, "retry_count": retry_count, "next_retry_at": next_retry_at}

    def list_dead_letters(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.list(status="dead_letter", limit=limit)

    def requeue_dead_letter(self, job_id: str) -> dict[str, Any]:
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if row[0] != "dead_letter":
                raise ValueError(f"job is not dead_letter: {job_id}")
            con.execute(
                "UPDATE jobs SET status='pending', retry_count=0, next_retry_at=0, locked_at=NULL, updated_at=?, last_error=NULL WHERE id=?",
                (now, job_id),
            )
        self._metric("omnidesk_jobs_dead_letter_requeued_total")
        return {"job_id": job_id, "status": "pending"}

    def purge_dead_letter(self, job_id: str) -> dict[str, Any]:
        with connect_sqlite(self.db_path) as con:
            cur = con.execute("DELETE FROM jobs WHERE id=? AND status='dead_letter'", (job_id,))
            if cur.rowcount == 0:
                exists = con.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not exists:
                    raise KeyError(job_id)
                raise ValueError(f"job is not dead_letter: {job_id}")
        self._metric("omnidesk_jobs_dead_letter_purged_total")
        return {"job_id": job_id, "purged": True}

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        with connect_sqlite(self.db_path) as con:
            row = con.execute(
                """
                SELECT id, dedupe_key, channel, message_id, source_key, payload_json, status,
                       retry_count, max_retries, next_retry_at, locked_at, created_at,
                       updated_at, last_error, result_json
                FROM jobs WHERE id=?
                """,
                (job_id,),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, *, status: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        params: tuple[Any, ...]
        if status:
            sql = """
                SELECT id, dedupe_key, channel, message_id, source_key, payload_json, status,
                       retry_count, max_retries, next_retry_at, locked_at, created_at,
                       updated_at, last_error, result_json
                FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?
            """
            params = (status, limit)
        else:
            sql = """
                SELECT id, dedupe_key, channel, message_id, source_key, payload_json, status,
                       retry_count, max_retries, next_retry_at, locked_at, created_at,
                       updated_at, last_error, result_json
                FROM jobs ORDER BY created_at DESC LIMIT ?
            """
            params = (limit,)
        with connect_sqlite(self.db_path) as con:
            rows = con.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with connect_sqlite(self.db_path) as con:
            rows = con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        return {str(status): int(count) for status, count in rows}

    def _metric(self, name: str, **labels: Any) -> None:
        metrics = getattr(self, "metrics", None)
        inc = getattr(metrics, "inc", None)
        if callable(inc):
            inc(name, **labels)

    @staticmethod
    def message_from_payload(payload_json: str) -> ChannelMessage:
        payload = json.loads(payload_json)
        return ChannelMessage(
            channel=str(payload["channel"]),
            sender_id=str(payload["sender_id"]),
            text=str(payload.get("text") or ""),
            thread_id=payload.get("thread_id"),
            message_id=payload.get("message_id"),
            raw=payload.get("raw") or {},
            received_at=float(payload.get("received_at") or time.time()),
        )

    @staticmethod
    def _message_to_json(message: ChannelMessage) -> str:
        return json.dumps(asdict(message), ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _dedupe_key(channel: str, source_key: str, message_id: Optional[str], payload_json: str) -> str:
        if message_id:
            seed = f"{channel}:{source_key}:{message_id}"
        else:
            seed = f"{channel}:{source_key}:{hashlib.sha256(payload_json.encode('utf-8')).hexdigest()}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        keys = [
            "id", "dedupe_key", "channel", "message_id", "source_key", "payload_json", "status",
            "retry_count", "max_retries", "next_retry_at", "locked_at", "created_at", "updated_at",
            "last_error", "result_json",
        ]
        return dict(zip(keys, row))
