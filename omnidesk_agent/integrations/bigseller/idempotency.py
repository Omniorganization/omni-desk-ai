from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any

from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import BigSellerConfigurationError
from omnidesk_agent.integrations.bigseller.schemas import utc_now
from omnidesk_agent.storage.sqlite import connect_sqlite


@dataclass(frozen=True)
class BigSellerIdempotencyKey:
    external_id: str
    store_id: str
    action_type: str

    @property
    def value(self) -> str:
        return f"{self.store_id}:{self.external_id}:{self.action_type}"


class BigSellerIdempotencyGuard:
    """Process-local idempotency guard for mock and unit-test operation only."""

    backend = "memory"

    def __init__(self, *, ttl_seconds: int = 86400):
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._records: dict[str, dict[str, object]] = {}
        self._lock = Lock()

    def _expiry(self) -> str:
        return (utc_now() + timedelta(seconds=self.ttl_seconds)).isoformat()

    def purge_expired(self) -> int:
        now = utc_now().isoformat()
        with self._lock:
            expired = [
                key
                for key, record in self._records.items()
                if str(record.get("expires_at") or "") <= now
            ]
            for key in expired:
                self._records.pop(key, None)
            return len(expired)

    def claim(self, *, external_id: str, store_id: str, action_type: str) -> bool:
        self.purge_expired()
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        now = utc_now().isoformat()
        with self._lock:
            existing = self._records.get(key)
            if existing is not None and existing.get("status") in {
                "in_progress",
                "completed",
            }:
                return False
            self._records[key] = {
                "external_id": external_id,
                "store_id": store_id,
                "action_type": action_type,
                "status": "in_progress",
                "created_at": existing.get("created_at") if existing else now,
                "updated_at": now,
                "expires_at": self._expiry(),
            }
            return True

    def complete(self, *, external_id: str, store_id: str, action_type: str) -> None:
        self._set_status(
            external_id=external_id,
            store_id=store_id,
            action_type=action_type,
            status="completed",
        )

    def release(self, *, external_id: str, store_id: str, action_type: str) -> None:
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        with self._lock:
            self._records.pop(key, None)

    def _set_status(
        self, *, external_id: str, store_id: str, action_type: str, status: str
    ) -> None:
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        with self._lock:
            record = self._records.setdefault(
                key,
                {
                    "external_id": external_id,
                    "store_id": store_id,
                    "action_type": action_type,
                    "created_at": utc_now().isoformat(),
                },
            )
            record["status"] = status
            record["updated_at"] = utc_now().isoformat()
            record["expires_at"] = self._expiry()

    def stats(self) -> dict[str, int | str | bool]:
        self.purge_expired()
        with self._lock:
            values = list(self._records.values())
        return {
            "backend": self.backend,
            "durable": False,
            "ttl_seconds": self.ttl_seconds,
            "total": len(values),
            "completed": sum(1 for item in values if item.get("status") == "completed"),
            "in_progress": sum(
                1 for item in values if item.get("status") == "in_progress"
            ),
        }


class SQLiteBigSellerIdempotencyGuard(BigSellerIdempotencyGuard):
    """SQLite-backed idempotency guard for restart-safe single-node deployments."""

    backend = "sqlite"

    def __init__(self, db_path: Path, *, ttl_seconds: int = 86400):
        self.db_path = db_path.expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._lock = Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = connect_sqlite(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _expiry(self) -> str:
        return (utc_now() + timedelta(seconds=self.ttl_seconds)).isoformat()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bigseller_idempotency_records (
                    key TEXT PRIMARY KEY,
                    external_id TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(bigseller_idempotency_records)"
                )
            }
            if "expires_at" not in columns:
                conn.execute(
                    "ALTER TABLE bigseller_idempotency_records ADD COLUMN expires_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'"
                )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bigseller_idempotency_status
                ON bigseller_idempotency_records(status)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bigseller_idempotency_expires_at
                ON bigseller_idempotency_records(expires_at)
                """
            )

    def purge_expired(self) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM bigseller_idempotency_records WHERE expires_at <= ?",
                (utc_now().isoformat(),),
            )
            return int(cursor.rowcount or 0)

    def claim(self, *, external_id: str, store_id: str, action_type: str) -> bool:
        self.purge_expired()
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        now = utc_now().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status, created_at FROM bigseller_idempotency_records WHERE key = ?",
                (key,),
            ).fetchone()
            if existing is not None and existing["status"] in {
                "in_progress",
                "completed",
            }:
                return False
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO bigseller_idempotency_records
                    (key, external_id, store_id, action_type, status, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at
                """,
                (
                    key,
                    external_id,
                    store_id,
                    action_type,
                    "in_progress",
                    created_at,
                    now,
                    self._expiry(),
                ),
            )
            return True

    def complete(self, *, external_id: str, store_id: str, action_type: str) -> None:
        self._set_status(
            external_id=external_id,
            store_id=store_id,
            action_type=action_type,
            status="completed",
        )

    def release(self, *, external_id: str, store_id: str, action_type: str) -> None:
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM bigseller_idempotency_records WHERE key = ?", (key,)
            )

    def _set_status(
        self, *, external_id: str, store_id: str, action_type: str, status: str
    ) -> None:
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        now = utc_now().isoformat()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM bigseller_idempotency_records WHERE key = ?",
                (key,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO bigseller_idempotency_records
                    (key, external_id, store_id, action_type, status, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at
                """,
                (
                    key,
                    external_id,
                    store_id,
                    action_type,
                    status,
                    created_at,
                    now,
                    self._expiry(),
                ),
            )

    def stats(self) -> dict[str, int | str | bool]:
        self.purge_expired()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM bigseller_idempotency_records GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        total = sum(counts.values())
        return {
            "backend": self.backend,
            "durable": True,
            "ttl_seconds": self.ttl_seconds,
            "total": total,
            "completed": counts.get("completed", 0),
            "in_progress": counts.get("in_progress", 0),
        }


class PostgresBigSellerIdempotencyGuard(BigSellerIdempotencyGuard):
    """PostgreSQL-backed guard for horizontally scaled production deployments."""

    backend = "postgres"

    def __init__(self, dsn: str, *, ttl_seconds: int = 86400):
        if not dsn:
            raise BigSellerConfigurationError(
                "BIGSELLER_POSTGRES_DSN is required for BigSeller Postgres state"
            )
        try:
            import psycopg  # type: ignore[import-not-found]
        except Exception as exc:
            raise BigSellerConfigurationError(
                "psycopg is required for BIGSELLER_STATE_BACKEND=postgres"
            ) from exc
        self._psycopg = psycopg
        self.dsn = dsn
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._ensure_schema()

    def _connect(self) -> Any:
        return self._psycopg.connect(self.dsn)

    def _expiry(self):
        return utc_now() + timedelta(seconds=self.ttl_seconds)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bigseller_idempotency_records (
                        key TEXT PRIMARY KEY,
                        external_id TEXT NOT NULL,
                        store_id TEXT NOT NULL,
                        action_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL,
                        expires_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE bigseller_idempotency_records
                    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_bigseller_idempotency_status
                    ON bigseller_idempotency_records(status)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_bigseller_idempotency_expires_at
                    ON bigseller_idempotency_records(expires_at)
                    """
                )

    def purge_expired(self) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM bigseller_idempotency_records WHERE expires_at <= %s",
                    (utc_now(),),
                )
                return int(cur.rowcount or 0)

    def claim(self, *, external_id: str, store_id: str, action_type: str) -> bool:
        self.purge_expired()
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        now = utc_now()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, created_at
                    FROM bigseller_idempotency_records
                    WHERE key = %s
                    FOR UPDATE
                    """,
                    (key,),
                )
                row = cur.fetchone()
                if row is not None and row[0] in {"in_progress", "completed"}:
                    return False
                created_at = row[1] if row else now
                cur.execute(
                    """
                    INSERT INTO bigseller_idempotency_records
                        (key, external_id, store_id, action_type, status, created_at, updated_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(key) DO UPDATE SET
                        status=EXCLUDED.status,
                        updated_at=EXCLUDED.updated_at,
                        expires_at=EXCLUDED.expires_at
                    """,
                    (
                        key,
                        external_id,
                        store_id,
                        action_type,
                        "in_progress",
                        created_at,
                        now,
                        self._expiry(),
                    ),
                )
        return True

    def complete(self, *, external_id: str, store_id: str, action_type: str) -> None:
        self._set_status(
            external_id=external_id,
            store_id=store_id,
            action_type=action_type,
            status="completed",
        )

    def release(self, *, external_id: str, store_id: str, action_type: str) -> None:
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM bigseller_idempotency_records WHERE key = %s", (key,)
                )

    def _set_status(
        self, *, external_id: str, store_id: str, action_type: str, status: str
    ) -> None:
        key = BigSellerIdempotencyKey(
            external_id=external_id, store_id=store_id, action_type=action_type
        ).value
        now = utc_now()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bigseller_idempotency_records
                        (key, external_id, store_id, action_type, status, created_at, updated_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(key) DO UPDATE SET
                        status=EXCLUDED.status,
                        updated_at=EXCLUDED.updated_at,
                        expires_at=EXCLUDED.expires_at
                    """,
                    (
                        key,
                        external_id,
                        store_id,
                        action_type,
                        status,
                        now,
                        now,
                        self._expiry(),
                    ),
                )

    def stats(self) -> dict[str, int | str | bool]:
        self.purge_expired()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, COUNT(*) FROM bigseller_idempotency_records GROUP BY status"
                )
                rows = cur.fetchall()
        counts = {str(status): int(count) for status, count in rows}
        total = sum(counts.values())
        return {
            "backend": self.backend,
            "durable": True,
            "ttl_seconds": self.ttl_seconds,
            "total": total,
            "completed": counts.get("completed", 0),
            "in_progress": counts.get("in_progress", 0),
        }


def create_bigseller_idempotency_guard(
    config: BigSellerConfig,
) -> BigSellerIdempotencyGuard:
    if config.state_backend == "memory":
        if not config.use_mock:
            raise BigSellerConfigurationError(
                "BIGSELLER_STATE_BACKEND=memory is not allowed for BigSeller real mode"
            )
        return BigSellerIdempotencyGuard(ttl_seconds=config.webhook_event_ttl_seconds)
    if config.state_backend == "postgres":
        return PostgresBigSellerIdempotencyGuard(
            config.postgres_dsn or "", ttl_seconds=config.webhook_event_ttl_seconds
        )
    return SQLiteBigSellerIdempotencyGuard(
        config.state_db_path, ttl_seconds=config.webhook_event_ttl_seconds
    )
