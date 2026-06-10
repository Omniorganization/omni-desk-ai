from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


def connect_sqlite(db_path: Path, *, timeout: float = 30.0, isolation_level: Optional[str] = None) -> sqlite3.Connection:
    """Open a SQLite connection with production-safe defaults for local agents.

    Defaults:
      - WAL journal for concurrent readers/writers.
      - busy_timeout to reduce spurious "database is locked" failures.
      - foreign_keys enabled.
      - row factory left to caller, because some stores use tuple rows.
    """
    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=timeout, isolation_level=isolation_level, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
    return con
