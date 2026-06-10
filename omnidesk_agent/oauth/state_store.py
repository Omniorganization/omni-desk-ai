from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path


class OAuthStateStore:
    def __init__(self, db_path: Path, ttl_seconds: int = 600):
        self.db_path = db_path.expanduser()
        self.ttl_seconds = ttl_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    redirect_uri TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    used_at REAL
                )
                """
            )

    def create(self, redirect_uri: str) -> str:
        state = secrets.token_urlsafe(32)
        with sqlite3.connect(self.db_path) as con:
            con.execute("INSERT INTO oauth_states (state, redirect_uri, created_at) VALUES (?, ?, ?)", (state, redirect_uri, time.time()))
        return state

    def verify_and_use(self, state: str, redirect_uri: str) -> bool:
        cutoff = time.time() - self.ttl_seconds
        with sqlite3.connect(self.db_path) as con:
            row = con.execute("SELECT redirect_uri, created_at, used_at FROM oauth_states WHERE state = ?", (state,)).fetchone()
            if not row:
                return False
            saved_redirect_uri, created_at, used_at = row
            if used_at is not None or created_at < cutoff or saved_redirect_uri != redirect_uri:
                return False
            con.execute("UPDATE oauth_states SET used_at = ? WHERE state = ?", (time.time(), state))
        return True
