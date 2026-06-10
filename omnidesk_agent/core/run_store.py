from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional


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
                    approval_proposal_json TEXT,
                    resume_token TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._migrate(con)

    def _migrate(self, con: sqlite3.Connection) -> None:
        cols = {row[1] for row in con.execute("PRAGMA table_info(runs)").fetchall()}
        for name, ddl in {
            "approval_proposal_json": "ALTER TABLE runs ADD COLUMN approval_proposal_json TEXT",
            "resume_token": "ALTER TABLE runs ADD COLUMN resume_token TEXT",
        }.items():
            if name not in cols:
                con.execute(ddl)

    def create(self, original_message: dict[str, Any]) -> str:
        run_id = str(uuid.uuid4())
        now = time.time()
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                INSERT INTO runs (id, status, original_message, resume_token, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, "planned", json.dumps(original_message, ensure_ascii=False), None, now, now),
            )
        return run_id

    def require_resume_token(self, run_id: str, resume_token: Optional[str]) -> None:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        expected = run.get("resume_token")
        if run.get("status") == "waiting_approval":
            if not expected:
                raise PermissionError("missing stored resume_token")
            if resume_token != expected:
                raise PermissionError("invalid resume_token")
        elif expected:
            raise PermissionError("resume_token should not exist for non-waiting run")

    def save_waiting(
        self,
        run_id: str,
        plan_json: dict[str, Any],
        current_step_index: int,
        results: list[dict[str, Any]],
        approval_id: str,
        approval_proposal: dict[str, Any],
    ) -> str:
        token = secrets.token_urlsafe(32)
        self.update(run_id, {
            "status": "waiting_approval",
            "plan_json": json.dumps(plan_json, ensure_ascii=False),
            "current_step_index": current_step_index,
            "results_json": json.dumps(results, ensure_ascii=False),
            "waiting_approval_id": approval_id,
            "approval_proposal_json": json.dumps(approval_proposal, ensure_ascii=False),
            "resume_token": token,
        })
        return token

    def complete(self, run_id: str, status: str, results: list[dict[str, Any]]) -> None:
        self.update(run_id, {
            "status": status,
            "results_json": json.dumps(results, ensure_ascii=False),
            "waiting_approval_id": None,
            "approval_proposal_json": None,
            "resume_token": None,
        })

    def update(self, run_id: str, fields: dict[str, Any]) -> None:
        fields = dict(fields)
        fields["updated_at"] = time.time()
        assignments = ", ".join([f"{k}=?" for k in fields])
        values = list(fields.values()) + [run_id]
        with sqlite3.connect(self.db_path) as con:
            con.execute(f"UPDATE runs SET {assignments} WHERE id = ?", values)

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                """
                SELECT id,status,original_message,plan_json,current_step_index,results_json,
                       waiting_approval_id,approval_proposal_json,resume_token,created_at,updated_at
                FROM runs WHERE id=?
                """,
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
            "approval_proposal": json.loads(row[7]) if row[7] else None,
            "resume_token": row[8],
            "created_at": row[9],
            "updated_at": row[10],
        }

    def get_by_approval(self, approval_id: str) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute("SELECT id FROM runs WHERE waiting_approval_id=?", (approval_id,)).fetchone()
        return self.get(row[0]) if row else None
