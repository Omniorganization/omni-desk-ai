from __future__ import annotations

from omnidesk_agent.storage.migrations import Migration, SQLiteMigrationRunner


def test_sqlite_migration_runner_applies_once(tmp_path):
    db = tmp_path / "m.sqlite3"

    def apply(conn):
        conn.execute("CREATE TABLE demo(id INTEGER PRIMARY KEY)")

    runner = SQLiteMigrationRunner(db, [Migration(1, "create_demo", apply)])
    assert runner.run() == [1]
    assert runner.run() == []
