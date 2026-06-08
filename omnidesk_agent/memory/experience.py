from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class ExperienceStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        cur = self.conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS experiences (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at REAL NOT NULL,
          task TEXT NOT NULL,
          plan TEXT,
          outcome TEXT,
          tags TEXT
        )
        """)
        try:
            cur.execute("CREATE VIRTUAL TABLE IF NOT EXISTS experiences_fts USING fts5(task, plan, outcome, tags, content='experiences', content_rowid='id')")
            cur.execute("""
            CREATE TRIGGER IF NOT EXISTS experiences_ai AFTER INSERT ON experiences BEGIN
              INSERT INTO experiences_fts(rowid, task, plan, outcome, tags) VALUES (new.id, new.task, new.plan, new.outcome, new.tags);
            END;
            """)
        except sqlite3.OperationalError:
            # Some Python builds omit FTS5. LIKE fallback remains available.
            pass
        self.conn.commit()

    def add(self, task: str, plan: str = "", outcome: str = "", tags: list[str] | None = None) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO experiences(created_at, task, plan, outcome, tags) VALUES(?,?,?,?,?)",
            (time.time(), task, plan, outcome, json.dumps(tags or [], ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        try:
            rows = cur.execute(
                """
                SELECT e.* FROM experiences_fts f
                JOIN experiences e ON e.id = f.rowid
                WHERE experiences_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            like = f"%{query}%"
            rows = cur.execute(
                """
                SELECT * FROM experiences
                WHERE task LIKE ? OR plan LIKE ? OR outcome LIKE ? OR tags LIKE ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (like, like, like, like, limit),
            ).fetchall()
        return [dict(r) for r in rows]
