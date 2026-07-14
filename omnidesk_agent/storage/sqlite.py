from __future__ import annotations

import atexit
import sqlite3
import threading
from pathlib import Path
from typing import Literal

SQLiteIsolationLevel = Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None

_OPEN_CONNECTIONS: set["ClosingConnection"] = set()
_OPEN_CONNECTIONS_LOCK = threading.Lock()


class ClosingConnection(sqlite3.Connection):
    """SQLite connection whose context manager commits/rolls back and closes.

    The stdlib sqlite3.Connection context manager does not close the database;
    it only manages transactions. This subclass preserves transaction semantics
    while making `with connect_sqlite(...) as con:` resource-safe. A small
    registry lets tests and runtime shutdown close any accidental escapees
    before Python 3.13 emits unraisable sqlite ResourceWarning entries.
    """

    _in_context: bool = False
    _closed: bool = False

    def __enter__(self):  # type: ignore[override]
        self._in_context = True
        return self

    def __exit__(self, exc_type, exc, tb):  # type: ignore[override]
        try:
            if not self._closed:
                if exc_type is None:
                    self.commit()
                else:
                    self.rollback()
        finally:
            self._in_context = False
            self.close()
        return False

    def close(self) -> None:  # type: ignore[override]
        if self._closed:
            return
        self._closed = True
        with _OPEN_CONNECTIONS_LOCK:
            _OPEN_CONNECTIONS.discard(self)
        super().close()


def close_all_open_connections() -> None:
    with _OPEN_CONNECTIONS_LOCK:
        conns = [con for con in _OPEN_CONNECTIONS if not getattr(con, "_in_context", False)]
    for con in conns:
        try:
            con.close()
        except Exception:
            pass


atexit.register(close_all_open_connections)


def connect_sqlite(
    db_path: Path,
    *,
    timeout: float = 30.0,
    isolation_level: SQLiteIsolationLevel = None,
) -> ClosingConnection:
    """Open a SQLite connection with production-safe defaults for local agents.

    Defaults:
      - WAL journal for concurrent readers/writers.
      - busy_timeout to reduce spurious "database is locked" failures.
      - foreign_keys enabled.
      - context-manager use closes connections to avoid long-running leaks.
    """
    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(
        str(db_path),
        timeout=timeout,
        isolation_level=isolation_level,
        check_same_thread=False,
        factory=ClosingConnection,
    )
    with _OPEN_CONNECTIONS_LOCK:
        _OPEN_CONNECTIONS.add(con)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
    return con
