from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_agent.storage.sqlite import connect_sqlite


@dataclass(frozen=True)
class BreakGlassSession:
    session_id: str
    actor: str
    reason: str
    expires_at: float
    approved_by: str
    active: bool


class BreakGlassStore:
    """Time-boxed emergency access ledger.

    Break-glass is intentionally explicit, short-lived, auditable, and separate
    from normal approvals. The store is local-first so it works during outages,
    while every transition is appended to the main security audit log for WORM
    checkpointing / external mirroring.
    """

    def __init__(self, db_path: Path, *, audit_log: Path):
        self.db_path = Path(db_path).expanduser()
        self.audit_log = Path(audit_log).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS break_glass_sessions (
                  session_id TEXT PRIMARY KEY,
                  actor TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  approved_by TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  expires_at REAL NOT NULL,
                  revoked_at REAL,
                  metadata_json TEXT NOT NULL
                )
                """
            )

    def open(self, *, session_id: str, actor: str, reason: str, approved_by: str, ttl_seconds: int = 900, metadata: dict[str, Any] | None = None) -> BreakGlassSession:
        actor = actor.strip()
        approved_by = approved_by.strip()
        reason = reason.strip()
        if not actor or not approved_by or not reason:
            raise ValueError("actor, approved_by, and reason are required")
        if actor == approved_by:
            raise PermissionError("break-glass approver must be distinct from actor")
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("break-glass ttl must be between 1 and 3600 seconds")
        now = time.time()
        expires_at = now + int(ttl_seconds)
        payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                INSERT INTO break_glass_sessions(session_id, actor, reason, approved_by, created_at, expires_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, actor, reason, approved_by, now, expires_at, payload),
            )
        session = BreakGlassSession(session_id, actor, reason, expires_at, approved_by, True)
        self._audit("break_glass.open", session=session, metadata=metadata or {})
        return session

    def revoke(self, session_id: str, *, revoked_by: str) -> None:
        revoked_by = revoked_by.strip()
        if not revoked_by:
            raise ValueError("revoked_by is required")
        with connect_sqlite(self.db_path) as con:
            con.execute("UPDATE break_glass_sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL", (time.time(), session_id))
        self._audit("break_glass.revoke", session_id=session_id, revoked_by=revoked_by)

    def get(self, session_id: str) -> BreakGlassSession:
        with connect_sqlite(self.db_path) as con:
            row = con.execute(
                "SELECT session_id, actor, reason, approved_by, expires_at, revoked_at FROM break_glass_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"break-glass session not found: {session_id}")
        active = bool(row[5] is None and float(row[4]) > time.time())
        return BreakGlassSession(str(row[0]), str(row[1]), str(row[2]), float(row[4]), str(row[3]), active)

    def assert_active(self, session_id: str, *, actor: str) -> BreakGlassSession:
        session = self.get(session_id)
        if not session.active:
            raise PermissionError("break-glass session is not active")
        if session.actor != actor:
            raise PermissionError("break-glass session actor mismatch")
        return session

    def _audit(self, event: str, **fields: Any) -> None:
        record = {"event": event, "ts": time.time(), **fields}
        with self.audit_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=lambda o: o.__dict__) + "\n")
