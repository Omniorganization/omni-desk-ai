from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class RunStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path.expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    original_message TEXT NOT NULL,
                    plan_json TEXT,
                    current_step_index INTEGER NOT NULL DEFAULT 0,
                    results_json TEXT NOT NULL DEFAULT '[]',
                    waiting_approval_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def create(self, original_message: dict[str, Any]) -> str:
        run_id = str(uuid.uuid4())
        now = time.time()
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO runs (id, status, original_message, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, "planned", json.dumps(original_message, ensure_ascii=False), now, now),
            )
        return run_id

    def save_waiting(self, run_id: str, plan_json: dict[str, Any], current_step_index: int, results: list[dict[str, Any]], approval_id: str) -> None:
        self.update(run_id, {
            "status": "waiting_approval",
            "plan_json": json.dumps(plan_json, ensure_ascii=False),
            "current_step_index": current_step_index,
            "results_json": json.dumps(results, ensure_ascii=False),
            "waiting_approval_id": approval_id,
        })

    def complete(self, run_id: str, status: str, results: list[dict[str, Any]]) -> None:
        self.update(run_id, {"status": status, "results_json": json.dumps(results, ensure_ascii=False), "waiting_approval_id": None})

    def update(self, run_id: str, fields: dict[str, Any]) -> None:
        fields = dict(fields)
        fields["updated_at"] = time.time()
        assignments = ", ".join([f"{k}=?" for k in fields])
        values = list(fields.values()) + [run_id]
        with sqlite3.connect(self.db_path) as con:
            con.execute(f"UPDATE runs SET {assignments} WHERE id = ?", values)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                "SELECT id,status,original_message,plan_json,current_step_index,results_json,waiting_approval_id,created_at,updated_at FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "status": row[1],
            "original_message": json.loads(row[2]),
            "plan_json": json.loads(row[3]) if row[3] else None,
            "current_step_index": row[4],
            "results": json.loads(row[5]),
            "waiting_approval_id": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }
