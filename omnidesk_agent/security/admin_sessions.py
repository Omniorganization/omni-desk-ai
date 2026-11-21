"""Short-lived, revocable admin sessions opened with a long-lived role token.

Web Admin kept the raw gateway bearer token in its cookie: the cookie's seal
gave integrity, not confidentiality, and logout only cleared the cookie, so a
copied cookie kept working against the gateway. ``POST /admin/session/login``
now exchanges a role token for an opaque session token that expires and can be
revoked; Web Admin keeps only that.

- A session token is ``omds_`` followed by 256 random bits. The store keeps
  ``lookup = HMAC(lookup key, token)``, never the token, so neither a copy of
  the table nor a log line can be turned back into a credential.
- Each row carries a seal, an HMAC over the session id, lookup, actor, role,
  organization and expiry. Both keys derive from the audit key in the secret
  store, with their own labels. A row inserted or edited by anyone without the
  key (a forged owner session, a raised role, a later expiry) does not
  authenticate.
- Known limit: revocation is a column, not sealed history. Someone who can
  write the table and kept a copy of a row from before its revocation can
  restore it until its original expiry, at most 12 hours.
- Logins are limited per actor: at most ``MAX_LOGINS_PER_MINUTE`` and
  ``MAX_ACTIVE_SESSIONS_PER_ACTOR`` live sessions. The limits are counted in
  the shared store, so they hold across instances, give or take a concurrent
  login.

The SQLite store lives next to the audit log, outside the agent's workspace.
The PostgreSQL store uses the repository factory's shared pool, so every
gateway instance sees the same sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from omnidesk_agent.security.audit_chain import DEFAULT_KEY_ENV, dev_key_file_for, load_audit_key
from omnidesk_agent.storage.sqlite import connect_sqlite

SESSION_TOKEN_PREFIX = "omds_"
SESSION_ID_PREFIX = "ses_"
DEFAULT_TTL_SECONDS = 8 * 3600
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 12 * 3600
MAX_LOGINS_PER_MINUTE = 10
MAX_ACTIVE_SESSIONS_PER_ACTOR = 20
ROLES = frozenset({"viewer", "operator", "owner"})

_LOOKUP_LABEL = b"omnidesk/admin-session/lookup/v1"
_SEAL_LABEL = b"omnidesk/admin-session/seal/v1"
# SQL is written out literally (no string building): nothing user-supplied is
# ever part of a statement, and bandit's B608 stays meaningful.
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS omnidesk_admin_sessions (
        session_id TEXT PRIMARY KEY,
        lookup TEXT NOT NULL UNIQUE,
        actor TEXT NOT NULL,
        role TEXT NOT NULL,
        organization TEXT NOT NULL,
        label TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        expires_at BIGINT NOT NULL,
        revoked_at DOUBLE PRECISION,
        seal TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS omnidesk_admin_sessions_actor ON omnidesk_admin_sessions (actor, created_at)",
)


class AdminSessionLimit(Exception):
    """Too many logins or live sessions for one actor; retry later or revoke some."""

    def __init__(self, message: str, retry_after_seconds: int):
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))


@dataclass(frozen=True)
class AdminSession:
    session_id: str
    actor: str
    role: str
    organization: str
    created_at: float
    expires_at: int
    label: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "actor": self.actor,
            "role": self.role,
            "organization": self.organization,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "label": self.label,
        }


class AdminSessionStore:
    """Backend-independent session logic; subclasses provide ``_run``."""

    backend = "base"

    def __init__(
        self,
        *,
        key_env: str = DEFAULT_KEY_ENV,
        dev_key_file: Optional[Path] = None,
        raw_key: Optional[bytes] = None,
        clock: Callable[[], float] = time.time,
    ):
        raw = raw_key if raw_key is not None else load_audit_key(key_env, dev_key_file=dev_key_file)
        self._lookup_key = hmac.new(raw, _LOOKUP_LABEL, hashlib.sha256).digest()
        self._seal_key = hmac.new(raw, _SEAL_LABEL, hashlib.sha256).digest()
        self.clock = clock
        for statement in _SCHEMA:
            self._run(statement)

    # -- backend -------------------------------------------------------------------

    def _run(self, sql: str, params: tuple = (), *, fetch: bool = False) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- keys ----------------------------------------------------------------------

    def _lookup(self, token: str) -> str:
        return hmac.new(self._lookup_key, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _seal(self, session_id: str, lookup: str, actor: str, role: str, organization: str, expires_at: int) -> str:
        canonical = json.dumps([session_id, lookup, actor, role, organization, int(expires_at)], separators=(",", ":"))
        return hmac.new(self._seal_key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

    def _session(self, row: tuple) -> Optional[AdminSession]:
        session_id, lookup, actor, role, organization, label, created_at, expires_at, revoked_at, seal = row
        expected = self._seal(str(session_id), str(lookup), str(actor), str(role), str(organization), int(expires_at))
        if not hmac.compare_digest(expected, str(seal)) or str(role) not in ROLES:
            return None
        if revoked_at is not None or int(expires_at) <= self.clock():
            return None
        return AdminSession(str(session_id), str(actor), str(role), str(organization), float(created_at), int(expires_at), str(label))

    # -- operations ----------------------------------------------------------------

    def create(self, *, actor: str, role: str, organization: str, ttl_seconds: Optional[int] = None, label: str = "") -> tuple[str, AdminSession]:
        """Open a session; returns the token (shown once) and the session."""
        actor, role, organization = str(actor or "").strip(), str(role or ""), str(organization or "org_default")
        if not actor:
            raise ValueError("an admin session needs an actor")
        if role not in ROLES:
            raise ValueError(f"unknown admin role: {role!r}")
        ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else max(MIN_TTL_SECONDS, min(int(ttl_seconds), MAX_TTL_SECONDS))
        now = self.clock()
        self._run("DELETE FROM omnidesk_admin_sessions WHERE expires_at <= %s", (int(now),))
        recent = self._run("SELECT created_at FROM omnidesk_admin_sessions WHERE actor = %s AND created_at > %s", (actor, now - 60), fetch=True)
        if len(recent) >= MAX_LOGINS_PER_MINUTE:
            oldest = min(float(r[0]) for r in recent)
            raise AdminSessionLimit("too many admin logins for this actor; try again shortly", math.ceil(oldest + 60 - now))
        live = self._run("SELECT expires_at FROM omnidesk_admin_sessions WHERE actor = %s AND revoked_at IS NULL AND expires_at > %s", (actor, int(now)), fetch=True)
        if len(live) >= MAX_ACTIVE_SESSIONS_PER_ACTOR:
            first_expiry = min(int(r[0]) for r in live)
            raise AdminSessionLimit("too many live admin sessions for this actor; revoke some", min(3600, math.ceil(first_expiry - now)))
        token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
        session = AdminSession(SESSION_ID_PREFIX + secrets.token_hex(12), actor, role, organization, now, int(now + ttl), str(label or "")[:64])
        lookup = self._lookup(token)
        seal = self._seal(session.session_id, lookup, actor, role, organization, session.expires_at)
        self._run(
            "INSERT INTO omnidesk_admin_sessions (session_id, lookup, actor, role, organization, label, created_at, expires_at, revoked_at, seal) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)",
            (session.session_id, lookup, actor, role, organization, session.label, now, session.expires_at, seal),
        )
        return token, session

    def resolve(self, token: str) -> Optional[AdminSession]:
        """The live session for ``token``; None when unknown, expired, revoked or tampered with."""
        token = str(token or "")
        if not token.startswith(SESSION_TOKEN_PREFIX):
            return None
        rows = self._run("SELECT session_id, lookup, actor, role, organization, label, created_at, expires_at, revoked_at, seal FROM omnidesk_admin_sessions WHERE lookup = %s", (self._lookup(token),), fetch=True)
        return self._session(rows[0]) if rows else None

    def get(self, session_id: str) -> Optional[AdminSession]:
        rows = self._run("SELECT session_id, lookup, actor, role, organization, label, created_at, expires_at, revoked_at, seal FROM omnidesk_admin_sessions WHERE session_id = %s", (str(session_id),), fetch=True)
        return self._session(rows[0]) if rows else None

    def revoke(self, session_id: str) -> bool:
        changed = self._run("UPDATE omnidesk_admin_sessions SET revoked_at = %s WHERE session_id = %s AND revoked_at IS NULL", (self.clock(), str(session_id)))
        return bool(changed)

    def revoke_actor(self, actor: str) -> list[str]:
        """Revoke every live session of ``actor``; returns their ids."""
        now = self.clock()
        rows = self._run("SELECT session_id FROM omnidesk_admin_sessions WHERE actor = %s AND revoked_at IS NULL AND expires_at > %s", (str(actor), int(now)), fetch=True)
        revoked = [str(r[0]) for r in rows]
        if revoked:
            self._run("UPDATE omnidesk_admin_sessions SET revoked_at = %s WHERE actor = %s AND revoked_at IS NULL", (now, str(actor)))
        return revoked


class SQLiteAdminSessionStore(AdminSessionStore):
    backend = "sqlite"

    def __init__(self, path: Path, **kwargs: Any):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        kwargs.setdefault("dev_key_file", dev_key_file_for(self.path))
        super().__init__(**kwargs)

    def _run(self, sql: str, params: tuple = (), *, fetch: bool = False) -> Any:
        with connect_sqlite(self.path) as con:
            cursor = con.execute(sql.replace("%s", "?"), params)
            return cursor.fetchall() if fetch else cursor.rowcount


class PostgresAdminSessionStore(AdminSessionStore):
    """Sessions in the shared PostgreSQL database, on the repository factory's pool."""

    backend = "postgres"

    def __init__(self, pool: Any, **kwargs: Any):
        self._pool = pool
        super().__init__(**kwargs)

    def _run(self, sql: str, params: tuple = (), *, fetch: bool = False) -> Any:
        with self._pool.connection() as con:
            with con.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchall() if fetch else cursor.rowcount
