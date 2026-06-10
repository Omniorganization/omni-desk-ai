from __future__ import annotations
import json, sqlite3, time
from pathlib import Path
from typing import Any
class UpgradeMemory:
    def __init__(self, db_path: Path):
        self.db_path=db_path.expanduser(); self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn=sqlite3.connect(str(self.db_path), check_same_thread=False); self.conn.row_factory=sqlite3.Row
        self.conn.execute("""CREATE TABLE IF NOT EXISTS upgrade_memory (upgrade_id TEXT PRIMARY KEY, created_at REAL NOT NULL, change_type TEXT NOT NULL, target TEXT NOT NULL, before_success_rate REAL, after_success_rate REAL, rollback INTEGER NOT NULL DEFAULT 0, side_effects TEXT, verdict TEXT NOT NULL, human_feedback TEXT, metadata TEXT)"""); self.conn.commit()
    def record(self, record: dict[str, Any]) -> None:
        self.conn.execute("""INSERT OR REPLACE INTO upgrade_memory (upgrade_id, created_at, change_type, target, before_success_rate, after_success_rate, rollback, side_effects, verdict, human_feedback, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (record["upgrade_id"], record.get("created_at", time.time()), record.get("change_type","workflow"), record.get("target","unknown"), record.get("before_success_rate"), record.get("after_success_rate"), 1 if record.get("rollback") else 0, json.dumps(record.get("side_effects", []), ensure_ascii=False), record.get("verdict","unknown"), record.get("human_feedback"), json.dumps(record.get("metadata", {}), ensure_ascii=False))); self.conn.commit()
    def effectiveness(self, change_type: str | None=None, target: str | None=None) -> dict:
        clauses=[]; params=[]
        if change_type: clauses.append("change_type=?"); params.append(change_type)
        if target: clauses.append("target=?"); params.append(target)
        where="WHERE "+" AND ".join(clauses) if clauses else ""
        rows=self.conn.execute(f"SELECT * FROM upgrade_memory {where}", params).fetchall(); items=[dict(r) for r in rows]; total=len(items) or 1
        effective=sum(1 for i in items if i["verdict"]=="effective"); rollback=sum(1 for i in items if i["rollback"]); rejected=sum(1 for i in items if i["verdict"]=="rejected")
        return {"count":len(items),"effective_rate":effective/total,"rollback_rate":rollback/total,"rejected_rate":rejected/total,"recommendation":"continue" if effective/total>=0.5 and rollback/total<0.2 else "deprioritize"}
    def recent(self, limit:int=20)->list[dict]: return [dict(r) for r in self.conn.execute("SELECT * FROM upgrade_memory ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]
