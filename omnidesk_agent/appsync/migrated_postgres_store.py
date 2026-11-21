"""PostgreSQL AppSync store built on per-entity transactions.

Every public method is one short database transaction that reads and locks only
the rows it needs (``SELECT ... FOR UPDATE`` on primary keys, ``SKIP LOCKED``
claims) and writes only the rows it changes. Nothing is cached between calls, so
any number of gateway replicas share one source of truth without a namespace-wide
lock or a reload of the namespace.

Sync event sequence numbers come from one counter row per namespace, updated at
the end of the writing transaction. Its row lock is held until commit, so
sequence order is commit order: a reader that sees event N has seen every event
below it, which is what lets the sync feed read incrementally.

Connections carry ``statement_timeout``, ``lock_timeout`` and
``idle_in_transaction_session_timeout`` so a stuck lock holder or a slow query
fails the request instead of hanging it.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal, Optional, TypeVar, cast

from omnidesk_agent.appsync.postgres_migrations import assert_appsync_schema_current
from omnidesk_agent.appsync.store import (
    LEGACY_HMAC_KEY_REFUSED,
    TERMINAL_TASK_STATUSES,
    AppSyncStore,
    ApprovalRecord,
    ConversationRecord,
    DeviceChallengeRecord,
    DeviceEnrollmentRecord,
    DeviceRecord,
    DeviceType,
    IdempotencyConflict,
    LocalInboxRecord,
    LocalOutboxRecord,
    MessageRecord,
    NotificationRecord,
    Organization,
    PushOutboxRecord,
    RuntimeStatusRecord,
    SyncConflictRecord,
    SyncCursorRecord,
    TaskRecord,
    TaskStatus,
    UserProfile,
    _approval_decision_message,
    _device_request_message,
    _device_signing_message,
    _fingerprint,
    _hash_secret,
    _id,
    _is_ed25519_public_key,
    _now,
    _secret_matches,
    _sha256_hex,
    _verify_device_signature,
    authorize_task_status_change,
    check_enrollment_device,
    is_legacy_hmac_device_key,
    public_device_view,
    runtime_status_changed,
    scope_sync_event_to_actor,
)

T = TypeVar("T")
ConflictPolicy = Literal["server-wins", "client-wins", "manual-review", "merge"]

# Like the in-memory store, a sync call returns at most the newest 1000 events
# after the cursor and moves the cursor to the head.
SYNC_EVENT_WINDOW = 1000
# Upper bound for list endpoints that have no pagination yet.
LIST_LIMIT = 1000

# One statement, one snapshot: the head and the window are read together. Event
# sequence numbers are assigned in commit order (see EVENT_COUNTER_SQL), so no
# uncommitted event can later appear below a committed head.
SYNC_WINDOW_SQL = """
WITH head AS (
    SELECT COALESCE(MAX(seq), 0) AS seq FROM omnidesk_appsync_sync_events WHERE namespace = %(namespace)s
), page AS (
    SELECT seq, organization_id, event_type, actor, payload, created_at
    FROM omnidesk_appsync_sync_events
    WHERE namespace = %(namespace)s AND seq > %(since)s
    ORDER BY seq DESC
    LIMIT %(limit)s
)
SELECT head.seq, page.seq, page.organization_id, page.event_type, page.actor, page.payload, page.created_at
FROM head LEFT JOIN page ON true
ORDER BY page.seq
"""

SYNC_ORGANIZATION_WINDOW_SQL = """
WITH head AS (
    SELECT COALESCE(MAX(seq), 0) AS seq FROM omnidesk_appsync_sync_events WHERE namespace = %(namespace)s
), page AS (
    SELECT seq, organization_id, event_type, actor, payload, created_at
    FROM omnidesk_appsync_sync_events
    WHERE namespace = %(namespace)s AND seq > %(since)s AND organization_id = %(organization_id)s
    ORDER BY seq DESC
    LIMIT %(limit)s
)
SELECT head.seq, page.seq, page.organization_id, page.event_type, page.actor, page.payload, page.created_at
FROM head LEFT JOIN page ON true
ORDER BY page.seq
"""

ACTOR_ORGANIZATION_SQL = "SELECT organization_id FROM omnidesk_appsync_users WHERE namespace = %s AND user_id = %s"

CONVERSATION_OWNERS_SQL = "SELECT conversation_id, actor FROM omnidesk_appsync_conversations WHERE namespace = %s AND conversation_id = ANY(%s)"

# Assigns ``n`` sequence numbers. The counter row lock is held until commit, so
# concurrent writers commit in sequence order. GREATEST() keeps the counter above
# events written by a replica of the previous release, which numbered events from
# its in-memory copy of the table (see LEGACY_EXCLUSIVE_LOCK_NOTE).
EVENT_COUNTER_SQL = """
INSERT INTO omnidesk_appsync_event_counters AS counter (namespace, seq)
VALUES (
    %(namespace)s,
    (SELECT COALESCE(MAX(seq), 0) FROM omnidesk_appsync_sync_events WHERE namespace = %(namespace)s) + %(n)s
)
ON CONFLICT (namespace) DO UPDATE
SET seq = GREATEST(
    counter.seq,
    (SELECT COALESCE(MAX(seq), 0) FROM omnidesk_appsync_sync_events WHERE namespace = %(namespace)s)
) + %(n)s
RETURNING seq
"""

INSERT_EVENT_SQL = """
INSERT INTO omnidesk_appsync_sync_events(namespace, organization_id, seq, event_type, actor, payload, created_at)
VALUES (%(namespace)s, %(organization_id)s, %(seq)s, %(event_type)s, %(actor)s, %(payload)s, %(created_at)s)
"""

# LEGACY_EXCLUSIVE_LOCK_NOTE: the previous release serialized every operation
# under the session-level pg_advisory_lock on this key, reloaded the namespace
# and re-upserted every row it held. While a rolling upgrade still runs such a
# replica, its whole-state write-back would overwrite rows this store changed
# concurrently. Writing transactions therefore take the same key in shared mode:
# they never wait for each other, only for a previous-release operation. Remove
# it once no previous-release replica can run against the database.
LEGACY_LOCK_SQL = "SELECT pg_advisory_xact_lock_shared(%s)"

# An approval authorizes exactly one claim: only a queued task with an unused
# approval is claimable, and an expired run is never re-claimed under it. Only
# task rows are locked: FOR UPDATE cannot apply to the nullable side of the join.
CLAIM_NEXT_TASK_SQL = """
WITH candidate AS (
    SELECT t.task_id
    FROM omnidesk_appsync_tasks t
    LEFT JOIN omnidesk_appsync_approvals a ON a.namespace = t.namespace AND a.approval_id = t.approval_id
    WHERE t.namespace = %(namespace)s
      AND t.organization_id = %(organization_id)s
      AND t.requires_desktop_runtime = true
      AND t.status IN ('queued', 'running')
      AND (t.status = 'queued' OR t.lease_expires_at IS NULL OR t.lease_expires_at <= %(now)s)
      AND (t.approval_id IS NULL OR (t.status = 'queued' AND a.task_id = t.task_id AND a.status = 'approved'))
    ORDER BY t.created_at ASC
    FOR UPDATE OF t SKIP LOCKED
    LIMIT 1
)
UPDATE omnidesk_appsync_tasks t
SET status = 'running', assigned_runtime_device_id = %(device_id)s, claimed_by_device_id = %(device_id)s,
    lease_expires_at = %(lease_expires_at)s, attempt_count = t.attempt_count + 1, updated_at = %(now)s
FROM candidate
WHERE t.namespace = %(namespace)s AND t.task_id = candidate.task_id
RETURNING {task_columns}
"""

CONSUME_APPROVAL_SQL = """
UPDATE omnidesk_appsync_approvals
SET status = 'consumed'
WHERE namespace = %(namespace)s AND approval_id = %(approval_id)s AND status = 'approved'
RETURNING approval_id, task_id, organization_id
"""

LEASE_RENEWAL_SQL = """
UPDATE omnidesk_appsync_tasks
SET lease_expires_at = %(lease_expires_at)s, updated_at = %(now)s
WHERE namespace = %(namespace)s AND organization_id = %(organization_id)s AND task_id = %(task_id)s
  AND claimed_by_device_id = %(device_id)s AND status = 'running'
RETURNING {task_columns}
"""

# Pushes carry task and approval text: only challenge-verified, unrevoked devices
# get them (AppSyncStore._device_receives_push). A chat reply goes to the
# conversation's actor alone.
PUSH_FANOUT_SQL = """
INSERT INTO omnidesk_appsync_push_outbox(
    namespace, organization_id, push_id, device_id, platform, push_token, audience, title, body,
    related_id, status, attempt_count, last_error, created_at, updated_at
)
SELECT d.namespace, %(organization_id)s, 'push_' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 16),
       d.device_id, d.platform, d.push_token, %(audience)s, %(title)s, %(body)s,
       %(related_id)s, 'pending', 0, NULL, %(now)s, %(now)s
FROM omnidesk_appsync_devices d
WHERE d.namespace = %(namespace)s AND d.organization_id = %(organization_id)s
  AND d.push_token IS NOT NULL AND d.push_token <> '' AND d.revoked_at IS NULL
  AND d.credential_status = 'verified' AND d.trust_level = 'challenge_verified'
  AND (%(audience)s = 'all' OR d.device_type = %(audience)s)
  AND (%(recipient)s::text IS NULL OR d.actor = %(recipient)s::text)
"""

REVOKED_DEVICE_PUSH_SQL = """
UPDATE omnidesk_appsync_push_outbox
SET push_token = NULL,
    status = CASE WHEN status = 'pending' THEN 'failed' ELSE status END,
    last_error = CASE WHEN status = 'pending' THEN 'device_revoked' ELSE last_error END,
    updated_at = CASE WHEN status = 'pending' THEN %(now)s ELSE updated_at END
WHERE namespace = %(namespace)s AND device_id = %(device_id)s
"""

INELIGIBLE_PUSH_SQL = """
UPDATE omnidesk_appsync_push_outbox p
SET status = 'failed', last_error = 'device_not_eligible', updated_at = %(now)s
WHERE p.namespace = %(namespace)s AND p.status = 'pending'
  AND NOT EXISTS (
      SELECT 1 FROM omnidesk_appsync_devices d
      WHERE d.namespace = p.namespace AND d.device_id = p.device_id
        AND d.push_token IS NOT NULL AND d.push_token <> '' AND d.revoked_at IS NULL
        AND d.credential_status = 'verified' AND d.trust_level = 'challenge_verified'
  )
"""

NONCE_SQL = """
INSERT INTO omnidesk_appsync_device_request_nonces(namespace, nonce_key, expires_at)
VALUES (%(namespace)s, %(nonce_key)s, %(expires_at)s)
ON CONFLICT (namespace, nonce_key) DO UPDATE SET expires_at = EXCLUDED.expires_at
WHERE omnidesk_appsync_device_request_nonces.expires_at <= %(now)s
RETURNING nonce_key
"""

# Every signed request adds a nonce row, so each claim also deletes a bounded
# batch of expired ones; their replay window is over. Concurrent requests skip
# each other's rows instead of waiting on them.
NONCE_PRUNE_BATCH = 100
NONCE_PRUNE_SQL = """
DELETE FROM omnidesk_appsync_device_request_nonces
WHERE ctid IN (
    SELECT ctid FROM omnidesk_appsync_device_request_nonces
    WHERE namespace = %(namespace)s AND expires_at <= %(now)s
    LIMIT %(batch)s
    FOR UPDATE SKIP LOCKED
)
"""

OPERATION_LOG_SQL = """
INSERT INTO omnidesk_appsync_operation_log(namespace, organization_id, seq, event, payload, created_at)
VALUES (%(namespace)s, %(organization_id)s, nextval('omnidesk_appsync_operation_log_entry_seq'), %(event)s, %(payload)s, %(created_at)s)
"""

# The newest ``limit`` entries, oldest first; ``organization_id`` NULL means all.
OPERATION_LOG_REPLAY_SQL = """
SELECT seq, event, payload, organization_id, created_at FROM (
    SELECT seq, event, payload, organization_id, created_at
    FROM omnidesk_appsync_operation_log
    WHERE namespace = %(namespace)s AND (%(organization_id)s::text IS NULL OR organization_id = %(organization_id)s::text)
    ORDER BY seq DESC
    LIMIT %(limit)s
) recent
ORDER BY seq ASC
"""

SYNC_STATE_COUNTS_SQL = """
SELECT
    (SELECT COUNT(*) FROM omnidesk_appsync_local_outbox WHERE namespace = %(namespace)s AND status IN ('pending', 'failed') AND (%(organization_id)s::text IS NULL OR organization_id = %(organization_id)s::text)),
    (SELECT COUNT(*) FROM omnidesk_appsync_local_outbox WHERE namespace = %(namespace)s AND status = 'synced' AND (%(organization_id)s::text IS NULL OR organization_id = %(organization_id)s::text)),
    (SELECT COUNT(*) FROM omnidesk_appsync_local_outbox WHERE namespace = %(namespace)s AND status = 'conflict' AND (%(organization_id)s::text IS NULL OR organization_id = %(organization_id)s::text)),
    (SELECT COUNT(*) FROM omnidesk_appsync_local_inbox WHERE namespace = %(namespace)s AND (%(organization_id)s::text IS NULL OR organization_id = %(organization_id)s::text)),
    (SELECT COUNT(*) FROM omnidesk_appsync_sync_conflicts WHERE namespace = %(namespace)s AND status = 'open' AND (%(organization_id)s::text IS NULL OR organization_id = %(organization_id)s::text))
"""

_JSON_COLUMNS = frozenset({"capabilities", "usage", "payload", "local_payload", "remote_payload"})


_QUERIES: dict[tuple[Any, ...], Any] = {}


def _composed(key: tuple[Any, ...], build: Callable[[Any], Any]) -> Any:
    """Compose a statement once with psycopg.sql (identifiers quoted) and cache it."""
    query = _QUERIES.get(key)
    if query is None:
        from psycopg import sql  # type: ignore

        query = build(sql)
        _QUERIES[key] = query
    return query


@dataclass(frozen=True)
class _Table:
    """A table whose columns are exactly the fields of an AppSync record dataclass."""

    name: str
    record: type
    key: str

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(item.name for item in fields(self.record))

    def column_list(self, alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        return ", ".join(prefix + column for column in self.columns)

    def _identifiers(self, sql: Any, columns: Sequence[str], alias: str = "") -> Any:
        return sql.SQL(", ").join(sql.Identifier(alias, column) if alias else sql.Identifier(column) for column in columns)

    def select_by_key(self, *, for_update: bool) -> Any:
        def build(sql: Any) -> Any:
            query = sql.SQL("SELECT {columns} FROM {table} WHERE namespace = %s AND {key} = %s").format(
                columns=self._identifiers(sql, self.columns), table=sql.Identifier(self.name), key=sql.Identifier(self.key)
            )
            return query + sql.SQL(" FOR UPDATE") if for_update else query

        return _composed((self.name, "select_by_key", for_update), build)

    def organization_by_key(self) -> Any:
        return _composed(
            (self.name, "organization_by_key"),
            lambda sql: sql.SQL("SELECT organization_id FROM {table} WHERE namespace = %s AND {key} = %s").format(
                table=sql.Identifier(self.name), key=sql.Identifier(self.key)
            ),
        )

    def exists_by_key(self) -> Any:
        return _composed(
            (self.name, "exists_by_key"),
            lambda sql: sql.SQL("SELECT 1 FROM {table} WHERE namespace = %s AND {key} = %s").format(
                table=sql.Identifier(self.name), key=sql.Identifier(self.key)
            ),
        )

    def select(self, *, where: str, order: str, limit: Optional[int], alias: str, for_update: bool = False) -> Any:
        """``where`` and ``order`` are fixed fragments from this module, never input."""

        def build(sql: Any) -> Any:
            source = sql.SQL("{table} {alias}").format(table=sql.Identifier(self.name), alias=sql.Identifier(alias)) if alias else sql.Identifier(self.name)
            namespace = sql.Identifier(alias, "namespace") if alias else sql.Identifier("namespace")
            query = sql.SQL("SELECT {columns} FROM {source} WHERE {namespace} = %(namespace)s").format(
                columns=self._identifiers(sql, self.columns, alias), source=source, namespace=namespace
            )
            if where:
                query += sql.SQL(" AND ") + sql.SQL(where)
            if order:
                query += sql.SQL(" ORDER BY ") + sql.SQL(order)
            if limit is not None:
                query += sql.SQL(" LIMIT ") + sql.Literal(int(limit))
            if for_update:
                query += sql.SQL(" FOR UPDATE")
            return query

        return _composed((self.name, "select", where, order, limit, alias, for_update), build)

    def insert(self, *, if_absent: bool) -> Any:
        def build(sql: Any) -> Any:
            columns = ("namespace",) + self.columns
            query = sql.SQL("INSERT INTO {table} ({columns}) VALUES ({values})").format(
                table=sql.Identifier(self.name),
                columns=self._identifiers(sql, columns),
                values=sql.SQL(", ").join(sql.Placeholder(column) for column in columns),
            )
            if if_absent:
                query += sql.SQL(" ON CONFLICT (namespace, {key}) DO NOTHING RETURNING {key}").format(key=sql.Identifier(self.key))
            return query

        return _composed((self.name, "insert", if_absent), build)

    def update_by_key(self) -> Any:
        def build(sql: Any) -> Any:
            assignments = sql.SQL(", ").join(
                sql.SQL("{column} = {value}").format(column=sql.Identifier(column), value=sql.Placeholder(column))
                for column in self.columns
                if column != self.key
            )
            return sql.SQL("UPDATE {table} SET {assignments} WHERE namespace = %(namespace)s AND {key} = {key_value}").format(
                table=sql.Identifier(self.name), assignments=assignments, key=sql.Identifier(self.key), key_value=sql.Placeholder(self.key)
            )

        return _composed((self.name, "update_by_key"), build)

    def upsert(self) -> Any:
        def build(sql: Any) -> Any:
            columns = ("namespace",) + self.columns
            assignments = sql.SQL(", ").join(
                sql.SQL("{column} = EXCLUDED.{column}").format(column=sql.Identifier(column)) for column in self.columns if column != self.key
            )
            return sql.SQL("INSERT INTO {table} ({columns}) VALUES ({values}) ON CONFLICT (namespace, {key}) DO UPDATE SET {assignments}").format(
                table=sql.Identifier(self.name),
                columns=self._identifiers(sql, columns),
                values=sql.SQL(", ").join(sql.Placeholder(column) for column in columns),
                key=sql.Identifier(self.key),
                assignments=assignments,
            )

        return _composed((self.name, "upsert"), build)


USERS = _Table("omnidesk_appsync_users", UserProfile, "user_id")
DEVICES = _Table("omnidesk_appsync_devices", DeviceRecord, "device_id")
CONVERSATIONS = _Table("omnidesk_appsync_conversations", ConversationRecord, "conversation_id")
MESSAGES = _Table("omnidesk_appsync_messages", MessageRecord, "message_id")
TASKS = _Table("omnidesk_appsync_tasks", TaskRecord, "task_id")
APPROVALS = _Table("omnidesk_appsync_approvals", ApprovalRecord, "approval_id")
NOTIFICATIONS = _Table("omnidesk_appsync_notifications", NotificationRecord, "notification_id")
RUNTIMES = _Table("omnidesk_appsync_runtime_status", RuntimeStatusRecord, "device_id")
ENROLLMENTS = _Table("omnidesk_appsync_device_enrollments", DeviceEnrollmentRecord, "enrollment_id")
PUSH_OUTBOX = _Table("omnidesk_appsync_push_outbox", PushOutboxRecord, "push_id")
CHALLENGES = _Table("omnidesk_appsync_device_challenges", DeviceChallengeRecord, "challenge_id")
LOCAL_OUTBOX = _Table("omnidesk_appsync_local_outbox", LocalOutboxRecord, "operation_id")
LOCAL_INBOX = _Table("omnidesk_appsync_local_inbox", LocalInboxRecord, "remote_event_id")
SYNC_CURSORS = _Table("omnidesk_appsync_sync_cursors", SyncCursorRecord, "cursor_id")
SYNC_CONFLICTS = _Table("omnidesk_appsync_sync_conflicts", SyncConflictRecord, "conflict_id")

CLAIM_NEXT_TASK = CLAIM_NEXT_TASK_SQL.format(task_columns=TASKS.column_list("t"))
LEASE_RENEWAL = LEASE_RENEWAL_SQL.format(task_columns=TASKS.column_list())


def _env_number(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class PostgresTimeouts:
    """Per-connection limits; all values come from the environment or defaults."""

    statement_timeout_ms: int = 15_000
    lock_timeout_ms: int = 5_000
    idle_in_transaction_timeout_ms: int = 60_000
    connect_timeout_seconds: int = 5
    pool_acquire_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "PostgresTimeouts":
        return cls(
            statement_timeout_ms=int(_env_number("OMNIDESK_APPSYNC_STATEMENT_TIMEOUT_MS", 15_000, minimum=100, maximum=600_000)),
            lock_timeout_ms=int(_env_number("OMNIDESK_APPSYNC_LOCK_TIMEOUT_MS", 5_000, minimum=50, maximum=120_000)),
            idle_in_transaction_timeout_ms=int(_env_number("OMNIDESK_APPSYNC_IDLE_TX_TIMEOUT_MS", 60_000, minimum=1_000, maximum=3_600_000)),
            connect_timeout_seconds=int(_env_number("OMNIDESK_APPSYNC_CONNECT_TIMEOUT_SECONDS", 5, minimum=1, maximum=120)),
            pool_acquire_timeout_seconds=_env_number("OMNIDESK_APPSYNC_POOL_ACQUIRE_TIMEOUT_SECONDS", 10.0, minimum=0.1, maximum=300.0),
        )

    def session_options(self) -> str:
        return (
            f"-c statement_timeout={self.statement_timeout_ms}"
            f" -c lock_timeout={self.lock_timeout_ms}"
            f" -c idle_in_transaction_session_timeout={self.idle_in_transaction_timeout_ms}"
        )


class PoolTimeout(RuntimeError):
    """No PostgreSQL connection became free within the acquire timeout."""


def _connection_is_broken(connection: Any, exc: BaseException) -> bool:
    if not isinstance(exc, Exception):
        # Interrupted mid-statement (cancellation, KeyboardInterrupt): unknown state.
        return True
    return bool(getattr(connection, "broken", False) or getattr(connection, "closed", False))


class PsycopgConnectionPool:
    """Small bounded connection pool without a second runtime dependency."""

    def __init__(self, dsn: str, *, max_size: int = 12, timeouts: PostgresTimeouts | None = None) -> None:
        self.dsn = dsn
        self.max_size = max(2, int(max_size))
        self.timeouts = timeouts or PostgresTimeouts()
        self._idle: queue.LifoQueue[Any] = queue.LifoQueue(self.max_size)
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    def _connect_kwargs(self) -> dict[str, Any]:
        from psycopg import conninfo  # type: ignore

        existing = str(conninfo.conninfo_to_dict(self.dsn).get("options") or "").strip()
        options = f"{existing} {self.timeouts.session_options()}".strip()
        return {"connect_timeout": self.timeouts.connect_timeout_seconds, "options": options}

    def _new_connection(self) -> Any:
        try:
            import psycopg  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for PostgreSQL AppSync") from exc
        return psycopg.connect(self.dsn, **self._connect_kwargs())

    def _discard(self, connection: Any) -> None:
        try:
            connection.close()
        finally:
            with self._lock:
                self._created = max(0, self._created - 1)

    def _acquire(self) -> Any:
        if self._closed:
            raise RuntimeError("PostgreSQL connection pool is closed")
        while True:
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                break
            if not bool(getattr(connection, "closed", False)):
                return connection
            with self._lock:
                self._created = max(0, self._created - 1)
        with self._lock:
            create = self._created < self.max_size
            if create:
                self._created += 1
        if create:
            try:
                return self._new_connection()
            except Exception:
                with self._lock:
                    self._created -= 1
                raise
        try:
            connection = self._idle.get(timeout=self.timeouts.pool_acquire_timeout_seconds)
        except queue.Empty as exc:
            raise PoolTimeout(
                f"no PostgreSQL AppSync connection became free within {self.timeouts.pool_acquire_timeout_seconds:g}s"
            ) from exc
        if bool(getattr(connection, "closed", False)):
            with self._lock:
                self._created = max(0, self._created - 1)
            return self._acquire()
        return connection

    def _release(self, connection: Any, *, broken: bool = False) -> None:
        if broken or self._closed or bool(getattr(connection, "closed", False)):
            self._discard(connection)
            return
        try:
            connection.rollback()
            connection.autocommit = False
            self._idle.put_nowait(connection)
        except Exception:
            self._discard(connection)

    @contextmanager
    def connection(self) -> Iterator[Any]:
        connection = self._acquire()
        broken = False
        try:
            yield connection
            connection.commit()
        except BaseException as exc:
            # A failed statement or a domain error rolls back and keeps the
            # connection; only an interrupted or dead connection is discarded.
            broken = _connection_is_broken(connection, exc)
            try:
                connection.rollback()
            except Exception:
                broken = True
            raise
        finally:
            self._release(connection, broken=broken)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                break
            self._discard(connection)


class _CommitThenRaise(Exception):
    """Returned (never raised) by a transaction body that must commit, then fail.

    Expiring an approval, enrollment or challenge is a state change the caller
    must see as an error, the way the in-memory store persists it and raises.
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass
class _Tx:
    cur: Any
    namespace: str
    now: float = field(default_factory=_now)
    events: list[dict[str, Any]] = field(default_factory=list)
    organizations: dict[str, str] = field(default_factory=dict)
    unqueued: set[int] = field(default_factory=set)


def _payload_dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _lock_key(scope: str) -> int:
    return int.from_bytes(hashlib.sha256(scope.encode("utf-8")).digest()[:8], "big", signed=True)


def _retryable_errors() -> tuple[type[BaseException], ...]:
    try:
        from psycopg import errors  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover
        return ()
    # Unique violations come from two transactions creating the same new row; a
    # retry sees the winner's row. Lock and statement timeouts are not retried.
    return (errors.UniqueViolation, errors.DeadlockDetected, errors.SerializationFailure)


class MigratedMultiInstancePostgresAppSyncStore:
    """Multi-instance AppSync store: per-entity SQL transactions, no shared cache.

    It implements the same public contract as :class:`AppSyncStore` (the JSON
    development store), sharing its records and its authorization rules.
    """

    MAX_TRANSACTION_ATTEMPTS = 3

    def __init__(
        self,
        dsn: str,
        namespace: str = "default",
        *,
        local_outbox_enabled: bool = False,
        pool_size: int = 12,
        timeouts: PostgresTimeouts | None = None,
    ) -> None:
        self.dsn = dsn
        self.namespace = namespace or "default"
        self.local_outbox_enabled = bool(local_outbox_enabled)
        self.timeouts = timeouts or PostgresTimeouts.from_env()
        self._connection_pool = PsycopgConnectionPool(dsn, max_size=pool_size, timeouts=self.timeouts)
        # Configuration only; no AppSync state is held in memory.
        self._lock = threading.RLock()
        self.actor_organizations: dict[str, str] = {}
        self.allow_legacy_hmac_device_keys = True
        self.network_state: dict[str, Any] = {"state": "local_only", "updated_at": _now()}
        self._legacy_lock_key = _lock_key_legacy(self.namespace)
        self._retryable = _retryable_errors()
        with self._connect() as conn:
            self._ensure_schema(conn)
        self.organization = self._load_default_organization()

    # ------------------------------------------------------------------ plumbing

    def _connect(self) -> Any:
        return self._connection_pool.connection()

    def _ensure_schema(self, conn: Any) -> None:
        assert_appsync_schema_current(conn, namespace=self.namespace)

    def close(self) -> None:
        pool = getattr(self, "_connection_pool", None)
        if pool is not None:
            pool.close()

    def health_details(self) -> dict[str, Any]:
        return {
            "backend": "postgres",
            "namespace": self.namespace,
            "schema_policy": "migration_gated",
            "multi_instance_consistency": "row_locks_and_commit_ordered_event_sequence",
            "connection_pool": "bounded",
            "statement_timeout_ms": self.timeouts.statement_timeout_ms,
            "lock_timeout_ms": self.timeouts.lock_timeout_ms,
            "chat_write_path": "direct_transactional_repository",
        }

    def _transaction(self, body: Callable[[_Tx], T], *, write: bool = True) -> T:
        """Run ``body`` in one transaction, retrying insert races and deadlocks."""
        attempt = 0
        while True:
            attempt += 1
            try:
                result = self._run_once(body, write=write)
            except self._retryable:
                if attempt >= self.MAX_TRANSACTION_ATTEMPTS:
                    raise
                continue
            if isinstance(result, _CommitThenRaise):
                raise result.error
            return result

    def _run_once(self, body: Callable[[_Tx], T], *, write: bool) -> T:
        with self._connect() as conn, conn.cursor() as cur:
            if write:
                cur.execute(LEGACY_LOCK_SQL, (self._legacy_lock_key,))
            tx = _Tx(cur=cur, namespace=self.namespace)
            result = body(tx)
            if tx.events:
                # Last, so the counter row lock is held only until the commit.
                self._flush_events(tx)
            return result

    def _read(self, body: Callable[[_Tx], T]) -> T:
        return self._transaction(body, write=False)

    def _params(self, record: Any) -> dict[str, Any]:
        from psycopg.types.json import Jsonb  # type: ignore

        values = asdict(record)
        params = {key: (Jsonb(value) if key in _JSON_COLUMNS else value) for key, value in values.items()}
        params["namespace"] = self.namespace
        return params

    @staticmethod
    def _from_row(table: _Table, row: Sequence[Any]) -> Any:
        values = dict(zip(table.columns, row))
        for column in _JSON_COLUMNS.intersection(values):
            raw = values[column]
            if isinstance(raw, str):
                values[column] = json.loads(raw or "null")
            if values[column] is None:
                values[column] = [] if column == "capabilities" else {}
        return table.record(**values)

    def _get(self, tx: _Tx, table: _Table, key: Optional[str], *, for_update: bool = False) -> Any:
        if not key:
            return None
        tx.cur.execute(table.select_by_key(for_update=for_update), (self.namespace, key))
        row = tx.cur.fetchone()
        return self._from_row(table, row) if row else None

    def _select(self, tx: _Tx, table: _Table, where: str, params: dict[str, Any], *, order: str = "", limit: Optional[int] = None, alias: str = "", for_update: bool = False) -> list[Any]:
        tx.cur.execute(table.select(where=where, order=order, limit=limit, alias=alias, for_update=for_update), {**params, "namespace": self.namespace})
        return [self._from_row(table, row) for row in tx.cur.fetchall()]

    def _insert(self, tx: _Tx, table: _Table, record: Any, *, if_absent: bool = False) -> bool:
        tx.cur.execute(table.insert(if_absent=if_absent), self._params(record))
        return tx.cur.fetchone() is not None if if_absent else True

    def _update(self, tx: _Tx, table: _Table, record: Any) -> None:
        """Write a whole row the transaction has locked with ``FOR UPDATE``."""
        tx.cur.execute(table.update_by_key(), self._params(record))

    def _upsert(self, tx: _Tx, table: _Table, record: Any) -> None:
        tx.cur.execute(table.upsert(), self._params(record))

    @staticmethod
    def _record(obj: Any) -> dict[str, Any]:
        return asdict(obj)

    # --------------------------------------------------------- organizations

    def _load_default_organization(self) -> Organization:
        def body(tx: _Tx) -> Organization:
            tx.cur.execute(
                "SELECT organization_id, name, created_at FROM omnidesk_appsync_organizations WHERE namespace = %s ORDER BY created_at ASC LIMIT 1",
                (self.namespace,),
            )
            row = tx.cur.fetchone()
            if row:
                return Organization(organization_id=str(row[0]), name=str(row[1]), created_at=float(row[2]))
            return Organization()

        return self._read(body)

    def _default_org_id(self) -> str:
        return self.organization.organization_id or "org_default"

    def _normalize_org_id(self, organization_id: Optional[str]) -> str:
        return str(organization_id or self._default_org_id() or "org_default").strip() or "org_default"

    def configure_device_key_policy(self, *, allow_legacy_hmac: bool) -> None:
        with self._lock:
            self.allow_legacy_hmac_device_keys = bool(allow_legacy_hmac)

    def _legacy_hmac_refused(self, public_key: Optional[str], *, allow_legacy_hmac: bool = True) -> bool:
        allowed = self.allow_legacy_hmac_device_keys and allow_legacy_hmac
        return not allowed and is_legacy_hmac_device_key(public_key)

    def configure_actor_organizations(self, assignments: Optional[dict[str, str]]) -> None:
        with self._lock:
            self.actor_organizations = {
                str(actor).strip(): self._normalize_org_id(organization_id)
                for actor, organization_id in (assignments or {}).items()
                if str(actor).strip()
            }

    def organization_for_new_actor(self, actor: Optional[str]) -> str:
        return self._normalize_org_id(self.actor_organizations.get(actor or ""))

    def _org_for_actor(self, tx: _Tx, actor: Optional[str]) -> str:
        key = actor or "unknown"
        cached = tx.organizations.get(key)
        if cached is not None:
            return cached
        tx.cur.execute(ACTOR_ORGANIZATION_SQL, (self.namespace, key))
        row = tx.cur.fetchone()
        organization_id = self._normalize_org_id(str(row[0])) if row else self.organization_for_new_actor(actor)
        tx.organizations[key] = organization_id
        return organization_id

    def _organization_for_actor(self, actor: Optional[str]) -> str:
        return self._read(lambda tx: self._org_for_actor(tx, actor))

    def _require_actor_org(self, tx: _Tx, actor: Optional[str], organization_id: Optional[str]) -> None:
        if self._org_for_actor(tx, actor) != self._normalize_org_id(organization_id):
            raise PermissionError("organization scope mismatch")

    def _org_for_device(self, tx: _Tx, device_id: Optional[str]) -> Optional[str]:
        if not device_id:
            return None
        tx.cur.execute(
            "SELECT organization_id FROM omnidesk_appsync_devices WHERE namespace = %s AND device_id = %s",
            (self.namespace, device_id),
        )
        row = tx.cur.fetchone()
        return self._normalize_org_id(str(row[0])) if row else None

    def _org_for_related(self, tx: _Tx, related_id: Optional[str], actor: Optional[str]) -> str:
        if related_id:
            for table in (TASKS, APPROVALS, CONVERSATIONS, DEVICES):
                tx.cur.execute(table.organization_by_key(), (self.namespace, related_id))
                row = tx.cur.fetchone()
                if row:
                    return self._normalize_org_id(str(row[0]))
        return self._org_for_actor(tx, actor)

    def _conversation_owner(self, tx: _Tx, conversation_id: Optional[str]) -> Optional[str]:
        if not conversation_id:
            return None
        tx.cur.execute(
            "SELECT actor FROM omnidesk_appsync_conversations WHERE namespace = %s AND conversation_id = %s",
            (self.namespace, conversation_id),
        )
        row = tx.cur.fetchone()
        return str(row[0]) if row else None

    def _task_for_actor(self, tx: _Tx, task: dict[str, Any], actor: Optional[str]) -> dict[str, Any]:
        """Tasks are organization-wide work items; the conversation they came from is not."""
        if actor and self._conversation_owner(tx, str(task.get("conversation_id") or "")) != actor:
            return {key: value for key, value in task.items() if key != "conversation_id"}
        return task

    # ----------------------------------------------------------------- events

    def _event(self, tx: _Tx, event_type: str, actor: str, payload: dict[str, Any], *, queue_locally: bool = True) -> dict[str, Any]:
        """``queue_locally=False`` for applied uploads: they must not be queued
        back into this node's own offline outbox."""
        payload = dict(payload or {})
        organization_id = self._normalize_org_id(
            payload.get("organization_id")
            or self._org_for_related(
                tx,
                str(payload.get("task_id") or payload.get("approval_id") or payload.get("conversation_id") or payload.get("device_id") or ""),
                actor,
            )
        )
        payload.setdefault("organization_id", organization_id)
        event = {"seq": 0, "ts": tx.now, "event_type": event_type, "actor": actor, "organization_id": organization_id, "payload": payload}
        tx.events.append(event)
        if not queue_locally:
            tx.unqueued.add(id(event))
        return event

    def _flush_events(self, tx: _Tx) -> None:
        from psycopg.types.json import Jsonb  # type: ignore

        count = len(tx.events)
        tx.cur.execute(EVENT_COUNTER_SQL, {"namespace": self.namespace, "n": count})
        row = tx.cur.fetchone()
        last = int(row[0])
        first = last - count + 1
        for offset, event in enumerate(tx.events):
            event["seq"] = first + offset
        tx.cur.executemany(
            INSERT_EVENT_SQL,
            [
                {
                    "namespace": self.namespace,
                    "organization_id": event["organization_id"],
                    "seq": event["seq"],
                    "event_type": event["event_type"],
                    "actor": event["actor"],
                    "payload": Jsonb(event["payload"]),
                    "created_at": event["ts"],
                }
                for event in tx.events
            ],
        )
        if self.local_outbox_enabled:
            for event in tx.events:
                if id(event) not in tx.unqueued and not str(event["event_type"]).startswith(("operation.", "sync.", "network.")):
                    self._auto_queue_event(tx, event)

    def _auto_queue_event(self, tx: _Tx, event: dict[str, Any]) -> None:
        payload = _payload_dict(event.get("payload"))
        organization_id = self._normalize_org_id(event.get("organization_id") or payload.get("organization_id"))
        record = LocalOutboxRecord(
            operation_id=_id("op"),
            operation_type=str(event.get("event_type") or "event"),
            payload={"event_seq": int(event.get("seq", 0)), "event_type": event.get("event_type"), "payload": payload},
            actor=str(event.get("actor") or "system"),
            organization_id=organization_id,
            device_id=payload.get("device_id") or payload.get("source_device_id"),
            idempotency_key=f"event:{event.get('seq')}:{event.get('event_type')}",
            payload_hash=_fingerprint(payload),
            created_at=tx.now,
            updated_at=tx.now,
        )
        self._insert(tx, LOCAL_OUTBOX, record)
        self._operation_log(tx, "operation.enqueued", {"operation_id": record.operation_id, "operation_type": record.operation_type, "organization_id": organization_id})

    def _operation_log(self, tx: _Tx, event: str, payload: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb  # type: ignore

        entry = dict(payload or {})
        tx.cur.execute(
            OPERATION_LOG_SQL,
            {
                "namespace": self.namespace,
                "organization_id": self._normalize_org_id(entry.get("organization_id")),
                "event": event,
                "payload": Jsonb(entry),
                "created_at": tx.now,
            },
        )

    def record_event(self, event_type: str, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append an event raised by a collaborator such as the project store."""
        return self._transaction(lambda tx: self._event(tx, event_type, actor, payload))

    def _notify(
        self,
        tx: _Tx,
        *,
        audience: Literal["desktop", "mobile", "web_admin", "all"],
        title: str,
        body: str,
        event_type: str,
        actor: str,
        related_id: Optional[str],
    ) -> dict[str, Any]:
        organization_id = self._org_for_related(tx, related_id, actor)
        notification = NotificationRecord(
            notification_id=_id("notif"), audience=audience, title=title, body=body, event_type=event_type,
            actor=actor, organization_id=organization_id, related_id=related_id, created_at=tx.now,
        )
        self._insert(tx, NOTIFICATIONS, notification)
        tx.cur.execute(
            PUSH_FANOUT_SQL,
            {
                "namespace": self.namespace, "organization_id": organization_id, "audience": audience,
                "title": title, "body": body, "related_id": related_id, "now": tx.now,
                "recipient": self._conversation_owner(tx, related_id),
            },
        )
        self._event(tx, "notification.created", actor, {"notification_id": notification.notification_id, "audience": audience, "event_type": event_type, "related_id": related_id, "organization_id": organization_id})
        return self._record(notification)

    # ------------------------------------------------------------ idempotency

    def _idempotency_lookup(self, tx: _Tx, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None, lock: bool) -> Optional[Any]:
        if not key:
            return None
        organization_id = self._org_for_actor(tx, actor)
        if lock:
            # Same-key requests run one at a time, so the second sees the first's answer.
            tx.cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(f"idempotency:{self.namespace}:{organization_id}:{actor}:{endpoint}:{key}"),))
        tx.cur.execute(
            "SELECT payload_hash, response FROM omnidesk_appsync_idempotency_keys WHERE namespace = %s AND organization_id = %s AND actor = %s AND endpoint = %s AND key = %s",
            (self.namespace, organization_id, actor or "unknown", endpoint, key),
        )
        row = tx.cur.fetchone()
        if not row:
            return None
        if str(row[0]) != _fingerprint(payload):
            raise IdempotencyConflict("idempotency key was reused with a different payload")
        response = row[1]
        return json.loads(response) if isinstance(response, str) else response

    def _idempotency_store(self, tx: _Tx, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None, response: Any) -> None:
        if not key:
            return
        from psycopg.types.json import Jsonb  # type: ignore

        tx.cur.execute(
            """
            INSERT INTO omnidesk_appsync_idempotency_keys(namespace, organization_id, actor, endpoint, key, payload_hash, response, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (namespace, organization_id, actor, endpoint, key)
            DO UPDATE SET payload_hash = EXCLUDED.payload_hash, response = EXCLUDED.response, created_at = EXCLUDED.created_at
            """,
            (self.namespace, self._org_for_actor(tx, actor), actor or "unknown", endpoint, key, _fingerprint(payload), Jsonb(response), tx.now),
        )

    def _idempotency_get(self, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None) -> Optional[Any]:
        return self._read(lambda tx: self._idempotency_lookup(tx, actor=actor, endpoint=endpoint, key=key, payload=payload, lock=False))

    def get_idempotency_response(self, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None = None) -> Optional[Any]:
        return self._idempotency_get(actor=actor, endpoint=endpoint, key=key, payload=payload)

    def put_idempotency_response(self, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None, response: Any) -> None:
        self._transaction(lambda tx: self._idempotency_store(tx, actor=actor, endpoint=endpoint, key=key, payload=payload, response=response))

    # ------------------------------------------------------------------ users

    def _ensure_user(self, tx: _Tx, actor: str, *, role: str = "operator", display_name: Optional[str] = None, organization_id: Optional[str] = None) -> UserProfile:
        actor = actor or "unknown"
        requested = self._normalize_org_id(organization_id) if organization_id is not None else None
        assigned = self.actor_organizations.get(actor)
        if requested is not None and assigned is not None and requested != assigned:
            raise PermissionError("organization does not match the server-side assignment for this actor")
        user = self._get(tx, USERS, actor)
        if user is None:
            new_organization_id = requested or self.organization_for_new_actor(actor)
            candidate = UserProfile(user_id=actor, display_name=display_name or actor, role=role, organization_id=new_organization_id, created_at=tx.now, updated_at=tx.now)
            if self._insert(tx, USERS, candidate, if_absent=True):
                tx.organizations[actor] = new_organization_id
                self._event(tx, "user.created", actor, {"user_id": actor, "role": role, "organization_id": new_organization_id})
                return candidate
            user = self._get(tx, USERS, actor)
        if requested is not None and self._normalize_org_id(user.organization_id) != requested:
            raise PermissionError("actor is already bound to a different organization")
        tx.organizations[actor] = self._normalize_org_id(user.organization_id)
        return user

    def ensure_user(self, actor: str, role: str = "operator", display_name: Optional[str] = None, organization_id: Optional[str] = None) -> dict[str, Any]:
        return self._record(
            self._transaction(lambda tx: self._ensure_user(tx, actor, role=role, display_name=display_name, organization_id=organization_id))
        )

    # ---------------------------------------------------------------- devices

    def register_device(self, *, actor: str, device_id: Optional[str], device_type: DeviceType, name: str, platform: str, push_token: Optional[str] = None, capabilities: Optional[list[str]] = None, public_key: Optional[str] = None, token_hash: Optional[str] = None, organization_id: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None, device_signature_verified: bool = False) -> dict[str, Any]:
        actor = actor or "unknown"
        if self._legacy_hmac_refused(public_key):
            raise PermissionError(LEGACY_HMAC_KEY_REFUSED)
        chosen_id = device_id or _id("dev")

        def body(tx: _Tx) -> dict[str, Any]:
            self._ensure_user(tx, actor, organization_id=organization_id)
            organization = self._org_for_actor(tx, actor)
            cached = self._idempotency_lookup(tx, actor=actor, endpoint="devices.register", key=idempotency_key, payload=idempotency_payload, lock=True)
            if cached is not None:
                return cached
            existing: Optional[DeviceRecord] = self._get(tx, DEVICES, chosen_id, for_update=True)
            if existing:
                if self._normalize_org_id(existing.organization_id) != organization:
                    raise PermissionError("device is already bound to a different organization")
                if existing.actor != actor:
                    raise PermissionError("device is already bound to a different actor")
                if existing.credential_status == "revoked" or existing.revoked_at:
                    raise PermissionError("device is revoked; enroll a new device")
                key_changed = bool(public_key) and public_key != existing.public_key
                if existing.credential_status == "verified":
                    AppSyncStore._check_verified_device_update(
                        existing, device_type=device_type, key_changed=key_changed, push_token=push_token,
                        capabilities=capabilities, token_hash=token_hash, device_signature_verified=device_signature_verified,
                    )
                existing.device_type = device_type
                existing.name = name or existing.name
                existing.platform = platform or existing.platform
                existing.organization_id = organization
                existing.push_token = push_token or existing.push_token
                existing.public_key = public_key or existing.public_key
                existing.token_hash = token_hash or existing.token_hash
                if key_changed:
                    existing.credential_status = "enrolled"
                    existing.trust_level = "public_key_registered"
                existing.capabilities = list(capabilities or existing.capabilities)
                existing.online = True
                existing.last_seen_at = tx.now
                existing.updated_at = tx.now
                self._update(tx, DEVICES, existing)
                device = existing
            else:
                device = DeviceRecord(
                    device_id=chosen_id, device_type=device_type, name=name, platform=platform, actor=actor,
                    organization_id=organization, push_token=push_token, public_key=public_key, token_hash=token_hash,
                    credential_status="enrolled" if public_key else "pending",
                    trust_level="public_key_registered" if public_key else "unverified",
                    capabilities=list(capabilities or []), last_seen_at=tx.now, created_at=tx.now, updated_at=tx.now,
                )
                if not self._insert(tx, DEVICES, device, if_absent=True):
                    raise _concurrent_insert()
            self._event(tx, "device.registered", actor, {"device_id": chosen_id, "device_type": device_type, "organization_id": organization})
            result = self._record(device)
            self._idempotency_store(tx, actor=actor, endpoint="devices.register", key=idempotency_key, payload=idempotency_payload, response=result)
            return result

        return self._transaction(body)

    def register_push_token(self, *, actor: str, device_id: str, push_token: str, platform: str = "unknown") -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            device: Optional[DeviceRecord] = self._get(tx, DEVICES, device_id, for_update=True)
            if not device:
                raise KeyError("device not found")
            self._require_actor_org(tx, actor, device.organization_id)
            if device.actor != actor:
                raise PermissionError("device belongs to a different actor")
            if device.revoked_at or device.credential_status == "revoked":
                raise PermissionError("device is revoked; enroll a new device")
            device.push_token = push_token
            device.platform = platform or device.platform
            device.last_seen_at = tx.now
            device.updated_at = tx.now
            self._update(tx, DEVICES, device)
            self._event(tx, "device.push_token_registered", actor, {"device_id": device_id, "platform": platform, "organization_id": device.organization_id})
            return self._record(device)

        return self._transaction(body)

    def rotate_device_token(self, *, actor: str, device_id: str) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            device: Optional[DeviceRecord] = self._get(tx, DEVICES, device_id, for_update=True)
            if not device:
                raise KeyError("device not found")
            self._require_actor_org(tx, actor, device.organization_id)
            if device.credential_status == "revoked":
                raise ValueError("device revoked")
            token = secrets.token_urlsafe(32)
            device.token_hash = _hash_secret(token, purpose="device_token")
            device.updated_at = tx.now
            self._update(tx, DEVICES, device)
            self._event(tx, "device.token_rotated", actor, {"device_id": device_id, "organization_id": device.organization_id})
            result = self._record(device)
            result["device_token"] = token
            return result

        return self._transaction(body)

    def revoke_device(self, *, actor: str, device_id: str, reason: Optional[str] = None) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            device: Optional[DeviceRecord] = self._get(tx, DEVICES, device_id, for_update=True)
            if not device:
                raise KeyError("device not found")
            self._require_actor_org(tx, actor, device.organization_id)
            device.credential_status = "revoked"
            device.trust_level = "revoked"
            device.revoked_at = tx.now
            device.online = False
            device.updated_at = tx.now
            # A lost or revoked device gets no further pushes, including queued ones.
            device.push_token = None
            self._update(tx, DEVICES, device)
            tx.cur.execute(REVOKED_DEVICE_PUSH_SQL, {"namespace": self.namespace, "device_id": device_id, "now": tx.now})
            self._event(tx, "device.revoked", actor, {"device_id": device_id, "reason": reason, "organization_id": device.organization_id})
            return self._record(device)

        return self._transaction(body)

    def register_approval_key(self, *, actor: str, device_id: str, public_key: str) -> dict[str, Any]:
        public_key = (public_key or "").strip()
        if not _is_ed25519_public_key(public_key):
            raise ValueError("approval key must be an Ed25519 public key (base64:<32 bytes>)")

        def body(tx: _Tx) -> dict[str, Any]:
            device: Optional[DeviceRecord] = self._get(tx, DEVICES, device_id, for_update=True)
            if not device:
                raise KeyError("device not found")
            if device.actor != actor:
                raise PermissionError("device is bound to a different actor")
            if device.revoked_at or device.credential_status == "revoked":
                raise PermissionError("device is revoked")
            if device.credential_status != "verified" or device.trust_level != "challenge_verified":
                raise PermissionError("device must be challenge-verified before registering an approval key")
            if device.device_type != "mobile":
                raise PermissionError("approval keys are only supported for mobile devices")
            if device.approval_public_key:
                if device.approval_public_key == public_key:
                    return self._record(device)
                # Write-once: a stolen device token must not be able to swap it.
                raise PermissionError("approval key already registered; re-enroll the device to replace it")
            device.approval_public_key = public_key
            device.approval_key_registered_at = tx.now
            device.updated_at = tx.now
            self._update(tx, DEVICES, device)
            self._event(tx, "device.approval_key_registered", actor, {"device_id": device_id, "organization_id": device.organization_id})
            return self._record(device)

        return self._transaction(body)

    def device_approval_key(self, device_id: str) -> Optional[str]:
        def body(tx: _Tx) -> Optional[str]:
            device: Optional[DeviceRecord] = self._get(tx, DEVICES, device_id)
            return device.approval_public_key if device else None

        return self._read(body)

    def _claim_nonce(self, tx: _Tx, nonce_key: str, *, ttl_seconds: int) -> bool:
        tx.cur.execute(NONCE_PRUNE_SQL, {"namespace": self.namespace, "now": tx.now, "batch": NONCE_PRUNE_BATCH})
        tx.cur.execute(
            NONCE_SQL,
            {"namespace": self.namespace, "nonce_key": nonce_key, "expires_at": tx.now + max(60, int(ttl_seconds or 600)), "now": tx.now},
        )
        return tx.cur.fetchone() is not None

    def verify_approval_signature(self, *, device_id: str, approval_id: str, decision: str, timestamp: str, nonce: str, signature: str, max_skew_seconds: int = 300, nonce_ttl_seconds: int = 600) -> tuple[bool, str]:
        now = _now()
        timestamp = (timestamp or "").strip()
        nonce = (nonce or "").strip()
        signature = (signature or "").strip()
        if not self.device_approval_key(device_id):
            return False, "approval_key_not_registered"
        if not timestamp or not nonce or not signature:
            return False, "missing_approval_signature"
        try:
            ts = float(timestamp)
            if ts > 10_000_000_000:
                ts = ts / 1000.0
        except ValueError:
            return False, "invalid_timestamp"
        if abs(now - ts) > max(30, int(max_skew_seconds or 300)):
            return False, "timestamp_out_of_window"
        if len(nonce) < 16 or len(nonce) > 180:
            return False, "invalid_nonce"

        def body(tx: _Tx) -> tuple[bool, str]:
            device: Optional[DeviceRecord] = self._get(tx, DEVICES, device_id)
            if not device or not device.approval_public_key:
                return False, "approval_key_not_registered"
            if self._legacy_hmac_refused(device.approval_public_key):
                return False, "legacy_hmac_key_not_allowed"
            message = _approval_decision_message(approval_id=approval_id, decision=decision, timestamp=timestamp, nonce=nonce)
            if not _verify_device_signature(device.approval_public_key, message, signature):
                return False, "approval_signature_mismatch"
            if not self._claim_nonce(tx, f"approval:{device_id}:{nonce}", ttl_seconds=nonce_ttl_seconds):
                return False, "nonce_replay"
            return True, "ok"

        return self._transaction(body)

    def verify_device_request_signature(self, *, device_id: str, method: str, path: str, body: bytes, timestamp: str, nonce: str, signature: str, expected_actor: str, required_device_types: set[str] | None = None, max_skew_seconds: int = 300, nonce_ttl_seconds: int = 600, allow_legacy_hmac: bool = True) -> tuple[bool, str]:
        now = _now()
        device_id = (device_id or "").strip()
        timestamp = (timestamp or "").strip()
        nonce = (nonce or "").strip()
        signature = (signature or "").strip()
        if not device_id:
            return False, "missing_device_id"
        if not timestamp:
            return False, "missing_timestamp"
        if not nonce:
            return False, "missing_nonce"
        if not signature:
            return False, "missing_signature"
        try:
            ts = float(timestamp)
            if ts > 10_000_000_000:  # tolerate millisecond clients
                ts = ts / 1000.0
        except ValueError:
            return False, "invalid_timestamp"
        if abs(now - ts) > max(30, int(max_skew_seconds or 300)):
            return False, "timestamp_out_of_window"
        if len(nonce) < 16 or len(nonce) > 180:
            return False, "invalid_nonce"
        request_body = body

        def transaction(tx: _Tx) -> tuple[bool, str]:
            device: Optional[DeviceRecord] = self._get(tx, DEVICES, device_id)
            if not device:
                return False, "device_not_found"
            if required_device_types and device.device_type not in required_device_types:
                return False, "wrong_device_type"
            if device.revoked_at or device.credential_status == "revoked":
                return False, "device_revoked"
            if device.credential_status != "verified" or device.trust_level != "challenge_verified":
                return False, "device_not_challenge_verified"
            if not expected_actor or device.actor != expected_actor:
                return False, "device_actor_mismatch"
            if self._normalize_org_id(device.organization_id) != self._org_for_actor(tx, expected_actor):
                return False, "device_organization_mismatch"
            public_key = device.public_key or ""
            if not public_key:
                return False, "missing_public_key"
            if self._legacy_hmac_refused(public_key, allow_legacy_hmac=allow_legacy_hmac):
                return False, "legacy_hmac_key_not_allowed"
            message = _device_request_message(method=method, path=path, body_sha256=_sha256_hex(request_body or b""), timestamp=timestamp, nonce=nonce)
            if not _verify_device_signature(public_key, message, signature):
                return False, "signature_mismatch"
            if not self._claim_nonce(tx, f"{device_id}:{nonce}", ttl_seconds=nonce_ttl_seconds):
                return False, "nonce_replay"
            # Liveness only; no sync event per signed request (see AppSyncStore).
            tx.cur.execute(
                "UPDATE omnidesk_appsync_devices SET last_seen_at = %s, updated_at = %s WHERE namespace = %s AND device_id = %s",
                (tx.now, tx.now, self.namespace, device_id),
            )
            return True, "ok"

        return self._transaction(transaction)

    # ------------------------------------------------------------- enrollment

    def start_device_enrollment(self, *, actor: str, device_type: DeviceType, pairing_code: str) -> dict[str, Any]:
        if device_type not in {"desktop", "mobile", "web_admin"}:
            raise ValueError("invalid device_type")

        def body(tx: _Tx) -> dict[str, Any]:
            user = self._ensure_user(tx, actor)
            organization_id = self._normalize_org_id(user.organization_id)
            record = DeviceEnrollmentRecord(
                enrollment_id=_id("enroll"), device_type=device_type, requested_by=actor or "unknown",
                pairing_code_hash=_hash_secret(pairing_code, purpose="pairing_code"), organization_id=organization_id,
                created_at=tx.now, expires_at=tx.now + 600,
            )
            self._insert(tx, ENROLLMENTS, record)
            self._event(tx, "device.enrollment_started", actor, {"enrollment_id": record.enrollment_id, "device_type": device_type, "organization_id": organization_id})
            result = self._record(record)
            result.pop("pairing_code_hash", None)
            return result

        return self._transaction(body)

    def complete_device_enrollment(self, *, actor: str, enrollment_id: str, pairing_code: str, device_id: str, public_key: str) -> dict[str, Any]:
        def body(tx: _Tx) -> Any:
            record: Optional[DeviceEnrollmentRecord] = self._get(tx, ENROLLMENTS, enrollment_id, for_update=True)
            if not record:
                raise KeyError("enrollment not found")
            self._require_actor_org(tx, actor, record.organization_id)
            if record.status != "pending":
                raise ValueError("enrollment is not pending")
            if record.expires_at < tx.now:
                record.status = "expired"
                self._update(tx, ENROLLMENTS, record)
                return _CommitThenRaise(ValueError("enrollment expired"))
            if not _secret_matches(record.pairing_code_hash, pairing_code, purpose="pairing_code"):
                raise ValueError("pairing code mismatch")
            bound_device = (device_id or "").strip()
            bound_key = (public_key or "").strip()
            if not bound_device or not bound_key:
                raise ValueError("device_id and public_key are required")
            if self._legacy_hmac_refused(bound_key):
                raise ValueError(LEGACY_HMAC_KEY_REFUSED)
            device: Optional[DeviceRecord] = self._get(tx, DEVICES, bound_device)
            if device:
                check_enrollment_device(record, device, actor=actor, normalize_org=self._normalize_org_id)
            # The pairing code binds this enrollment to one device and key; the
            # challenge must later be signed by exactly that key.
            record.status = "paired"
            record.device_id = bound_device
            record.public_key = bound_key
            self._update(tx, ENROLLMENTS, record)
            self._event(tx, "device.enrollment_completed", actor, {"enrollment_id": enrollment_id, "device_id": bound_device, "organization_id": record.organization_id})
            result = self._record(record)
            result.pop("pairing_code_hash", None)
            return result

        return self._transaction(body)

    def _paired_enrollment_error(self, tx: _Tx, enrollment: DeviceEnrollmentRecord, *, device_id: str) -> Optional[_CommitThenRaise]:
        if enrollment.status == "paired" and enrollment.expires_at < tx.now:
            enrollment.status = "expired"
            self._update(tx, ENROLLMENTS, enrollment)
            return _CommitThenRaise(ValueError("enrollment expired"))
        if enrollment.status != "paired":
            raise ValueError("enrollment is not awaiting device verification")
        if not device_id or device_id != enrollment.device_id:
            raise PermissionError("device does not match the enrollment")
        return None

    def issue_device_challenge(self, *, actor: str, enrollment_id: str, device_id: str) -> dict[str, Any]:
        def body(tx: _Tx) -> Any:
            record: Optional[DeviceEnrollmentRecord] = self._get(tx, ENROLLMENTS, enrollment_id, for_update=True)
            if not record:
                raise KeyError("enrollment not found")
            self._require_actor_org(tx, actor, record.organization_id)
            expired = self._paired_enrollment_error(tx, record, device_id=device_id)
            if expired is not None:
                return expired
            device: Optional[DeviceRecord] = self._get(tx, DEVICES, device_id)
            if not device:
                raise KeyError("device not found")
            check_enrollment_device(record, device, actor=actor, normalize_org=self._normalize_org_id)
            nonce = secrets.token_urlsafe(32)
            nonce_hash = _hash_secret(nonce, purpose="device_challenge_nonce")
            challenge = DeviceChallengeRecord(
                challenge_id=_id("chal"), enrollment_id=enrollment_id, device_id=device_id, nonce_hash=nonce_hash,
                organization_id=record.organization_id, created_at=tx.now, expires_at=tx.now + 300,
            )
            self._insert(tx, CHALLENGES, challenge)
            self._event(tx, "device.challenge_issued", actor, {"challenge_id": challenge.challenge_id, "enrollment_id": enrollment_id, "device_id": device_id, "organization_id": record.organization_id})
            result = self._record(challenge)
            result["nonce"] = nonce
            result["signing_message"] = _device_signing_message(challenge_id=challenge.challenge_id, nonce_hash=nonce_hash).decode("utf-8")
            result["signature_algorithms"] = ["ed25519", "p256_ecdsa_sha256"]
            result.pop("nonce_hash", None)
            return result

        return self._transaction(body)

    def verify_device_challenge(self, *, actor: str, enrollment_id: str, challenge_id: str, device_id: str, signature: str, allow_legacy_hmac: bool = True) -> dict[str, Any]:
        def body(tx: _Tx) -> Any:
            challenge: Optional[DeviceChallengeRecord] = self._get(tx, CHALLENGES, challenge_id, for_update=True)
            if not challenge or challenge.enrollment_id != enrollment_id or challenge.device_id != device_id:
                raise KeyError("challenge not found")
            self._require_actor_org(tx, actor, challenge.organization_id)
            if challenge.status != "pending":
                raise ValueError("challenge already used")
            if challenge.expires_at < tx.now:
                challenge.status = "expired"
                self._update(tx, CHALLENGES, challenge)
                return _CommitThenRaise(ValueError("challenge expired"))
            enrollment: Optional[DeviceEnrollmentRecord] = self._get(tx, ENROLLMENTS, enrollment_id, for_update=True)
            if not enrollment:
                raise KeyError("enrollment not found")
            if self._normalize_org_id(enrollment.organization_id) != self._normalize_org_id(challenge.organization_id):
                raise PermissionError("challenge belongs to a different organization")
            expired = self._paired_enrollment_error(tx, enrollment, device_id=device_id)
            if expired is not None:
                return expired
            device: Optional[DeviceRecord] = self._get(tx, DEVICES, device_id, for_update=True)
            if not device:
                raise KeyError("device not found")
            check_enrollment_device(enrollment, device, actor=actor, normalize_org=self._normalize_org_id)
            # Verify against the key bound by the pairing code, never a key the
            # device registered on its own.
            public_key = enrollment.public_key or ""
            if self._legacy_hmac_refused(public_key, allow_legacy_hmac=allow_legacy_hmac):
                raise PermissionError(LEGACY_HMAC_KEY_REFUSED)
            message = _device_signing_message(challenge_id=challenge.challenge_id, nonce_hash=challenge.nonce_hash)
            if not _verify_device_signature(public_key, message, signature):
                raise ValueError("device challenge signature mismatch")
            challenge.status = "verified"
            challenge.verified_at = tx.now
            token = secrets.token_urlsafe(32)
            token_hash = _hash_secret(token, purpose="device_token")
            device.public_key = public_key
            device.credential_status = "verified"
            device.trust_level = "challenge_verified"
            device.token_hash = token_hash
            device.last_challenge_nonce_hash = challenge.nonce_hash
            device.last_challenge_expires_at = challenge.expires_at
            device.updated_at = tx.now
            # Single use: the enrollment and its outstanding challenges are spent.
            enrollment.status = "completed"
            enrollment.completed_at = tx.now
            self._update(tx, CHALLENGES, challenge)
            self._update(tx, DEVICES, device)
            self._update(tx, ENROLLMENTS, enrollment)
            tx.cur.execute(
                "UPDATE omnidesk_appsync_device_challenges SET status = 'expired' WHERE namespace = %s AND enrollment_id = %s AND status = 'pending' AND challenge_id <> %s",
                (self.namespace, enrollment_id, challenge_id),
            )
            self._event(tx, "device.challenge_verified", actor, {"challenge_id": challenge_id, "enrollment_id": enrollment_id, "device_id": device_id, "algorithm": "asymmetric", "organization_id": challenge.organization_id})
            return {"device_id": device_id, "device_token": token, "token_hash": token_hash, "trust_level": "challenge_verified"}

        return self._transaction(body)

    # ---------------------------------------------------------- conversations

    def _owned_conversation(self, tx: _Tx, conversation_id: str, actor: str, *, for_update: bool = False) -> ConversationRecord:
        conversation: Optional[ConversationRecord] = self._get(tx, CONVERSATIONS, conversation_id, for_update=for_update)
        if not conversation:
            raise KeyError("conversation not found")
        self._require_actor_org(tx, actor, conversation.organization_id)
        if conversation.actor != actor:
            # The same answer as for a missing conversation.
            raise KeyError("conversation not found")
        return conversation

    def create_conversation(self, *, actor: str, title: str, source_device_id: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            cached = self._idempotency_lookup(tx, actor=actor, endpoint="conversations.create", key=idempotency_key, payload=idempotency_payload, lock=True)
            if cached is not None:
                return cached
            user = self._ensure_user(tx, actor)
            source_org = self._normalize_org_id(user.organization_id)
            device_org = self._org_for_device(tx, source_device_id)
            if device_org is not None and device_org != source_org:
                raise PermissionError("source device belongs to a different organization")
            conversation = ConversationRecord(
                conversation_id=_id("conv"), title=title or "New conversation", actor=actor, organization_id=source_org,
                source_device_id=source_device_id, created_at=tx.now, updated_at=tx.now,
            )
            self._insert(tx, CONVERSATIONS, conversation)
            self._event(tx, "conversation.created", actor, {"conversation_id": conversation.conversation_id, "title": conversation.title, "organization_id": source_org})
            result = self._record(conversation)
            self._idempotency_store(tx, actor=actor, endpoint="conversations.create", key=idempotency_key, payload=idempotency_payload, response=result)
            return result

        return self._transaction(body)

    def get_conversation(self, conversation_id: str, *, actor: str) -> dict[str, Any]:
        return self._record(self._read(lambda tx: self._owned_conversation(tx, conversation_id, actor)))

    def list_conversations(self, actor: Optional[str] = None) -> list[dict[str, Any]]:
        def body(tx: _Tx) -> list[dict[str, Any]]:
            if actor:
                rows = self._select(tx, CONVERSATIONS, "actor = %(actor)s AND organization_id = %(organization_id)s", {"actor": actor, "organization_id": self._org_for_actor(tx, actor)}, order="updated_at DESC", limit=LIST_LIMIT)
            else:
                rows = self._select(tx, CONVERSATIONS, "", {}, order="updated_at DESC", limit=LIST_LIMIT)
            return [self._record(row) for row in rows]

        return self._read(body)

    def list_messages(self, conversation_id: str, actor: Optional[str] = None) -> list[dict[str, Any]]:
        def body(tx: _Tx) -> list[dict[str, Any]]:
            conversation: Optional[ConversationRecord] = self._get(tx, CONVERSATIONS, conversation_id)
            if not conversation:
                raise KeyError("conversation not found")
            if actor and (conversation.actor != actor or self._org_for_actor(tx, actor) != self._normalize_org_id(conversation.organization_id)):
                return []
            # Messages another actor wrote into the conversation are never chat history.
            rows = self._select(
                tx, MESSAGES, "organization_id = %(organization_id)s AND conversation_id = %(conversation_id)s AND actor = %(actor)s",
                {"organization_id": conversation.organization_id, "conversation_id": conversation_id, "actor": conversation.actor},
                order="created_at, message_id",
            )
            return [self._record(row) for row in rows]

        return self._read(body)

    def add_chat_user_message(self, *, actor: str, conversation_id: str, content: str, source_device_id: Optional[str] = None) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            conversation = self._owned_conversation(tx, conversation_id, actor, for_update=True)
            message = MessageRecord(message_id=_id("msg"), conversation_id=conversation_id, role="user", content=content, actor=actor, organization_id=conversation.organization_id, source_device_id=source_device_id, created_at=tx.now)
            self._insert(tx, MESSAGES, message)
            conversation.updated_at = tx.now
            self._update(tx, CONVERSATIONS, conversation)
            self._event(tx, "conversation.ask.requested", actor, {"message_id": message.message_id, "conversation_id": conversation_id, "organization_id": conversation.organization_id})
            return self._record(message)

        return self._transaction(body)

    def add_assistant_message(self, *, actor: str, conversation_id: str, content: str, provider: str, model: str, profile: str, usage: Optional[dict[str, Any]] = None, trace_id: Optional[str] = None) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            conversation = self._owned_conversation(tx, conversation_id, actor, for_update=True)
            message = MessageRecord(
                message_id=_id("msg"), conversation_id=conversation_id, role="assistant", content=content, actor=actor,
                organization_id=conversation.organization_id, model_provider=provider, model_name=model, model_profile=profile,
                usage=dict(usage or {}), trace_id=trace_id or _id("trace"), created_at=tx.now,
            )
            self._insert(tx, MESSAGES, message)
            conversation.updated_at = tx.now
            self._update(tx, CONVERSATIONS, conversation)
            self._event(tx, "conversation.ask.completed", actor, {"message_id": message.message_id, "conversation_id": conversation_id, "provider": provider, "model": model, "profile": profile, "trace_id": message.trace_id, "organization_id": conversation.organization_id})
            self._notify(tx, audience="all", title="Omni assistant replied", body=content[:180], event_type="conversation.ask.completed", actor=actor, related_id=conversation_id)
            return self._record(message)

        return self._transaction(body)

    # ---------------------------------------------------- tasks and approvals

    def _create_approval(self, tx: _Tx, *, task: TaskRecord, actor: str, risk: Literal["low", "medium", "high", "critical"], action: str, reason: str) -> ApprovalRecord:
        approval = ApprovalRecord(
            approval_id=_id("appr"), task_id=task.task_id, risk=risk, action=action, reason=reason, requested_by=actor,
            organization_id=self._normalize_org_id(task.organization_id), created_at=tx.now, expires_at=tx.now + 900,
        )
        self._insert(tx, APPROVALS, approval)
        self._event(tx, "approval.requested", actor, {"approval_id": approval.approval_id, "task_id": task.task_id, "risk": risk, "organization_id": approval.organization_id})
        self._notify(tx, audience="mobile", title="Omni approval required", body=action[:180], event_type="approval.requested", actor=actor, related_id=approval.approval_id)
        self._notify(tx, audience="web_admin", title="High-risk action pending", body=reason, event_type="approval.requested", actor=actor, related_id=approval.approval_id)
        return approval

    def add_message_and_task(self, *, actor: str, conversation_id: str, content: str, source_device_id: Optional[str] = None, requires_desktop_runtime: bool = False, risk: str = "medium", idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            cached = self._idempotency_lookup(tx, actor=actor, endpoint="messages.create", key=idempotency_key, payload=idempotency_payload, lock=True)
            if cached is not None:
                return cached
            conversation = self._owned_conversation(tx, conversation_id, actor)
            task_id = _id("task")
            message = MessageRecord(message_id=_id("msg"), conversation_id=conversation_id, role="user", content=content, actor=actor, organization_id=conversation.organization_id, source_device_id=source_device_id, task_id=task_id, created_at=tx.now)
            task = TaskRecord(
                task_id=task_id, conversation_id=conversation_id, title=(content or "Task")[:120], actor=actor,
                organization_id=conversation.organization_id, status="queued", requires_desktop_runtime=bool(requires_desktop_runtime),
                idempotency_key=idempotency_key, created_at=tx.now, updated_at=tx.now,
            )
            self._insert(tx, MESSAGES, message)
            self._insert(tx, TASKS, task)
            self._event(tx, "message.created", actor, {"message_id": message.message_id, "conversation_id": conversation_id, "task_id": task_id, "organization_id": conversation.organization_id})
            self._event(tx, "task.created", actor, {"task_id": task_id, "conversation_id": conversation_id, "requires_desktop_runtime": requires_desktop_runtime, "organization_id": conversation.organization_id})
            approval: Optional[ApprovalRecord] = None
            if risk in {"high", "critical"} or requires_desktop_runtime:
                approval = self._create_approval(tx, task=task, actor=actor, risk="high" if risk not in {"critical"} else "critical", action=task.title, reason="Desktop/runtime action requires cross-device approval")
                task.status = "blocked"
                task.approval_id = approval.approval_id
                self._update(tx, TASKS, task)
            result = {"message": self._record(message), "task": self._record(task), "approval": self._record(approval) if approval else None}
            self._idempotency_store(tx, actor=actor, endpoint="messages.create", key=idempotency_key, payload=idempotency_payload, response=result)
            return result

        return self._transaction(body)

    def decide_approval(self, *, approval_id: str, actor: str, decision: Literal["approved", "rejected"], reason: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        def body(tx: _Tx) -> Any:
            cached = self._idempotency_lookup(tx, actor=actor, endpoint="approvals.decide", key=idempotency_key, payload=idempotency_payload, lock=True)
            if cached is not None:
                return cached["approval"] if isinstance(cached, dict) and "approval" in cached else cached
            approval: Optional[ApprovalRecord] = self._get(tx, APPROVALS, approval_id, for_update=True)
            if not approval:
                raise KeyError("approval not found")
            self._require_actor_org(tx, actor, approval.organization_id)
            if approval.status != "pending":
                raise ValueError("approval already decided")
            if approval.expires_at and approval.expires_at < tx.now:
                approval.status = "expired"
                self._update(tx, APPROVALS, approval)
                self._event(tx, "approval.expired", actor, {"approval_id": approval_id, "task_id": approval.task_id, "organization_id": approval.organization_id})
                return _CommitThenRaise(ValueError("approval expired"))
            approval.status = decision
            approval.decided_by = actor
            approval.decision_reason = reason
            approval.decision_idempotency_key = idempotency_key
            approval.decided_at = tx.now
            self._update(tx, APPROVALS, approval)
            task: Optional[TaskRecord] = self._get(tx, TASKS, approval.task_id, for_update=True)
            # Only a task still waiting on this approval moves; a decision must not
            # revive a task that was cancelled meanwhile.
            if task and task.status == "blocked":
                task.status = "queued" if decision == "approved" else "cancelled"
                task.updated_at = tx.now
                self._update(tx, TASKS, task)
            self._event(tx, "approval.decided", actor, {"approval_id": approval_id, "decision": decision, "task_id": approval.task_id, "organization_id": approval.organization_id})
            self._notify(tx, audience="desktop", title=f"Approval {decision}", body=approval.action[:180], event_type="approval.decided", actor=actor, related_id=approval.task_id)
            result = self._record(approval)
            self._idempotency_store(tx, actor=actor, endpoint="approvals.decide", key=idempotency_key, payload=idempotency_payload, response={"approval": result})
            return result

        return self._transaction(body)

    def list_approvals(self, status: Optional[str] = None, actor: Optional[str] = None) -> list[dict[str, Any]]:
        def body(tx: _Tx) -> list[dict[str, Any]]:
            clauses: list[str] = []
            params: dict[str, Any] = {}
            if actor:
                clauses.append("organization_id = %(organization_id)s")
                params["organization_id"] = self._org_for_actor(tx, actor)
            if status:
                clauses.append("status = %(status)s")
                params["status"] = status
            rows = self._select(tx, APPROVALS, " AND ".join(clauses), params, order="created_at DESC", limit=LIST_LIMIT)
            return [self._record(row) for row in rows]

        return self._read(body)

    def heartbeat_runtime(self, *, actor: str, device_id: str, status: Literal["online", "offline", "degraded"], version: Optional[str] = None, hostname: Optional[str] = None, active_task_id: Optional[str] = None, capabilities: Optional[list[str]] = None) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            organization_id = self._org_for_device(tx, device_id) or self._org_for_actor(tx, actor)
            if active_task_id:
                tx.cur.execute("SELECT organization_id FROM omnidesk_appsync_tasks WHERE namespace = %s AND task_id = %s", (self.namespace, active_task_id))
                row = tx.cur.fetchone()
                if row and self._normalize_org_id(str(row[0])) != organization_id:
                    raise PermissionError("runtime cannot attach to a task from another organization")
            previous: Optional[RuntimeStatusRecord] = self._get(tx, RUNTIMES, device_id, for_update=True)
            record = RuntimeStatusRecord(device_id=device_id, status=status, organization_id=organization_id, version=version, hostname=hostname, active_task_id=active_task_id, capabilities=list(capabilities or []), last_heartbeat_at=tx.now)
            self._upsert(tx, RUNTIMES, record)
            tx.cur.execute(
                "UPDATE omnidesk_appsync_devices SET online = %s, last_seen_at = %s, updated_at = %s WHERE namespace = %s AND device_id = %s",
                (status == "online", tx.now, tx.now, self.namespace, device_id),
            )
            if runtime_status_changed(previous, record):
                self._event(tx, "runtime.status_changed", actor, {"device_id": device_id, "status": status, "active_task_id": active_task_id, "organization_id": organization_id})
            return self._record(record)

        return self._transaction(body)

    def claim_next_task(self, *, actor: str, device_id: str, lease_seconds: int = 60, capabilities: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
        lease = max(15, min(int(lease_seconds or 60), 600))

        def body(tx: _Tx) -> Optional[dict[str, Any]]:
            self._ensure_user(tx, actor)
            organization_id = self._org_for_device(tx, device_id) or self._org_for_actor(tx, actor)
            tx.cur.execute(CLAIM_NEXT_TASK, {"namespace": self.namespace, "organization_id": organization_id, "device_id": device_id, "now": tx.now, "lease_expires_at": tx.now + lease})
            row = tx.cur.fetchone()
            if not row:
                return None
            task: TaskRecord = self._from_row(TASKS, row)
            self._event(tx, "task.claimed", actor, {"task_id": task.task_id, "device_id": device_id, "lease_expires_at": task.lease_expires_at, "capabilities": list(capabilities or []), "organization_id": organization_id})
            if task.approval_id:
                # Consumed in the claim's transaction, so a re-queued task can never run again under it.
                tx.cur.execute(CONSUME_APPROVAL_SQL, {"namespace": self.namespace, "approval_id": task.approval_id})
                consumed = tx.cur.fetchone()
                if consumed:
                    self._event(tx, "approval.consumed", actor, {"approval_id": task.approval_id, "task_id": task.task_id, "device_id": device_id, "organization_id": self._normalize_org_id(str(consumed[2]))})
            return self._task_for_actor(tx, self._record(task), actor)

        return self._transaction(body)

    def renew_task_lease(self, *, actor: str, task_id: str, device_id: str, lease_seconds: int = 60) -> dict[str, Any]:
        lease = max(15, min(int(lease_seconds or 60), 600))

        def body(tx: _Tx) -> dict[str, Any]:
            organization_id = self._org_for_device(tx, device_id) or self._org_for_actor(tx, actor)
            tx.cur.execute(LEASE_RENEWAL, {"namespace": self.namespace, "organization_id": organization_id, "task_id": task_id, "device_id": device_id, "now": tx.now, "lease_expires_at": tx.now + lease})
            row = tx.cur.fetchone()
            if not row:
                raise KeyError("lease not found")
            return self._task_for_actor(tx, self._record(self._from_row(TASKS, row)), actor)

        return self._transaction(body)

    def get_task(self, task_id: str, actor: Optional[str] = None) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            task: Optional[TaskRecord] = self._get(tx, TASKS, task_id)
            if not task:
                raise KeyError("task not found")
            if actor and self._org_for_actor(tx, actor) != self._normalize_org_id(task.organization_id):
                raise KeyError("task not found")
            return self._task_for_actor(tx, self._record(task), actor)

        return self._read(body)

    def update_task_status(self, *, task_id: str, actor: str, status: TaskStatus, result_summary: Optional[str] = None, assigned_runtime_device_id: Optional[str] = None, device_id: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Apply a status report; ``device_id`` is the device that authenticated the request."""

        def body(tx: _Tx) -> dict[str, Any]:
            cached = self._idempotency_lookup(tx, actor=actor, endpoint="tasks.status", key=idempotency_key, payload=idempotency_payload, lock=True)
            if cached is not None:
                return cached
            task: Optional[TaskRecord] = self._get(tx, TASKS, task_id, for_update=True)
            if not task:
                raise KeyError("task not found")
            reporting_device_id = device_id or assigned_runtime_device_id
            authorize_task_status_change(
                task, actor=actor, actor_organization_id=self._org_for_actor(tx, actor), status=status,
                device_id=device_id, assigned_runtime_device_id=assigned_runtime_device_id,
                reporting_device=self._get(tx, DEVICES, reporting_device_id) if reporting_device_id else None,
                normalize_org=self._normalize_org_id, now=tx.now,
            )
            AppSyncStore._apply_task_status(task, status=status, result_summary=result_summary, now=tx.now)
            self._update(tx, TASKS, task)
            self._event(tx, "task.status", actor, {"task_id": task_id, "status": status, "organization_id": task.organization_id})
            if status in TERMINAL_TASK_STATUSES:
                self._notify(tx, audience="all", title=f"Task {status}", body=task.title[:180], event_type="task.status", actor=actor, related_id=task_id)
            result = self._task_for_actor(tx, self._record(task), actor)
            self._idempotency_store(tx, actor=actor, endpoint="tasks.status", key=idempotency_key, payload=idempotency_payload, response=result)
            return result

        return self._transaction(body)

    # ----------------------------------------------------------- notifications

    def list_notifications(self, audience: Optional[str] = None, unread_only: bool = False, actor: Optional[str] = None) -> list[dict[str, Any]]:
        def body(tx: _Tx) -> list[dict[str, Any]]:
            clauses: list[str] = []
            params: dict[str, Any] = {}
            if actor:
                clauses.append("n.organization_id = %(organization_id)s")
                clauses.append("NOT EXISTS (SELECT 1 FROM omnidesk_appsync_conversations c WHERE c.namespace = n.namespace AND c.conversation_id = n.related_id AND c.actor <> %(actor)s)")
                params.update(organization_id=self._org_for_actor(tx, actor), actor=actor)
            if audience:
                clauses.append("n.audience IN (%(audience)s, 'all')")
                params["audience"] = audience
            if unread_only:
                clauses.append("NOT n.read")
            rows = self._select(tx, NOTIFICATIONS, " AND ".join(clauses), params, order="n.created_at DESC", limit=LIST_LIMIT, alias="n")
            return [self._record(row) for row in rows]

        return self._read(body)

    def pending_push_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        def body(tx: _Tx) -> list[dict[str, Any]]:
            # Queued while the device was unverified, or before it was revoked.
            tx.cur.execute(INELIGIBLE_PUSH_SQL, {"namespace": self.namespace, "now": tx.now})
            rows = self._select(tx, PUSH_OUTBOX, "status = 'pending'", {}, order="created_at", limit=max(1, min(int(limit or 100), 500)))
            return [self._record(row) for row in rows]

        return self._transaction(body)

    def mark_push_delivery(self, *, push_id: str, status: Literal["sent", "failed"], error: Optional[str] = None) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            item: Optional[PushOutboxRecord] = self._get(tx, PUSH_OUTBOX, push_id, for_update=True)
            if not item:
                raise KeyError("push outbox item not found")
            item.status = status
            item.attempt_count += 1
            item.last_error = error
            item.updated_at = tx.now
            self._update(tx, PUSH_OUTBOX, item)
            self._event(tx, "push.delivery", "push-provider", {"push_id": push_id, "status": status, "organization_id": item.organization_id})
            return self._record(item)

        return self._transaction(body)

    # --------------------------------------------------------------- bootstrap

    def bootstrap(self, actor: str, *, role: str = "viewer") -> dict[str, Any]:
        """``role`` is the one the gateway authenticated; the stored profile role is
        not a credential. Viewers see only their own devices."""
        existing: Optional[UserProfile] = self._read(lambda tx: self._get(tx, USERS, actor or "unknown"))
        user = self._record(existing) if existing is not None else self.ensure_user(actor)
        organization_id = self._normalize_org_id(user["organization_id"])
        sees_organization_devices = role in {"owner", "operator"}

        def body(tx: _Tx) -> dict[str, Any]:
            # The feed head first. Every event at or below it committed before the
            # reads below, so its effect is in this snapshot; a write committing in
            # between can only be replayed to the client, never skipped.
            tx.cur.execute("SELECT COALESCE(MAX(seq), 0) FROM omnidesk_appsync_sync_events WHERE namespace = %s", (self.namespace,))
            head = int(tx.cur.fetchone()[0])
            device_filter = "organization_id = %(organization_id)s" + ("" if sees_organization_devices else " AND actor = %(actor)s")
            devices = self._select(tx, DEVICES, device_filter, {"organization_id": organization_id, "actor": actor}, order="created_at", limit=LIST_LIMIT)
            runtimes = self._select(tx, RUNTIMES, "organization_id = %(organization_id)s", {"organization_id": organization_id}, order="last_heartbeat_at DESC", limit=LIST_LIMIT)
            approvals = self._select(tx, APPROVALS, "organization_id = %(organization_id)s AND status = 'pending'", {"organization_id": organization_id}, order="created_at DESC", limit=LIST_LIMIT)
            notifications = self._select(
                tx, NOTIFICATIONS,
                "n.organization_id = %(organization_id)s AND NOT EXISTS (SELECT 1 FROM omnidesk_appsync_conversations c WHERE c.namespace = n.namespace AND c.conversation_id = n.related_id AND c.actor <> %(actor)s)",
                {"organization_id": organization_id, "actor": actor}, order="n.created_at DESC", limit=50, alias="n",
            )
            return {
                "organization": {**self._record(self.organization), "organization_id": organization_id},
                "user": {**user, "role": role},
                "devices": [public_device_view(self._record(v)) for v in devices],
                "runtime_status": [self._record(v) for v in runtimes],
                "pending_approvals": [self._record(v) for v in approvals],
                "notifications": [self._record(v) for v in notifications],
                "sync_seq": head,
            }

        return self._read(body)

    # -------------------------------------------------------------- sync feed

    def sync_since(self, since_seq: int, actor: Optional[str] = None) -> dict[str, Any]:
        """Read the sync feed: one indexed, actor-scoped statement, no locks."""
        since = max(0, int(since_seq or 0))
        params: dict[str, Any] = {"namespace": self.namespace, "since": since, "limit": SYNC_EVENT_WINDOW}
        owners: dict[str, str] = {}
        with self._connect() as conn, conn.cursor() as cur:
            if actor:
                cur.execute(ACTOR_ORGANIZATION_SQL, (self.namespace, actor))
                row = cur.fetchone()
                params["organization_id"] = self._normalize_org_id(row[0]) if row else self.organization_for_new_actor(actor)
                cur.execute(SYNC_ORGANIZATION_WINDOW_SQL, params)
            else:
                cur.execute(SYNC_WINDOW_SQL, params)
            rows: list[tuple[Any, ...]] = cur.fetchall()
            head = int(rows[0][0]) if rows else since
            events: list[dict[str, Any]] = []
            references: set[str] = set()
            for r in rows:
                if r[1] is None:
                    continue
                payload: dict[str, Any] = r[5] if isinstance(r[5], dict) else json.loads(r[5] or "{}")
                references.update(str(payload.get(key) or "") for key in ("conversation_id", "related_id"))
                events.append({"seq": int(r[1]), "organization_id": r[2], "event_type": r[3], "actor": r[4], "payload": payload, "ts": float(r[6])})
            references.discard("")
            if actor and references:
                cur.execute(CONVERSATION_OWNERS_SQL, (self.namespace, sorted(references)))
                owners = {str(r[0]): str(r[1]) for r in cur.fetchall()}
        if actor:
            scoped = (scope_sync_event_to_actor(event, actor=actor, conversation_owner=owners.get) for event in events)
            events = [event for event in scoped if event is not None]
        return {"sync_seq": head, "events": events}

    # ---------------------------------------------------- offline sync (local)

    def set_network_state(self, state: str, *, reason: Optional[str] = None) -> dict[str, Any]:
        allowed = {"offline", "local_only", "online_detected", "reconnecting", "syncing", "update_checking", "healthy", "failed", "rollback"}
        if state not in allowed:
            raise ValueError("invalid network state")
        with self._lock:
            changed = self.network_state.get("state") != state or self.network_state.get("reason") != reason
            self.network_state = {"state": state, "reason": reason, "updated_at": _now()}
            snapshot = dict(self.network_state)
        if changed:
            # Connectivity is this process's own; only changes are events.
            self._transaction(lambda tx: self._event(tx, "network.state", "system", snapshot))
        return snapshot

    def enqueue_local_operation(self, *, actor: str, operation_type: str, payload: dict[str, Any], device_id: Optional[str] = None, idempotency_key: Optional[str] = None, conflict_policy: ConflictPolicy = "manual-review") -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            organization_id = self._org_for_actor(tx, actor)
            data = dict(payload or {})
            operation_id = _id("op")
            key = idempotency_key or data.get("idempotency_key") or operation_id
            record = LocalOutboxRecord(
                operation_id=operation_id, operation_type=operation_type, payload=data, actor=actor or "unknown",
                organization_id=organization_id, device_id=device_id or data.get("device_id") or data.get("source_device_id"),
                idempotency_key=str(key), payload_hash=_fingerprint(data), conflict_policy=conflict_policy,
                created_at=tx.now, updated_at=tx.now,
            )
            self._insert(tx, LOCAL_OUTBOX, record)
            self._operation_log(tx, "operation.enqueued", {"operation_id": operation_id, "operation_type": operation_type, "organization_id": organization_id})
            return self._record(record)

        return self._transaction(body)

    def pending_local_outbox(self, *, actor: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        def body(tx: _Tx) -> list[dict[str, Any]]:
            clauses = ["status IN ('pending', 'failed')", "(next_retry_at IS NULL OR next_retry_at <= %(now)s)"]
            params: dict[str, Any] = {"now": tx.now}
            if actor:
                clauses.append("organization_id = %(organization_id)s")
                params["organization_id"] = self._org_for_actor(tx, actor)
            rows = self._select(tx, LOCAL_OUTBOX, " AND ".join(clauses), params, order="created_at", limit=max(1, min(int(limit or 100), 500)))
            return [self._record(row) for row in rows]

        return self._read(body)

    def _mark_synced(self, tx: _Tx, operation_id: str, *, remote_seq: Optional[int]) -> dict[str, Any]:
        item: Optional[LocalOutboxRecord] = self._get(tx, LOCAL_OUTBOX, operation_id, for_update=True)
        if not item:
            raise KeyError("local outbox operation not found")
        item.status = "synced"
        item.remote_seq = remote_seq
        item.updated_at = tx.now
        self._update(tx, LOCAL_OUTBOX, item)
        self._operation_log(tx, "operation.synced", {"operation_id": operation_id, "remote_seq": remote_seq, "organization_id": item.organization_id})
        return self._record(item)

    def mark_local_operation_synced(self, operation_id: str, *, remote_seq: Optional[int] = None) -> dict[str, Any]:
        return self._transaction(lambda tx: self._mark_synced(tx, operation_id, remote_seq=remote_seq))

    def mark_local_operation_failed(self, operation_id: str, *, error: str, retry_delay_seconds: int = 60) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            item: Optional[LocalOutboxRecord] = self._get(tx, LOCAL_OUTBOX, operation_id, for_update=True)
            if not item:
                raise KeyError("local outbox operation not found")
            item.status = "failed"
            item.retry_count += 1
            item.last_error = str(error)[:1000]
            item.next_retry_at = tx.now + max(1, int(retry_delay_seconds or 60))
            item.updated_at = tx.now
            self._update(tx, LOCAL_OUTBOX, item)
            self._operation_log(tx, "operation.failed", {"operation_id": operation_id, "error": item.last_error, "retry_count": item.retry_count, "organization_id": item.organization_id})
            return self._record(item)

        return self._transaction(body)

    def _record_cursor(self, tx: _Tx, *, actor: str, remote: str, since_seq: int, device_id: Optional[str]) -> dict[str, Any]:
        organization_id = self._org_for_actor(tx, actor)
        cursor_id = f"{organization_id}:{actor or 'unknown'}:{remote or 'default'}:{device_id or ''}"
        record = SyncCursorRecord(cursor_id=cursor_id, remote=remote or "default", actor=actor or "unknown", organization_id=organization_id, device_id=device_id, since_seq=max(0, int(since_seq or 0)), updated_at=tx.now)
        self._upsert(tx, SYNC_CURSORS, record)
        self._operation_log(tx, "sync.cursor.updated", {"cursor_id": cursor_id, "since_seq": record.since_seq, "organization_id": organization_id})
        return self._record(record)

    def record_sync_cursor(self, *, actor: str, remote: str, since_seq: int, device_id: Optional[str] = None) -> dict[str, Any]:
        return self._transaction(lambda tx: self._record_cursor(tx, actor=actor, remote=remote, since_seq=since_seq, device_id=device_id))

    def record_remote_events(self, *, actor: str, events: list[dict[str, Any]], remote: str = "default", device_id: Optional[str] = None, conflict_policy: ConflictPolicy = "manual-review") -> dict[str, Any]:
        return self._transaction(lambda tx: self._record_remote_events(tx, actor=actor, events=events, remote=remote, device_id=device_id, conflict_policy=conflict_policy))

    def _record_remote_events(self, tx: _Tx, *, actor: str, events: list[dict[str, Any]], remote: str, device_id: Optional[str], conflict_policy: ConflictPolicy) -> dict[str, Any]:
        organization_id = self._org_for_actor(tx, actor)
        applied = 0
        duplicates = 0
        conflicts: list[dict[str, Any]] = []
        max_seq = 0
        for event in events:
            remote_seq = int(event.get("seq", 0) or 0)
            max_seq = max(max_seq, remote_seq)
            payload = _payload_dict(event.get("payload"))
            remote_event_id = str(event.get("event_id") or event.get("operation_id") or f"{remote}:{remote_seq}:{event.get('event_type', 'event')}")
            # Event ids and idempotency keys are client-chosen (or predictable), so
            # dedupe and outbox matching only ever see the caller's own records.
            inbox_key = AppSyncStore._inbox_key(organization_id, actor, remote_event_id)
            existing: Optional[LocalInboxRecord] = self._get(tx, LOCAL_INBOX, inbox_key, for_update=True)
            if existing is not None:
                duplicates += 1
                existing.status = "duplicate"
                self._update(tx, LOCAL_INBOX, existing)
                continue
            key = str(event.get("idempotency_key") or payload.get("idempotency_key") or "")
            payload_hash = _fingerprint(payload)
            matched: list[LocalOutboxRecord] = []
            if key:
                # Locked, so a replica that recorded a conflict on the same operation
                # first is waited for, and the operation is then no longer matched
                # (READ COMMITTED re-checks the row): it is never marked synced
                # while its conflict is open.
                matched = self._select(
                    tx, LOCAL_OUTBOX,
                    "idempotency_key = %(key)s AND status IN ('pending', 'failed', 'syncing') AND actor = %(actor)s AND organization_id = %(organization_id)s",
                    {"key": key, "actor": actor, "organization_id": organization_id}, order="created_at", for_update=True,
                )
            inbox = LocalInboxRecord(remote_event_id=inbox_key, event_type=str(event.get("event_type") or "event"), payload=payload, actor=actor, organization_id=organization_id, source_device_id=device_id, idempotency_key=key or None, payload_hash=payload_hash, status="applied", received_at=tx.now)
            if matched and any(item.payload_hash != payload_hash for item in matched):
                conflicts.append(self._open_conflict(tx, operation=matched[0], remote_event_id=remote_event_id, actor=actor, organization_id=organization_id, strategy=conflict_policy, remote_payload=payload, conflict_type="idempotency_payload_mismatch"))
                inbox.status = "conflict"
                self._insert(tx, LOCAL_INBOX, inbox)
                continue
            self._insert(tx, LOCAL_INBOX, inbox)
            if matched:
                self._mark_synced(tx, matched[0].operation_id, remote_seq=remote_seq)
            applied += 1
        if max_seq:
            self._record_cursor(tx, actor=actor, remote=remote, since_seq=max_seq, device_id=device_id)
        self._operation_log(tx, "sync.remote_events.recorded", {"applied": applied, "duplicates": duplicates, "conflicts": len(conflicts), "organization_id": organization_id})
        return {"applied": applied, "duplicates": duplicates, "conflicts": conflicts, "cursor": max_seq}

    def _open_conflict(self, tx: _Tx, *, operation: LocalOutboxRecord, remote_event_id: Optional[str], actor: str, organization_id: str, strategy: ConflictPolicy, remote_payload: dict[str, Any], conflict_type: str) -> dict[str, Any]:
        conflict = SyncConflictRecord(
            conflict_id=_id("conflict"), operation_id=operation.operation_id, remote_event_id=remote_event_id,
            conflict_type=conflict_type, strategy=strategy, actor=actor or "unknown", organization_id=organization_id,
            local_payload=dict(operation.payload or {}), remote_payload=dict(remote_payload or {}), created_at=tx.now,
        )
        self._insert(tx, SYNC_CONFLICTS, conflict)
        tx.cur.execute(
            "UPDATE omnidesk_appsync_local_outbox SET status = 'conflict', updated_at = %s WHERE namespace = %s AND operation_id = %s",
            (tx.now, self.namespace, operation.operation_id),
        )
        self._operation_log(tx, "sync.conflict.opened", {"conflict_id": conflict.conflict_id, "operation_id": operation.operation_id, "strategy": strategy, "organization_id": organization_id})
        return self._record(conflict)

    def receive_outbox_operations(self, *, actor: str, operations: list[dict[str, Any]], remote: str = "default", device_id: Optional[str] = None, conflict_policy: ConflictPolicy = "manual-review") -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        for operation in operations:
            payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
            events.append({
                "seq": int(operation.get("remote_seq") or operation.get("seq") or 0),
                "operation_id": operation.get("operation_id") or operation.get("id"),
                "event_type": operation.get("operation_type") or operation.get("event_type") or "operation.replay",
                "actor": operation.get("actor") or actor,
                "idempotency_key": operation.get("idempotency_key"),
                "payload": payload,
            })

        def body(tx: _Tx) -> dict[str, Any]:
            result = self._record_remote_events(tx, actor=actor, events=events, remote=remote, device_id=device_id, conflict_policy=conflict_policy)
            from omnidesk_agent.appsync.offline_sync_apply import apply_uploaded_operations_sql

            return {**result, **apply_uploaded_operations_sql(self, tx, actor=actor, operations=operations, remote=remote, device_id=device_id)}

        return self._transaction(body)

    def replay_operation_log(self, *, actor: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit or 200), 2000))

        def body(tx: _Tx) -> list[dict[str, Any]]:
            organization_id = self._org_for_actor(tx, actor) if actor else None
            tx.cur.execute(OPERATION_LOG_REPLAY_SQL, {"namespace": self.namespace, "organization_id": organization_id, "limit": bounded})
            entries: list[dict[str, Any]] = []
            for r in tx.cur.fetchall():
                payload = r[2] if isinstance(r[2], dict) else json.loads(r[2] or "{}")
                entries.append({"seq": int(r[0]), "ts": float(r[4]), "event": r[1], "payload": {**payload, "organization_id": r[3]}})
            return entries

        return self._read(body)

    def sync_state(self, *, actor: Optional[str] = None) -> dict[str, Any]:
        def body(tx: _Tx) -> dict[str, Any]:
            organization_id = self._org_for_actor(tx, actor) if actor else None
            tx.cur.execute(SYNC_STATE_COUNTS_SQL, {"namespace": self.namespace, "organization_id": organization_id})
            counts = tx.cur.fetchone()
            cursors = self._select(
                tx, SYNC_CURSORS, "organization_id = %(organization_id)s" if actor else "",
                {"organization_id": organization_id} if actor else {}, order="updated_at DESC", limit=LIST_LIMIT,
            )
            return {
                "network_state": dict(self.network_state),
                "outbox": {"pending": int(counts[0]), "synced": int(counts[1]), "conflict": int(counts[2])},
                "inbox": {"received": int(counts[3])},
                "conflicts": {"open": int(counts[4])},
                "cursors": [self._record(cursor) for cursor in cursors],
            }

        return self._read(body)


def _lock_key_legacy(namespace: str) -> int:
    """The previous release's namespace lock key (see LEGACY_EXCLUSIVE_LOCK_NOTE)."""
    digest = hashlib.sha256(f"omnidesk-appsync-state:{namespace}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _concurrent_insert() -> BaseException:
    """Another transaction created the same row first; the retry sees it."""
    try:
        from psycopg import errors  # type: ignore

        return errors.UniqueViolation("row was created concurrently")
    except ModuleNotFoundError:  # pragma: no cover
        return RuntimeError("row was created concurrently")


__all__ = [
    "MigratedMultiInstancePostgresAppSyncStore",
    "PoolTimeout",
    "PostgresTimeouts",
    "PsycopgConnectionPool",
    "SYNC_EVENT_WINDOW",
]
