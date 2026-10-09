from __future__ import annotations

import time
import logging
import math
from contextlib import contextmanager
from threading import Condition
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
        if not all(math.isfinite(float(value)) and float(value) > 0 for value in (
            acquire_timeout_seconds, connect_timeout_seconds,
        )):
            raise PostgresUnavailable('PostgreSQL pool timeouts must be finite and positive')
        self.acquire_timeout_seconds = max(0.1, float(acquire_timeout_seconds))
        self.connect_timeout_seconds = max(1.0, float(connect_timeout_seconds))
        self._idle: list[Any] = []
        self._condition = Condition()
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

    @staticmethod
    def _close_connection(connection: Any) -> None:
        try:
            connection.close()
        except Exception:
            # Cleanup must neither mask a transaction error nor stop draining
            # other idle connections. Do not put credentials in diagnostics.
            logging.getLogger(__name__).warning('PostgreSQL connection cleanup failed')

    def _acquire(self) -> Any:
        deadline = time.monotonic() + self.acquire_timeout_seconds
        with self._condition:
            while True:
                if self._closed:
                    raise PostgresUnavailable('PostgreSQL connection pool is closed')
                while self._idle:
                    connection = self._idle.pop()
                    if self._usable(connection):
                        self._in_use += 1
                        return connection
                    self._created -= 1
                if self._created < self.max_size:
                    self._created += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PostgresUnavailable(
                        f'PostgreSQL pool acquisition timed out after {self.acquire_timeout_seconds:.1f}s'
                    )
                self._waiters += 1
                try:
                    self._condition.wait(timeout=remaining)
                finally:
                    self._waiters -= 1

        # Opening a socket must not hold the condition: close and stats must
        # remain available while DNS/TLS/authentication is in progress.
        try:
            connection = self._new_connection()
        except BaseException:
            with self._condition:
                self._created -= 1
                self._condition.notify_all()
            raise
        with self._condition:
            if not self._closed:
                self._in_use += 1
                return connection
            self._created -= 1
            self._condition.notify_all()
        self._close_connection(connection)
        raise PostgresUnavailable('PostgreSQL connection pool is closed')

    def _release(self, connection: Any, *, discard: bool = False) -> None:
        with self._condition:
            self._in_use -= 1
            if not discard and not self._closed and self._usable(connection):
                self._idle.append(connection)
                self._condition.notify_all()
                return
            self._created -= 1
            self._condition.notify_all()
        self._close_connection(connection)

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
            except BaseException:
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
        with self._condition:
            return {
                'max_size': self.max_size,
                'created': self._created,
                'in_use': self._in_use,
                'idle': len(self._idle),
                'waiters': self._waiters,
                'closed': self._closed,
            }

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            idle, self._idle = self._idle, []
            self._created -= len(idle)
            self._condition.notify_all()
        # Checked-out transactions finish normally and close on return.
        for connection in idle:
            self._close_connection(connection)
