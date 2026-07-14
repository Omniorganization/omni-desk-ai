from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Iterable

from omnidesk_agent.appsync.postgres_store import NORMALIZED_SCHEMA_SQL

MIGRATION_TABLE = "omnidesk_appsync_schema_migrations"
LATEST_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class PostgresMigration:
    version: int
    name: str
    statements: tuple[str, ...]


def _split_sql(sql: str) -> tuple[str, ...]:
    """Split the repository's DDL bundle into simple migration statements."""
    return tuple(statement.strip() for statement in sql.split(";") if statement.strip())


CHAT_REQUESTS_SQL = """
CREATE TABLE IF NOT EXISTS omnidesk_appsync_chat_requests (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at DOUBLE PRECISION,
    last_sequence BIGINT NOT NULL DEFAULT 0,
    response JSONB NOT NULL DEFAULT '{}'::jsonb,
    error JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, organization_id, actor, endpoint, idempotency_key),
    CONSTRAINT omnidesk_chat_request_status_check CHECK (
        status IN ('reserved', 'running', 'finalizing', 'completed', 'failed', 'interrupted')
    )
)
"""

CHAT_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS omnidesk_appsync_chat_stream_events (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    sequence BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(namespace, organization_id, actor, endpoint, idempotency_key, sequence),
    FOREIGN KEY(namespace, organization_id, actor, endpoint, idempotency_key)
        REFERENCES omnidesk_appsync_chat_requests(
            namespace, organization_id, actor, endpoint, idempotency_key
        ) ON DELETE CASCADE
)
"""

MIGRATIONS: tuple[PostgresMigration, ...] = (
    PostgresMigration(1, "normalized_appsync_baseline", _split_sql(NORMALIZED_SCHEMA_SQL)),
    PostgresMigration(
        2,
        "atomic_chat_requests_and_append_only_events",
        (
            CHAT_REQUESTS_SQL,
            CHAT_EVENTS_SQL,
            "CREATE INDEX IF NOT EXISTS omnidesk_chat_requests_status_lease_idx ON omnidesk_appsync_chat_requests(namespace, status, lease_expires_at)",
            "CREATE INDEX IF NOT EXISTS omnidesk_chat_events_replay_idx ON omnidesk_appsync_chat_stream_events(namespace, organization_id, actor, endpoint, idempotency_key, sequence)",
        ),
    ),
    PostgresMigration(
        3,
        "production_query_indexes",
        (
            "CREATE INDEX IF NOT EXISTS omnidesk_messages_conversation_created_idx ON omnidesk_appsync_messages(namespace, organization_id, conversation_id, created_at)",
            "CREATE INDEX IF NOT EXISTS omnidesk_conversations_actor_updated_idx ON omnidesk_appsync_conversations(namespace, organization_id, actor, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS omnidesk_notifications_actor_created_idx ON omnidesk_appsync_notifications(namespace, organization_id, actor, created_at DESC)",
        ),
    ),
)


def _advisory_lock_key(namespace: str) -> int:
    digest = hashlib.sha256(f"omnidesk-appsync-migration:{namespace}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _ensure_migration_table(cur: Any) -> None:
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
            namespace TEXT NOT NULL,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(namespace, version)
        )
        """
    )


def _checksum(migration: PostgresMigration) -> str:
    return hashlib.sha256("\n;\n".join(migration.statements).encode()).hexdigest()


def apply_appsync_migrations(dsn: str, *, namespace: str = "production", migrations: Iterable[PostgresMigration] = MIGRATIONS) -> list[int]:
    if not str(dsn or "").strip():
        raise RuntimeError("PostgreSQL DSN is required for AppSync migration")
    try:
        import psycopg  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("psycopg is required for AppSync migration") from exc
    ordered = sorted(migrations, key=lambda item: item.version)
    applied_now: list[int] = []
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_advisory_lock_key(namespace),))
        _ensure_migration_table(cur)
        cur.execute(f"SELECT version, checksum FROM {MIGRATION_TABLE} WHERE namespace=%s", (namespace,))
        applied = {int(row[0]): str(row[1]) for row in cur.fetchall()}
        for migration in ordered:
            checksum = _checksum(migration)
            if migration.version in applied:
                if applied[migration.version] != checksum:
                    raise RuntimeError(f"AppSync migration {migration.version} checksum changed after application")
                continue
            for statement in migration.statements:
                cur.execute(statement)
            cur.execute(
                f"INSERT INTO {MIGRATION_TABLE}(namespace,version,name,checksum,applied_at) VALUES(%s,%s,%s,%s,%s)",
                (namespace, migration.version, migration.name, checksum, time.time()),
            )
            applied_now.append(migration.version)
        conn.commit()
    return applied_now


def assert_appsync_schema_current(conn: Any, *, namespace: str) -> None:
    with conn.cursor() as cur:
        try:
            cur.execute(f"SELECT COALESCE(MAX(version),0) FROM {MIGRATION_TABLE} WHERE namespace=%s", (namespace,))
            version = int(cur.fetchone()[0])
        except Exception as exc:
            raise RuntimeError("AppSync schema is not initialized; run `python -m omnidesk_agent.appsync.migrate`") from exc
    if version < LATEST_SCHEMA_VERSION:
        raise RuntimeError(f"AppSync schema version {version} is behind required version {LATEST_SCHEMA_VERSION}")


def appsync_schema_status(conn: Any, *, namespace: str) -> dict[str, int | bool]:
    with conn.cursor() as cur:
        try:
            cur.execute(f"SELECT COALESCE(MAX(version),0) FROM {MIGRATION_TABLE} WHERE namespace=%s", (namespace,))
            current = int(cur.fetchone()[0])
        except Exception:
            current = 0
    return {"current": current, "required": LATEST_SCHEMA_VERSION, "ready": current >= LATEST_SCHEMA_VERSION}
