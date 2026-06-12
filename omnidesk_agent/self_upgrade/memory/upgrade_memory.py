from __future__ import annotations

import json
import sqlite3
import time
import weakref
from pathlib import Path
from typing import Optional

from omnidesk_agent.storage.sqlite import connect_sqlite


class UpgradeMemory:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser(); self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect_sqlite(self.db_path); self.conn.row_factory = sqlite3.Row; self._closed = False
        self._finalizer = weakref.finalize(self, self.conn.close)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS upgrade_memory (upgrade_id TEXT PRIMARY KEY, created_at REAL NOT NULL, change_type TEXT NOT NULL, target TEXT NOT NULL, before_success_rate REAL, after_success_rate REAL, rollback INTEGER NOT NULL DEFAULT 0, side_effects TEXT, verdict TEXT NOT NULL, human_feedback TEXT, metadata TEXT)"""); self.conn.commit()

    def close(self) -> None:
        if not self._closed:
            if self._finalizer.alive:
                self._finalizer()
            else:
                self.conn.close()
            self._closed = True

    def __enter__(self) -> "UpgradeMemory":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass

    def record(self, record: dict) -> None:
        self.conn.execute("""INSERT OR REPLACE INTO upgrade_memory (upgrade_id, created_at, change_type, target, before_success_rate, after_success_rate, rollback, side_effects, verdict, human_feedback, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (record["upgrade_id"], record.get("created_at", time.time()), record.get("change_type", "workflow"), record.get("target", "unknown"), record.get("before_success_rate"), record.get("after_success_rate"), 1 if record.get("rollback") else 0, json.dumps(record.get("side_effects", []), ensure_ascii=False), record.get("verdict", "unknown"), record.get("human_feedback"), json.dumps(record.get("metadata", {}), ensure_ascii=False))); self.conn.commit()

    def effectiveness(self, change_type: Optional[str] = None) -> dict:
        where = "WHERE change_type=?" if change_type else ""; params = (change_type,) if change_type else ()
        rows = self.conn.execute(f"SELECT * FROM upgrade_memory {where}", params).fetchall(); items = [dict(r) for r in rows]; total = len(items) or 1
        effective = sum(1 for r in items if r["verdict"] == "effective"); rollback = sum(1 for r in items if r["rollback"])
        return {"total": len(items), "effective_rate": effective / total, "rollback_rate": rollback / total, "recommendation": "continue" if effective / total >= 0.6 and rollback / total <= 0.1 else "review"}

    def recent(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM upgrade_memory ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]
