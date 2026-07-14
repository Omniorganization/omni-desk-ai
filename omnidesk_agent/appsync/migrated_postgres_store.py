from __future__ import annotations

import hashlib
import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from omnidesk_agent.appsync.postgres_applying_sync import ApplyingDurablePostgresAppSyncStore
from omnidesk_agent.appsync.postgres_migrations import assert_appsync_schema_current
from omnidesk_agent.appsync.postgres_offline_sync import DurablePostgresAppSyncStore

T = TypeVar("T")


class PsycopgConnectionPool:
    """Small bounded connection pool without a second runtime dependency."""

    def __init__(self, dsn: str, *, max_size: int = 12) -> None:
        self.dsn = dsn
        self.max_size = max(2, int(max_size))
        self._idle: queue.LifoQueue[Any] = queue.LifoQueue(self.max_size)
        self._created = 0
        self._lock = threading.Lock()

    def _new_connection(self) -> Any:
        try:
            import psycopg  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for PostgreSQL AppSync") from exc
        return psycopg.connect(self.dsn)

    def _acquire(self) -> Any:
        try:
            connection = self._idle.get_nowait()
            if not bool(getattr(connection, "closed", False)):
                return connection
        except queue.Empty:
            pass
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
                self._created -= 1
            return self._acquire()
        return connection

    def _release(self, connection: Any, *, broken: bool = False) -> None:
        if broken or bool(getattr(connection, "closed", False)):
            try:
                connection.close()
            finally:
                with self._lock:
                    self._created = max(0, self._created - 1)
            return
        try:
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


class MigratedMultiInstancePostgresAppSyncStore(ApplyingDurablePostgresAppSyncStore):
    """Migration-gated multi-instance compatibility store.

    Chat uses direct SQL transactions. Lower-volume inherited V1 operations are
    refreshed and serialized under a database advisory lock until they are moved
    to dedicated repositories.
    """

    _SERIALIZED_METHODS = frozenset({
        "set_network_state", "enqueue_local_operation", "pending_local_operations",
        "mark_local_operation", "record_remote_event", "record_remote_events",
        "receive_outbox_operations", "record_sync_cursor", "sync_state", "ensure_user",
        "register_device", "create_conversation", "get_conversation", "get_idempotency_response",
        "put_idempotency_response", "add_chat_user_message", "add_assistant_message",
        "add_message_and_task", "decide_approval", "heartbeat_runtime", "claim_next_task",
        "renew_task_lease", "get_task", "update_task_status", "list_conversations",
        "list_messages", "list_approvals", "list_notifications", "register_push_token",
        "pending_push_outbox", "mark_push_delivery", "start_device_enrollment",
        "complete_device_enrollment", "issue_device_challenge", "verify_device_challenge",
        "rotate_device_token", "revoke_device", "verify_device_request_signature", "bootstrap",
        "sync_since",
    })

    def __init__(self, dsn: str, namespace: str = "default", *, local_outbox_enabled: bool = False, pool_size: int = 12) -> None:
        self._connection_pool = PsycopgConnectionPool(dsn, max_size=pool_size)
        self._multi_instance_lock = threading.RLock()
        self._operation_state = threading.local()
        super().__init__(dsn=dsn, namespace=namespace, local_outbox_enabled=local_outbox_enabled)

    def _connect(self) -> Any:
        return self._connection_pool.connection()

    def _ensure_schema(self, conn: Any) -> None:
        assert_appsync_schema_current(conn, namespace=self.namespace)

    def _advisory_key(self) -> int:
        digest = hashlib.sha256(f"omnidesk-appsync-state:{self.namespace}".encode()).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    def _refresh_state(self) -> None:
        DurablePostgresAppSyncStore._load(self)

    def _serialized_call(self, method: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if int(getattr(self._operation_state, "depth", 0)) > 0:
            return method(*args, **kwargs)
        with self._multi_instance_lock, self._connect() as lock_conn:
            lock_conn.autocommit = True
            try:
                with lock_conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_lock(%s)", (self._advisory_key(),))
                self._operation_state.depth = 1
                self._refresh_state()
                return method(*args, **kwargs)
            finally:
                self._operation_state.depth = 0
                with lock_conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (self._advisory_key(),))

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
