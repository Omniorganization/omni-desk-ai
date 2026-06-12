from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MigrationFn = Callable[[sqlite3.Connection], None]


@dataclass
class Migration:
    version: int
    name: str
    apply: MigrationFn




def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS schema_migrations (
      version INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      applied_at REAL NOT NULL
    )
    """)


def apply_migrations(conn: sqlite3.Connection, migrations: list[Migration]) -> list[int]:
    """Apply migrations on an existing connection.

    Stores that already need connection-specific pragmas or row factories can use
    this helper instead of each inventing its own ad-hoc migration table.
    """
    ensure_schema_migrations(conn)
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    new_versions: list[int] = []
    for migration in sorted(migrations, key=lambda m: m.version):
        if migration.version in applied:
            continue
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
            (migration.version, migration.name, time.time()),
        )
        new_versions.append(migration.version)
    return new_versions


class SQLiteMigrationRunner:
    """Small migration runner for local SQLite stores."""

    def __init__(self, db_path: Path, migrations: list[Migration]):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.migrations = sorted(migrations, key=lambda m: m.version)

    def run(self) -> list[int]:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            with conn:
                return apply_migrations(conn, self.migrations)
        finally:
            conn.close()
