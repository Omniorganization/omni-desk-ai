from __future__ import annotations

import pytest

from omnidesk_agent.storage.sqlite import connect_sqlite


def test_connect_sqlite_context_manager_closes_connection(tmp_path):
    with connect_sqlite(tmp_path / "close.sqlite3") as con:
        con.execute("CREATE TABLE demo(id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO demo(id) VALUES(1)")

    with pytest.raises(Exception):
        con.execute("SELECT 1")
