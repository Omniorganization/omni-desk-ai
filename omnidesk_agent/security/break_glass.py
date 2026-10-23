from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_agent.storage.sqlite import connect_sqlite

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending_second_approval"
# Delegated sessions written before second-owner approval existed never had a
# second approver, so they stay unusable after upgrade.
STATUS_LEGACY_UNAPPROVED = "legacy_unapproved"
DEFAULT_APPROVAL_WINDOW_SECONDS = 900


@dataclass(frozen=True)
class BreakGlassSession:
    session_id: str
    actor: str
    reason: str
    expires_at: float
    approved_by: str
    active: bool
    status: str = STATUS_ACTIVE
    second_approver: str | None = None


def has_required_approvals(session: BreakGlassSession) -> bool:
    """A session for another actor needs a second owner distinct from the requester and the target."""
    if session.actor == session.approved_by:
        return True
    second = str(getattr(session, "second_approver", None) or "").strip()
    return bool(second) and second not in {session.actor, session.approved_by}


# The helpers below are shared by the SQLite store here and the PostgreSQL store
# (repositories/postgres_state.py) so both backends enforce the same rules.


def _validate(actor: str, approver: str, reason: str, ttl_seconds: int) -> tuple[str, str, str]:
    actor = actor.strip()
    approver = approver.strip()
    reason = reason.strip()
    if not actor or not approver or not reason:
        raise ValueError("actor, approved_by, and reason are required")
    if ttl_seconds <= 0 or ttl_seconds > 3600:
        raise ValueError("break-glass ttl must be between 1 and 3600 seconds")
    return actor, approver, reason


def validate_self_session(actor: str, approved_by: str, reason: str, ttl_seconds: int) -> tuple[str, str, str]:
    actor, approved_by, reason = _validate(actor, approved_by, reason, ttl_seconds)
    if actor != approved_by:
        raise PermissionError("break-glass for another actor needs a second owner: use request_delegated() and approve()")
    return actor, approved_by, reason


def validate_delegated_request(
    actor: str, requested_by: str, reason: str, ttl_seconds: int, approval_window_seconds: int
) -> tuple[str, str, str]:
    actor, requested_by, reason = _validate(actor, requested_by, reason, ttl_seconds)
    if actor == requested_by:
        raise ValueError("a self-targeted break-glass session does not need a second approval; use open()")
    if approval_window_seconds <= 0 or approval_window_seconds > 3600:
        raise ValueError("break-glass approval window must be between 1 and 3600 seconds")
    return actor, requested_by, reason


def check_second_approver(record: dict[str, Any], approver: str, now: float) -> None:
    """Raise unless `approver` may activate this pending delegated request."""
    if record.get("status") != STATUS_PENDING:
        raise PermissionError("break-glass session is not waiting for a second approval")
    if record.get("revoked_at") is not None:
        raise PermissionError("break-glass request was revoked")
    if float(record.get("expires_at") or 0) <= now:
        raise PermissionError("break-glass request expired before a second approval")
    if approver == record.get("approved_by"):
        raise PermissionError("second approver must be distinct from the requesting owner")
    if approver == record.get("actor"):
        raise PermissionError("the elevated actor cannot approve their own break-glass session")


def session_from_record(record: dict[str, Any]) -> BreakGlassSession:
    actor = str(record["actor"])
    approved_by = str(record["approved_by"])
    # Rows without a status predate second-owner approval: only self-targeted ones stay usable.
    status = str(record.get("status") or (STATUS_ACTIVE if actor == approved_by else STATUS_LEGACY_UNAPPROVED))
    expires_at = float(record.get("expires_at") or 0)
    active = bool(status == STATUS_ACTIVE and record.get("revoked_at") is None and expires_at > time.time())
    second_approver = str(record["second_approver"]) if record.get("second_approver") else None
    return BreakGlassSession(str(record["session_id"]), actor, str(record["reason"]), expires_at, approved_by, active, status, second_approver)


def require_usable(session: BreakGlassSession, actor: str) -> BreakGlassSession:
    if not session.active:
        raise PermissionError("break-glass session is not active")
    if session.actor != actor:
        raise PermissionError("break-glass session actor mismatch")
    if not has_required_approvals(session):
        raise PermissionError("break-glass session for another actor lacks a second owner approval")
    return session


class BreakGlassStore:
    """Time-boxed emergency access ledger.

    Break-glass is intentionally explicit, short-lived, auditable, and separate
    from normal approvals. The store is local-first so it works during outages,
    while every transition is appended to the main security audit log for WORM
    checkpointing / external mirroring.

    An owner may open a session for themselves. A session that elevates another
    actor is only a pending request until a second, distinct owner approves it;
    neither the requester nor the elevated actor can be that second owner.
    """

    def __init__(self, db_path: Path, *, audit_log: Path):
        self.db_path = Path(db_path).expanduser()
        self.audit_log = Path(audit_log).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with connect_sqlite(self.db_path) as con:
            # Serialize the schema check and ALTERs across processes opening the same file.
            con.execute("BEGIN IMMEDIATE")
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
                  metadata_json TEXT NOT NULL,
                  status TEXT,
                  second_approver TEXT,
                  approved_at REAL,
                  ttl_seconds INTEGER
                )
                """
            )
            self._migrate(con)

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        cols = {row[1] for row in con.execute("PRAGMA table_info(break_glass_sessions)").fetchall()}
        for name, ddl in {
            "status": "ALTER TABLE break_glass_sessions ADD COLUMN status TEXT",
            "second_approver": "ALTER TABLE break_glass_sessions ADD COLUMN second_approver TEXT",
            "approved_at": "ALTER TABLE break_glass_sessions ADD COLUMN approved_at REAL",
            "ttl_seconds": "ALTER TABLE break_glass_sessions ADD COLUMN ttl_seconds INTEGER",
        }.items():
            if name not in cols:
                con.execute(ddl)
        con.execute(
            """
            UPDATE break_glass_sessions
            SET status = CASE WHEN actor = approved_by THEN 'active' ELSE 'legacy_unapproved' END
            WHERE status IS NULL
            """
        )

    def open(self, *, session_id: str, actor: str, reason: str, approved_by: str, ttl_seconds: int = 900, metadata: dict[str, Any] | None = None) -> BreakGlassSession:
        actor, approved_by, reason = validate_self_session(actor, approved_by, reason, ttl_seconds)
        now = time.time()
        expires_at = now + int(ttl_seconds)
        payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        self._insert(
            """
            INSERT INTO break_glass_sessions(session_id, actor, reason, approved_by, created_at, expires_at, metadata_json, status, approved_at, ttl_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (session_id, actor, reason, approved_by, now, expires_at, payload, now, int(ttl_seconds)),
        )
        session = BreakGlassSession(session_id, actor, reason, expires_at, approved_by, True)
        self._audit("break_glass.open", session=session, metadata=metadata or {})
        return session

    def request_delegated(
        self,
        *,
        session_id: str,
        actor: str,
        reason: str,
        requested_by: str,
        ttl_seconds: int = 900,
        metadata: dict[str, Any] | None = None,
        approval_window_seconds: int = DEFAULT_APPROVAL_WINDOW_SECONDS,
    ) -> BreakGlassSession:
        """Record a pending session for another actor; it grants nothing until approve()."""
        actor, requested_by, reason = validate_delegated_request(actor, requested_by, reason, ttl_seconds, approval_window_seconds)
        now = time.time()
        approve_by = now + int(approval_window_seconds)
        payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        self._insert(
            """
            INSERT INTO break_glass_sessions(session_id, actor, reason, approved_by, created_at, expires_at, metadata_json, status, ttl_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_second_approval', ?)
            """,
            (session_id, actor, reason, requested_by, now, approve_by, payload, int(ttl_seconds)),
        )
        session = BreakGlassSession(session_id, actor, reason, approve_by, requested_by, False, STATUS_PENDING)
        self._audit("break_glass.request", session=session, metadata=metadata or {})
        return session

    def approve(self, session_id: str, *, approver: str, metadata: dict[str, Any] | None = None) -> BreakGlassSession:
        """Second-owner approval that turns a pending delegated request into an active session."""
        approver = approver.strip()
        if not approver:
            raise ValueError("approver is required")
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT actor, approved_by, status, expires_at, revoked_at, ttl_seconds FROM break_glass_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"break-glass session not found: {session_id}")
            record: dict[str, Any] = dict(zip(("actor", "approved_by", "status", "expires_at", "revoked_at", "ttl_seconds"), row))
            check_second_approver(record, approver, now)
            cur = con.execute(
                """
                UPDATE break_glass_sessions
                SET status='active', second_approver=?, approved_at=?, expires_at=?
                WHERE session_id=? AND status='pending_second_approval' AND revoked_at IS NULL
                """,
                (approver, now, now + int(record["ttl_seconds"]), session_id),
            )
            if cur.rowcount != 1:
                raise PermissionError("break-glass session is not waiting for a second approval")
        session = self.get(session_id)
        self._audit("break_glass.approve", session=session, second_approver=approver, metadata=metadata or {})
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
                "SELECT session_id, actor, reason, approved_by, expires_at, revoked_at, status, second_approver FROM break_glass_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"break-glass session not found: {session_id}")
        fields = ("session_id", "actor", "reason", "approved_by", "expires_at", "revoked_at", "status", "second_approver")
        return session_from_record(dict(zip(fields, row)))

    def assert_active(self, session_id: str, *, actor: str) -> BreakGlassSession:
        return require_usable(self.get(session_id), actor)

    def _insert(self, sql: str, params: tuple[Any, ...]) -> None:
        try:
            with connect_sqlite(self.db_path) as con:
                con.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise ValueError("break-glass session already exists") from exc

    def _audit(self, event: str, **fields: Any) -> None:
        record = {"event": event, "ts": time.time(), **fields}
        with self.audit_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=lambda o: o.__dict__) + "\n")
