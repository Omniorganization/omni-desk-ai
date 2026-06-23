from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.storage.sqlite import connect_sqlite


class AgentRunIdempotencyConflict(ValueError):
    pass


class AgentRunIdempotencyInProgress(ValueError):
    pass


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash_payload(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _scope_key(*, actor: str, source_device: Optional[str], key: str) -> str:
    scoped = {"route": "/agent/run", "actor": actor, "source_device": source_device or "", "key": key}
    return hashlib.sha256(_canonical_json(scoped).encode("utf-8")).hexdigest()


class SQLiteAgentRunIdempotencyStore:
    def __init__(self, db_path: Path, *, ttl_seconds: int = 1800):
        self.db_path = Path(db_path).expanduser()
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_run_idempotency (
                  scope_key TEXT PRIMARY KEY,
                  actor TEXT NOT NULL,
                  source_device TEXT,
                  request_key TEXT NOT NULL,
                  payload_hash TEXT NOT NULL,
                  status TEXT NOT NULL,
                  response_json TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  expires_at REAL NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_agent_run_idem_expires_at ON agent_run_idempotency(expires_at)")

    def begin(self, *, actor: str, key: str, source_device: Optional[str], payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        now = time.time()
        scope = _scope_key(actor=actor, source_device=source_device, key=key)
        payload_hash = _hash_payload(payload)
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM agent_run_idempotency WHERE expires_at <= ?", (now,))
            row = con.execute(
                "SELECT payload_hash, status, response_json FROM agent_run_idempotency WHERE scope_key=?",
                (scope,),
            ).fetchone()
            if row:
                if str(row[0]) != payload_hash:
                    raise AgentRunIdempotencyConflict("idempotency key was reused with a different /agent/run payload")
                if str(row[1]) == "completed" and row[2]:
                    return json.loads(str(row[2]))
                raise AgentRunIdempotencyInProgress("idempotent /agent/run request is already in progress")
            con.execute(
                """
                INSERT INTO agent_run_idempotency(scope_key, actor, source_device, request_key, payload_hash, status, response_json, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, 'running', NULL, ?, ?, ?)
                """,
                (scope, actor, source_device or "", key, payload_hash, now, now, now + self.ttl_seconds),
            )
        return None

    def complete(self, *, actor: str, key: str, source_device: Optional[str], response: dict[str, Any]) -> None:
        now = time.time()
        scope = _scope_key(actor=actor, source_device=source_device, key=key)
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                UPDATE agent_run_idempotency
                SET status='completed', response_json=?, updated_at=?, expires_at=?
                WHERE scope_key=? AND status='running'
                """,
                (json.dumps(response, ensure_ascii=False, sort_keys=True, default=str), now, now + self.ttl_seconds, scope),
            )

    def fail(self, *, actor: str, key: str, source_device: Optional[str]) -> None:
        scope = _scope_key(actor=actor, source_device=source_device, key=key)
        with connect_sqlite(self.db_path) as con:
            con.execute("DELETE FROM agent_run_idempotency WHERE scope_key=? AND status='running'", (scope,))


class JsonStateAgentRunIdempotencyStore:
    namespace = "agent_run_idempotency"

    def __init__(self, state: Any, *, ttl_seconds: int = 1800):
        self.state = state
        self.ttl_seconds = max(60, int(ttl_seconds))

    def begin(self, *, actor: str, key: str, source_device: Optional[str], payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        now = time.time()
        scope = _scope_key(actor=actor, source_device=source_device, key=key)
        payload_hash = _hash_payload(payload)
        self._cleanup(now)
        row = {
            "scope_key": scope,
            "actor": actor,
            "source_device": source_device or "",
            "request_key": key,
            "payload_hash": payload_hash,
            "status": "running",
            "response": None,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + self.ttl_seconds,
        }
        if self.state.insert_once(self.namespace, scope, row):
            return None
        existing = self.state.get(self.namespace, scope)
        if not existing or float(existing.get("expires_at") or 0.0) <= now:
            self.state.put(self.namespace, scope, row)
            return None
        if str(existing.get("payload_hash") or "") != payload_hash:
            raise AgentRunIdempotencyConflict("idempotency key was reused with a different /agent/run payload")
        if existing.get("status") == "completed" and isinstance(existing.get("response"), dict):
            return dict(existing["response"])
        raise AgentRunIdempotencyInProgress("idempotent /agent/run request is already in progress")

    def complete(self, *, actor: str, key: str, source_device: Optional[str], response: dict[str, Any]) -> None:
        now = time.time()
        scope = _scope_key(actor=actor, source_device=source_device, key=key)
        try:
            self.state.update_locked(
                self.namespace,
                scope,
                lambda row: {**row, "status": "completed", "response": dict(response), "updated_at": now, "expires_at": now + self.ttl_seconds},
            )
        except KeyError:
            return

    def fail(self, *, actor: str, key: str, source_device: Optional[str]) -> None:
        scope = _scope_key(actor=actor, source_device=source_device, key=key)
        self.state.delete(self.namespace, scope)

    def _cleanup(self, now: float) -> None:
        for row in self.state.list(self.namespace, limit=1000):
            if float(row.get("expires_at") or 0.0) <= now:
                self.state.delete(self.namespace, str(row.get("scope_key") or ""))
