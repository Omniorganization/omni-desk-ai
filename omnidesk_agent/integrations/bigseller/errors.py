from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from threading import Lock
from typing import Any, Optional

from omnidesk_agent.integrations.bigseller.schemas import BigSellerQueuedError, utc_now


SECRET_KEY_RE = re.compile(
    r"(token|secret|password|app[_-]?key|authorization|cookie)", re.IGNORECASE
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(access[_-]?token|refresh[_-]?token|app[_-]?key|secret|authorization)\s*[:=]\s*[^,}]+"
)


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if SECRET_KEY_RE.search(str(key))
            else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE_RE.sub(
            lambda match: (
                match.group(0).split(match.group(1), 1)[0]
                + match.group(1)
                + "=[REDACTED]"
            ),
            value,
        )
    return value


class BigSellerError(Exception):
    code = "bigseller_error"

    def __init__(self, message: str, *, code: Optional[str] = None):
        self.safe_message = str(redact_secrets(message))
        if code:
            self.code = code
        super().__init__(self.safe_message)


class BigSellerDisabledError(BigSellerError):
    code = "bigseller_disabled"


class BigSellerConfigurationError(BigSellerError):
    code = "bigseller_configuration_error"


class BigSellerAuthenticationError(BigSellerError):
    code = "bigseller_authentication_error"


class BigSellerUnauthorizedError(BigSellerAuthenticationError):
    code = "bigseller_unauthorized"


class BigSellerRateLimitError(BigSellerError):
    code = "bigseller_rate_limited"


class BigSellerEndpointNotConfigured(BigSellerConfigurationError):
    code = "bigseller_endpoint_not_configured"


class BigSellerRetryableError(BigSellerError):
    code = "bigseller_retryable_error"


class BigSellerSyncErrorQueue:
    """Process-local retry/dead-letter queue for mock and unit-test operation."""

    backend = "memory"

    def __init__(self, *, max_retries: int = 3):
        self.max_retries = max(0, int(max_retries))
        self._items: dict[str, BigSellerQueuedError] = {}
        self._lock = Lock()

    @staticmethod
    def _key(entity_type: str, external_id: str, store_id: str, action: str) -> str:
        digest = hashlib.sha256(
            f"{entity_type}:{store_id}:{external_id}:{action}".encode("utf-8")
        ).hexdigest()
        return digest[:32]

    def enqueue(
        self,
        *,
        entity_type: str,
        external_id: str,
        store_id: str,
        action: str,
        payload: dict[str, Any],
        error: BaseException | str,
        error_code: str = "sync_error",
    ) -> BigSellerQueuedError:
        now = utc_now()
        error_message = str(redact_secrets(str(error)))[:1000]
        item_id = self._key(entity_type, external_id, store_id, action)
        with self._lock:
            existing = self._items.get(item_id)
            retry_count = 1 if existing is None else existing.retry_count + 1
            status = "dead_letter" if retry_count > self.max_retries else "retryable"
            next_retry_at = (
                now + timedelta(seconds=30 * (2 ** max(0, retry_count - 1)))
                if status == "retryable"
                else now
            )
            item = BigSellerQueuedError(
                id=existing.id if existing else str(uuid.uuid4()),
                entity_type=entity_type,
                external_id=external_id,
                store_id=store_id,
                action=action,
                payload=redact_secrets(payload),
                status=status,
                retry_count=retry_count,
                max_retries=self.max_retries,
                error_code=error_code,
                error_message=error_message,
                next_retry_at=next_retry_at,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self._items[item_id] = item
            return item

    def resolve(
        self, *, entity_type: str, external_id: str, store_id: str, action: str
    ) -> None:
        with self._lock:
            item = self._items.get(
                self._key(entity_type, external_id, store_id, action)
            )
            if item is not None:
                item.status = "resolved"
                item.updated_at = utc_now()

    def list(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[BigSellerQueuedError]:
        with self._lock:
            items = sorted(
                self._items.values(), key=lambda item: item.updated_at, reverse=True
            )
        if status:
            items = [item for item in items if item.status == status]
        return items[: max(0, limit)]

    def stats(self) -> dict[str, int | str | bool]:
        with self._lock:
            items = list(self._items.values())
        return {
            "backend": self.backend,
            "durable": False,
            "retryable": sum(1 for item in items if item.status == "retryable"),
            "dead_letter": sum(1 for item in items if item.status == "dead_letter"),
            "resolved": sum(1 for item in items if item.status == "resolved"),
            "total": len(items),
        }


class SQLiteBigSellerSyncErrorQueue(BigSellerSyncErrorQueue):
    """SQLite-backed retry/dead-letter queue for restart-safe operation."""

    backend = "sqlite"

    def __init__(self, db_path: Path, *, max_retries: int = 3):
        self.db_path = db_path.expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_retries = max(0, int(max_retries))
        self._lock = Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bigseller_sync_errors (
                    key TEXT PRIMARY KEY,
                    id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL,
                    max_retries INTEGER NOT NULL,
                    error_code TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    next_retry_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bigseller_sync_errors_status
                ON bigseller_sync_errors(status)
                """
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> BigSellerQueuedError:
        return BigSellerQueuedError(
            id=row["id"],
            entity_type=row["entity_type"],
            external_id=row["external_id"],
            store_id=row["store_id"],
            action=row["action"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            retry_count=int(row["retry_count"]),
            max_retries=int(row["max_retries"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            next_retry_at=row["next_retry_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def enqueue(
        self,
        *,
        entity_type: str,
        external_id: str,
        store_id: str,
        action: str,
        payload: dict[str, Any],
        error: BaseException | str,
        error_code: str = "sync_error",
    ) -> BigSellerQueuedError:
        now = utc_now()
        item_key = self._key(entity_type, external_id, store_id, action)
        error_message = str(redact_secrets(str(error)))[:1000]
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM bigseller_sync_errors WHERE key = ?", (item_key,)
            ).fetchone()
            retry_count = 1 if existing is None else int(existing["retry_count"]) + 1
            status = "dead_letter" if retry_count > self.max_retries else "retryable"
            next_retry_at = (
                now + timedelta(seconds=30 * (2 ** max(0, retry_count - 1)))
                if status == "retryable"
                else now
            )
            item_id = existing["id"] if existing else str(uuid.uuid4())
            created_at = existing["created_at"] if existing else now.isoformat()
            updated_at = now.isoformat()
            payload_json = json.dumps(
                redact_secrets(payload), ensure_ascii=False, sort_keys=True
            )
            conn.execute(
                """
                INSERT INTO bigseller_sync_errors
                    (key, id, entity_type, external_id, store_id, action,
                     payload_json, status, retry_count, max_retries, error_code,
                     error_message, next_retry_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    status=excluded.status,
                    retry_count=excluded.retry_count,
                    error_code=excluded.error_code,
                    error_message=excluded.error_message,
                    next_retry_at=excluded.next_retry_at,
                    updated_at=excluded.updated_at
                """,
                (
                    item_key,
                    item_id,
                    entity_type,
                    external_id,
                    store_id,
                    action,
                    payload_json,
                    status,
                    retry_count,
                    self.max_retries,
                    error_code,
                    error_message,
                    next_retry_at.isoformat(),
                    created_at,
                    updated_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM bigseller_sync_errors WHERE key = ?", (item_key,)
            ).fetchone()
            return self._from_row(row)

    def resolve(
        self, *, entity_type: str, external_id: str, store_id: str, action: str
    ) -> None:
        item_key = self._key(entity_type, external_id, store_id, action)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE bigseller_sync_errors
                SET status = 'resolved', updated_at = ?
                WHERE key = ?
                """,
                (utc_now().isoformat(), item_key),
            )

    def list(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[BigSellerQueuedError]:
        sql = "SELECT * FROM bigseller_sync_errors"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params = (*params, max(0, limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def stats(self) -> dict[str, int | str | bool]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM bigseller_sync_errors GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "backend": self.backend,
            "durable": True,
            "retryable": counts.get("retryable", 0),
            "dead_letter": counts.get("dead_letter", 0),
            "resolved": counts.get("resolved", 0),
            "total": sum(counts.values()),
        }


class PostgresBigSellerSyncErrorQueue(BigSellerSyncErrorQueue):
    """PostgreSQL-backed retry/dead-letter queue for multi-instance production."""

    backend = "postgres"

    def __init__(self, dsn: str, *, max_retries: int = 3):
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
        self.max_retries = max(0, int(max_retries))
        self._ensure_schema()

    def _connect(self) -> Any:
        return self._psycopg.connect(self.dsn)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bigseller_sync_errors (
                        key TEXT PRIMARY KEY,
                        id TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        store_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        payload_json JSONB NOT NULL,
                        status TEXT NOT NULL,
                        retry_count INTEGER NOT NULL,
                        max_retries INTEGER NOT NULL,
                        error_code TEXT NOT NULL,
                        error_message TEXT NOT NULL,
                        next_retry_at TIMESTAMPTZ NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_bigseller_sync_errors_status
                    ON bigseller_sync_errors(status)
                    """
                )

    def enqueue(
        self,
        *,
        entity_type: str,
        external_id: str,
        store_id: str,
        action: str,
        payload: dict[str, Any],
        error: BaseException | str,
        error_code: str = "sync_error",
    ) -> BigSellerQueuedError:
        now = utc_now()
        item_key = self._key(entity_type, external_id, store_id, action)
        error_message = str(redact_secrets(str(error)))[:1000]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, retry_count, created_at FROM bigseller_sync_errors WHERE key = %s FOR UPDATE",
                    (item_key,),
                )
                existing = cur.fetchone()
                retry_count = 1 if existing is None else int(existing[1]) + 1
                status = "dead_letter" if retry_count > self.max_retries else "retryable"
                next_retry_at = (
                    now + timedelta(seconds=30 * (2 ** max(0, retry_count - 1)))
                    if status == "retryable"
                    else now
                )
                item_id = existing[0] if existing else str(uuid.uuid4())
                created_at = existing[2] if existing else now
                safe_payload = json.dumps(
                    redact_secrets(payload), ensure_ascii=False, sort_keys=True
                )
                cur.execute(
                    """
                    INSERT INTO bigseller_sync_errors
                        (key, id, entity_type, external_id, store_id, action,
                         payload_json, status, retry_count, max_retries, error_code,
                         error_message, next_retry_at, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(key) DO UPDATE SET
                        payload_json=EXCLUDED.payload_json,
                        status=EXCLUDED.status,
                        retry_count=EXCLUDED.retry_count,
                        error_code=EXCLUDED.error_code,
                        error_message=EXCLUDED.error_message,
                        next_retry_at=EXCLUDED.next_retry_at,
                        updated_at=EXCLUDED.updated_at
                    """,
                    (
                        item_key,
                        item_id,
                        entity_type,
                        external_id,
                        store_id,
                        action,
                        safe_payload,
                        status,
                        retry_count,
                        self.max_retries,
                        error_code,
                        error_message,
                        next_retry_at,
                        created_at,
                        now,
                    ),
                )
        return BigSellerQueuedError(
            id=item_id,
            entity_type=entity_type,
            external_id=external_id,
            store_id=store_id,
            action=action,
            payload=redact_secrets(payload),
            status=status,
            retry_count=retry_count,
            max_retries=self.max_retries,
            error_code=error_code,
            error_message=error_message,
            next_retry_at=next_retry_at,
            created_at=created_at,
            updated_at=now,
        )

    def resolve(
        self, *, entity_type: str, external_id: str, store_id: str, action: str
    ) -> None:
        item_key = self._key(entity_type, external_id, store_id, action)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bigseller_sync_errors
                    SET status = 'resolved', updated_at = %s
                    WHERE key = %s
                    """,
                    (utc_now(), item_key),
                )

    def list(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[BigSellerQueuedError]:
        sql = """
            SELECT id, entity_type, external_id, store_id, action, payload_json,
                   status, retry_count, max_retries, error_code, error_message,
                   next_retry_at, created_at, updated_at
            FROM bigseller_sync_errors
        """
        params: list[Any] = []
        if status:
            sql += " WHERE status = %s"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(max(0, limit))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
        return [
            BigSellerQueuedError(
                id=row[0],
                entity_type=row[1],
                external_id=row[2],
                store_id=row[3],
                action=row[4],
                payload=row[5] if isinstance(row[5], dict) else json.loads(row[5]),
                status=row[6],
                retry_count=int(row[7]),
                max_retries=int(row[8]),
                error_code=row[9],
                error_message=row[10],
                next_retry_at=row[11],
                created_at=row[12],
                updated_at=row[13],
            )
            for row in rows
        ]

    def stats(self) -> dict[str, int | str | bool]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, COUNT(*) FROM bigseller_sync_errors GROUP BY status"
                )
                rows = cur.fetchall()
        counts = {str(status): int(count) for status, count in rows}
        return {
            "backend": self.backend,
            "durable": True,
            "retryable": counts.get("retryable", 0),
            "dead_letter": counts.get("dead_letter", 0),
            "resolved": counts.get("resolved", 0),
            "total": sum(counts.values()),
        }


def create_bigseller_error_queue(config: Any) -> BigSellerSyncErrorQueue:
    if config.state_backend == "memory":
        if not config.use_mock:
            raise BigSellerConfigurationError(
                "BIGSELLER_STATE_BACKEND=memory is not allowed for BigSeller real mode"
            )
        return BigSellerSyncErrorQueue(max_retries=config.max_retries)
    if config.state_backend == "postgres":
        return PostgresBigSellerSyncErrorQueue(
            config.postgres_dsn or "", max_retries=config.max_retries
        )
    return SQLiteBigSellerSyncErrorQueue(
        config.state_db_path, max_retries=config.max_retries
    )
