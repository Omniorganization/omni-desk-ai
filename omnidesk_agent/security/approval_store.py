from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class ApprovalStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path.expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    proposal TEXT NOT NULL,
                    result TEXT,
                    created_at REAL NOT NULL,
                    decided_at REAL
                )
                """
            )

    def create(self, proposal: dict[str, Any]) -> str:
        aid = str(uuid.uuid4())
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO approvals (id, status, proposal, created_at) VALUES (?, ?, ?, ?)",
                (aid, "pending", json.dumps(proposal, ensure_ascii=False), time.time()),
            )
        return aid

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, status, proposal, result, created_at, decided_at FROM approvals"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC"
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def decide(self, approval_id: str, status: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
        if status not in {"approved", "denied"}:
            raise ValueError("status must be approved or denied")
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "UPDATE approvals SET status = ?, result = ?, decided_at = ? WHERE id = ?",
                (status, json.dumps(result or {}, ensure_ascii=False), time.time(), approval_id),
            )
            row = con.execute(
                "SELECT id, status, proposal, result, created_at, decided_at FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        if not row:
            raise KeyError(approval_id)
        return self._row(row)

    @staticmethod
    def _row(row) -> dict[str, Any]:
        return {
            "id": row[0],
            "status": row[1],
            "proposal": json.loads(row[2]),
            "result": json.loads(row[3]) if row[3] else None,
            "created_at": row[4],
            "decided_at": row[5],
        }
