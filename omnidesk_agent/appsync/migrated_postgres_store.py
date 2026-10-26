from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Optional, TypeVar

from omnidesk_agent.appsync.postgres_applying_sync import (
    ApplyingDurablePostgresAppSyncStore,
)
from omnidesk_agent.appsync.postgres_migrations import (
    assert_appsync_schema_current,
)
from omnidesk_agent.appsync.store import scope_sync_event_to_actor

T = TypeVar("T")

# Like the in-memory store, a sync call returns at most the newest 1000 events
# after the cursor and moves the cursor to the head.
SYNC_EVENT_WINDOW = 1000

# One statement, one snapshot: the head and the window are read together. Event
# sequence numbers are assigned and committed under the namespace advisory lock,
# so no uncommitted event can later appear below a committed head.
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


class PsycopgConnectionPool:
    """Small bounded connection pool without a second runtime dependency."""

    def __init__(self, dsn: str, *, max_size: int = 12) -> None:
        self.dsn = dsn
        self.max_size = max(2, int(max_size))
        self._idle: queue.LifoQueue[Any] = queue.LifoQueue(self.max_size)
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    def _new_connection(self) -> Any:
        try:
            import psycopg  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                "psycopg is required for PostgreSQL AppSync"
            ) from exc
        return psycopg.connect(self.dsn)

    def _acquire(self) -> Any:
        if self._closed:
            raise RuntimeError("PostgreSQL connection pool is closed")
        while True:
            try:
                connection = self._idle.get_nowait()
                if not bool(getattr(connection, "closed", False)):
                    return connection
                with self._lock:
                    self._created = max(0, self._created - 1)
            except queue.Empty:
                break
        with self._lock:
            if self._created < self.max_size:
                self._created += 1
                try:
                    return self._new_connection()
                except Exception:
                    self._created -= 1
                    raise
        connection = self._idle.get(timeout=30)
        if bool(getattr(connection, "closed", False)):
            with self._lock:
                self._created = max(0, self._created - 1)
            return self._acquire()
        return connection

    def _release(self, connection: Any, *, broken: bool = False) -> None:
        if broken or self._closed or bool(getattr(connection, "closed", False)):
            try:
                connection.close()
            finally:
                with self._lock:
                    self._created = max(0, self._created - 1)
            return
        try:
            connection.rollback()
            connection.autocommit = False
            self._idle.put_nowait(connection)
        except Exception:
            try:
                connection.close()
            finally:
                with self._lock:
                    self._created = max(0, self._created - 1)

    @contextmanager
    def connection(self) -> Iterator[Any]:
        connection = self._acquire()
        broken = False
        try:
            yield connection
            connection.commit()
        except BaseException:
            broken = True
            try:
                connection.rollback()
            except Exception:
                pass
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
            try:
                connection.close()
            finally:
                with self._lock:
                    self._created = max(0, self._created - 1)


class MigratedMultiInstancePostgresAppSyncStore(
    ApplyingDurablePostgresAppSyncStore
):
    """Migration-gated multi-instance compatibility store.

    Chat uses direct SQL transactions. Lower-volume inherited V1 operations are
    refreshed and serialized under a database advisory lock until they are moved
    to dedicated repositories.
    """

    _SERIALIZED_METHODS = frozenset(
        {
            "set_network_state",
            "enqueue_local_operation",
            "pending_local_operations",
            "pending_local_outbox",
            "mark_local_operation",
            "mark_local_operation_synced",
            "mark_local_operation_failed",
            "record_remote_event",
            "record_remote_events",
            "receive_outbox_operations",
            "record_sync_cursor",
            "sync_state",
            "ensure_user",
            "register_device",
            "create_conversation",
            "get_conversation",
            "get_idempotency_response",
            "put_idempotency_response",
            "add_chat_user_message",
            "add_assistant_message",
            "add_message_and_task",
            "decide_approval",
            "heartbeat_runtime",
            "claim_next_task",
            "renew_task_lease",
            "get_task",
            "update_task_status",
            "list_conversations",
            "list_messages",
            "list_approvals",
            "list_notifications",
            "register_push_token",
            "pending_push_outbox",
            "mark_push_delivery",
            "start_device_enrollment",
            "complete_device_enrollment",
            "issue_device_challenge",
            "verify_device_challenge",
            "rotate_device_token",
            "revoke_device",
            "verify_device_request_signature",
            "register_approval_key",
            "device_approval_key",
            "verify_approval_signature",
            "bootstrap",
            "record_event",
        }
    )

    def __init__(
        self,
        dsn: str,
        namespace: str = "default",
        *,
        local_outbox_enabled: bool = False,
        pool_size: int = 12,
    ) -> None:
        self._connection_pool = PsycopgConnectionPool(
            dsn, max_size=pool_size
        )
        self._multi_instance_lock = threading.RLock()
        self._operation_state = threading.local()
        super().__init__(
            dsn=dsn,
            namespace=namespace,
            local_outbox_enabled=local_outbox_enabled,
        )

    def _connect(self) -> Any:
        return self._connection_pool.connection()

    def _ensure_schema(self, conn: Any) -> None:
        assert_appsync_schema_current(conn, namespace=self.namespace)

    def _advisory_key(self) -> int:
        digest = hashlib.sha256(
            f"omnidesk-appsync-state:{self.namespace}".encode()
        ).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    def _refresh_state(self) -> None:
        self._load()

    def _load(self) -> None:
        super()._load()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT nonce_key, expires_at FROM omnidesk_appsync_device_request_nonces WHERE namespace=%s AND expires_at>%s",
                (self.namespace, time.time()),
            )
            self.device_request_nonces = {str(key): float(expires_at) for key, expires_at in cur.fetchall()}

    def _persist(self) -> None:
        # Mirroring upserts this instance's whole cache. Outside the advisory lock and
        # a fresh reload it would roll back other instances' writes, such as device
        # revocations, consumed approvals and completed enrollments.
        if int(getattr(self._operation_state, "depth", 0)) <= 0:
            raise RuntimeError("AppSync state can only be persisted inside a serialized operation")
        super()._persist()

    def _mirror_normalized(self, cur: Any, Jsonb: Any) -> None:
        super()._mirror_normalized(cur, Jsonb)
        now = time.time()
        cur.execute(
            "DELETE FROM omnidesk_appsync_device_request_nonces WHERE namespace=%s AND expires_at<=%s",
            (self.namespace, now),
        )
        for nonce_key, expires_at in self.device_request_nonces.items():
            if expires_at > now:
                cur.execute(
                    "INSERT INTO omnidesk_appsync_device_request_nonces(namespace, nonce_key, expires_at) VALUES (%s,%s,%s) ON CONFLICT(namespace, nonce_key) DO NOTHING",
                    (self.namespace, nonce_key, expires_at),
                )

    def _serialized_call(
        self, method: Callable[..., T], /, *args: Any, **kwargs: Any
    ) -> T:
        if int(getattr(self._operation_state, "depth", 0)) > 0:
            return method(*args, **kwargs)
        with self._multi_instance_lock, self._connect() as lock_conn:
            lock_conn.autocommit = True
            try:
                with lock_conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_lock(%s)",
                        (self._advisory_key(),),
                    )
                self._operation_state.depth = 1
                self._refresh_state()
                return method(*args, **kwargs)
            finally:
                self._operation_state.depth = 0
                with lock_conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(%s)",
                        (self._advisory_key(),),
                    )

    def __getattribute__(self, name: str) -> Any:
        attribute = super().__getattribute__(name)
        if name.startswith("_"):
            return attribute
        serialized = super().__getattribute__("_SERIALIZED_METHODS")
        if name not in serialized or not callable(attribute):
            return attribute
        state = super().__getattribute__("_operation_state")
        if int(getattr(state, "depth", 0)) > 0:
            return attribute

        def guarded(*args: Any, **kwargs: Any) -> Any:
            return self._serialized_call(attribute, *args, **kwargs)

        guarded.__name__ = getattr(attribute, "__name__", name)
        guarded.__doc__ = getattr(attribute, "__doc__", None)
        return guarded

    def sync_since(self, since_seq: int, actor: Optional[str] = None) -> dict[str, Any]:
        """Read the sync feed straight from PostgreSQL.

        Clients poll this (``/app/ws`` every few seconds per connection), so it does
        not take the namespace advisory lock and reload every table the way the
        serialized operations do.
        """
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

    def close(self) -> None:
        self._connection_pool.close()

    def health_details(self) -> dict[str, Any]:
        return {
            "backend": "postgres",
            "namespace": self.namespace,
            "schema_policy": "migration_gated",
            "multi_instance_consistency": "database_advisory_lock",
            "connection_pool": "bounded",
            "chat_write_path": "direct_transactional_repository",
        }
