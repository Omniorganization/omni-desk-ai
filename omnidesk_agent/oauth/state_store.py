from __future__ import annotations

import secrets
import time
from pathlib import Path

from omnidesk_agent.storage.sqlite import connect_sqlite
from omnidesk_agent.storage.migrations import Migration, apply_migrations


class OAuthStateStore:
    def __init__(self, db_path: Path, ttl_seconds: int = 600):
        self.db_path = db_path.expanduser()
        self.ttl_seconds = ttl_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    redirect_uri TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    used_at REAL
                )
                """
            )
            apply_migrations(con, [
                Migration(1, "oauth_state_schema_baseline", lambda _con: None),
                Migration(2, "oauth_state_actor_binding", _add_actor_column),
            ])

    def create(self, redirect_uri: str, *, actor: str | None = None) -> str:
        state = secrets.token_urlsafe(32)
        actor_key = _actor_key(actor)
        with connect_sqlite(self.db_path) as con:
            con.execute(
                "INSERT INTO oauth_states (state, redirect_uri, actor, created_at) VALUES (?, ?, ?, ?)",
                (state, redirect_uri, actor_key, time.time()),
            )
        return state

    def verify_and_use(self, state: str, redirect_uri: str, *, actor: str | None = None) -> bool:
        cutoff = time.time() - self.ttl_seconds
        actor_key = _actor_key(actor) if actor is not None else None
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT redirect_uri, actor, created_at, used_at FROM oauth_states WHERE state = ?", (state,)).fetchone()
            if not row:
                return False
            saved_redirect_uri, saved_actor, created_at, used_at = row
            if used_at is not None or created_at < cutoff or saved_redirect_uri != redirect_uri:
                return False
            if actor_key is not None and str(saved_actor or "") != actor_key:
                return False
            con.execute("UPDATE oauth_states SET used_at = ? WHERE state = ?", (time.time(), state))
        return True


def _add_actor_column(con) -> None:
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(oauth_states)").fetchall()}
    if "actor" not in columns:
        con.execute("ALTER TABLE oauth_states ADD COLUMN actor TEXT NOT NULL DEFAULT ''")


def _actor_key(actor: str | None) -> str:
    return str(actor or "unknown").strip()[:256] or "unknown"
