from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from omnidesk_agent.storage.sqlite import connect_sqlite
from omnidesk_agent.storage.migrations import Migration, apply_migrations
from typing import Any, Optional


class ApprovalStore:
    def __init__(self, db_path: Path, ttl_seconds: int = 600):
        self.db_path = db_path.expanduser()
        self.ttl_seconds = ttl_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    proposal TEXT NOT NULL,
                    result TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    decided_at REAL
                )
                """
            )
            self._migrate(con)
            apply_migrations(con, [Migration(1, "approval_store_schema_baseline", lambda _con: None)])

    def _migrate(self, con: sqlite3.Connection) -> None:
        cols = {row[1] for row in con.execute("PRAGMA table_info(approvals)").fetchall()}
        if "expires_at" not in cols:
            con.execute("ALTER TABLE approvals ADD COLUMN expires_at REAL")

    def create(self, proposal: dict[str, Any], ttl_seconds: Optional[int] = None) -> str:
        aid = str(uuid.uuid4())
        now = time.time()
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        expires_at = now + ttl if ttl and ttl > 0 else None
        with connect_sqlite(self.db_path) as con:
            con.execute(
                "INSERT INTO approvals (id, status, proposal, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (aid, "pending", json.dumps(proposal, ensure_ascii=False), now, expires_at),
            )
        return aid

    def get(self, approval_id: str) -> Optional[dict[str, Any]]:
        with connect_sqlite(self.db_path) as con:
            row = con.execute(
                "SELECT id, status, proposal, result, created_at, expires_at, decided_at FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT id, status, proposal, result, created_at, expires_at, decided_at FROM approvals"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC"
        with connect_sqlite(self.db_path) as con:
            rows = con.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def decide(self, approval_id: str, status: str, result: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if status not in {"approved", "denied"}:
            raise ValueError("status must be approved or denied")
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id, status, proposal, result, created_at, expires_at, decided_at FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if not row:
                raise KeyError(approval_id)
            current = self._row(row)
            if current.get("expires_at") and time.time() > current["expires_at"]:
                raise PermissionError(f"approval expired: {approval_id}")
            if current["status"] != "pending":
                raise PermissionError(f"approval already decided: {approval_id}")
            con.execute(
                "UPDATE approvals SET status = ?, result = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                (status, json.dumps(result or {}, ensure_ascii=False), time.time(), approval_id),
            )
            row = con.execute(
                "SELECT id, status, proposal, result, created_at, expires_at, decided_at FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        return self._row(row)

    def require_approved(self, approval_id: str, expected_proposal: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        approval = self.get(approval_id)
        if not approval:
            raise PermissionError(f"approval not found: {approval_id}")
        if approval.get("expires_at") and time.time() > approval["expires_at"]:
            raise PermissionError(f"approval expired: {approval_id}")
        if approval["status"] != "approved":
            raise PermissionError(f"approval is not approved: {approval_id}")

        if expected_proposal:
            proposal = approval.get("proposal") or {}
            # Strict matching for execution-scope fields. Missing expected fields are ignored for compatibility.
            for key in ("tool", "action", "source", "actor", "run_id", "plan_id", "step_index", "scope_hash"):
                if expected_proposal.get(key) is not None and proposal.get(key) != expected_proposal.get(key):
                    raise PermissionError(f"approval proposal mismatch on {key}")
        return approval

    @staticmethod
    def _row(row) -> dict[str, Any]:
        return {
            "id": row[0],
            "status": row[1],
            "proposal": json.loads(row[2]),
            "result": json.loads(row[3]) if row[3] else None,
            "created_at": row[4],
            "expires_at": row[5],
            "decided_at": row[6],
        }
