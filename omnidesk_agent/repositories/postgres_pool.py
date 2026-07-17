from __future__ import annotations

import time
from contextlib import contextmanager
from queue import Empty, Full, LifoQueue
from threading import Lock
from typing import Any, Iterator


class PostgresUnavailable(RuntimeError):
    pass


class SharedPostgresConnectionPool:
    """Small dependency-free bounded psycopg connection pool.

    The enterprise lock already carries psycopg. Keeping the pool in the
    runtime avoids adding a second package while still removing per-query
    TCP/TLS/authentication setup. Connections are committed on successful
    context exit, rolled back on failure, and discarded when unhealthy.
    """

    def __init__(
        self,
        dsn: str,
        *,
        max_size: int = 12,
        acquire_timeout_seconds: float = 5.0,
        connect_timeout_seconds: float = 5.0,
    ) -> None:
        if not str(dsn).strip():
            raise PostgresUnavailable('PostgreSQL DSN is required')
        self.dsn = str(dsn)
        self.max_size = max(2, min(int(max_size), 64))
        self.acquire_timeout_seconds = max(0.1, float(acquire_timeout_seconds))
        self.connect_timeout_seconds = max(1.0, float(connect_timeout_seconds))
        self._idle: LifoQueue[Any] = LifoQueue(maxsize=self.max_size)
        self._lock = Lock()
        self._created = 0
        self._in_use = 0
        self._waiters = 0
        self._closed = False

    def _new_connection(self) -> Any:
        try:
            import psycopg  # type: ignore
        except Exception as exc:  # pragma: no cover - optional enterprise dependency
            raise PostgresUnavailable('Install psycopg[binary] to use postgres repositories') from exc
        return psycopg.connect(
            self.dsn,
            connect_timeout=max(1, int(self.connect_timeout_seconds)),
        )

    @staticmethod
    def _usable(connection: Any) -> bool:
        return not bool(getattr(connection, 'closed', False))

    def _reserve_connection_slot(self) -> bool:
        with self._lock:
            if self._closed:
                raise PostgresUnavailable('PostgreSQL connection pool is closed')
            if self._created >= self.max_size:
                return False
            self._created += 1
            return True

    def _decrement_created(self) -> None:
        with self._lock:
            self._created = max(0, self._created - 1)

    def _acquire(self) -> Any:
        while True:
            try:
                connection = self._idle.get_nowait()
            except Empty:
                connection = None
            if connection is not None:
                if self._usable(connection):
                    with self._lock:
                        self._in_use += 1
                    return connection
                self._decrement_created()
                continue

            if self._reserve_connection_slot():
                try:
                    connection = self._new_connection()
                except Exception:
                    self._decrement_created()
                    raise
                with self._lock:
                    self._in_use += 1
                return connection

            with self._lock:
                if self._closed:
                    raise PostgresUnavailable('PostgreSQL connection pool is closed')
                self._waiters += 1
            try:
                connection = self._idle.get(timeout=self.acquire_timeout_seconds)
            except Empty as exc:
                raise PostgresUnavailable(
                    f'PostgreSQL pool acquisition timed out after {self.acquire_timeout_seconds:.1f}s'
                ) from exc
            finally:
                with self._lock:
                    self._waiters = max(0, self._waiters - 1)
            if self._usable(connection):
                with self._lock:
                    self._in_use += 1
                return connection
            self._decrement_created()

    def _release(self, connection: Any, *, discard: bool = False) -> None:
        with self._lock:
            self._in_use = max(0, self._in_use - 1)
            closed = self._closed
        if discard or closed or not self._usable(connection):
            try:
                connection.close()
            finally:
                self._decrement_created()
            return
        try:
            self._idle.put_nowait(connection)
        except Full:  # defensive: should not happen with in-use accounting
            try:
                connection.close()
            finally:
                self._decrement_created()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        connection = self._acquire()
        discard = False
        try:
            yield connection
            connection.commit()
        except BaseException:
            try:
                connection.rollback()
            except Exception:
                discard = True
            raise
        finally:
            self._release(connection, discard=discard)

    def ping(self) -> dict[str, Any]:
        started = time.monotonic()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                row = cursor.fetchone()
        if not row or int(row[0]) != 1:
            raise PostgresUnavailable('PostgreSQL readiness query returned an invalid result')
        return {
            'ok': True,
            'latency_seconds': time.monotonic() - started,
            'pool': self.stats(),
        }

    def stats(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                'max_size': self.max_size,
                'created': self._created,
                'in_use': self._in_use,
                'idle': self._idle.qsize(),
                'waiters': self._waiters,
                'closed': self._closed,
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        while True:
            try:
                connection = self._idle.get_nowait()
            except Empty:
                break
            try:
                connection.close()
            finally:
                self._decrement_created()
