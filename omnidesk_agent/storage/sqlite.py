from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


class ClosingConnection(sqlite3.Connection):
    """SQLite connection whose context manager commits/rolls back and closes.

    The stdlib sqlite3.Connection context manager does not close the database;
    it only manages transactions. This subclass preserves transaction semantics
    while making `with connect_sqlite(...) as con:` resource-safe.
    """

    def __exit__(self, exc_type, exc, tb):  # type: ignore[override]
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False

    def __del__(self) -> None:  # pragma: no cover - interpreter-level safety net
        try:
            self.close()
        except Exception:
            pass


def connect_sqlite(db_path: Path, *, timeout: float = 30.0, isolation_level: Optional[str] = None) -> sqlite3.Connection:
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
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
    return con
