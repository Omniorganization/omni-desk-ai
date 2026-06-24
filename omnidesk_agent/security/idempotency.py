from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.storage.sqlite import connect_sqlite


class SideEffectIdempotencyConflict(ValueError):
    pass


class SideEffectIdempotencyInProgress(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def hash_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_idempotency_response(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return {"ok": True, "result_repr": repr(value)}
    return {"ok": True, "result": value}


def side_effect_scope_key(*, route: str, actor: str, key: str, resource_id: Optional[str] = None) -> str:
    scoped = {
        "route": str(route),
        "actor": str(actor),
        "resource_id": str(resource_id or ""),
        "key": str(key),
    }
    return hashlib.sha256(canonical_json(scoped).encode("utf-8")).hexdigest()


class SQLiteSideEffectIdempotencyStore:
    def __init__(self, db_path: Path, *, ttl_seconds: int = 1800):
        self.db_path = Path(db_path).expanduser()
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS side_effect_idempotency (
                  scope_key TEXT PRIMARY KEY,
                  route TEXT NOT NULL,
                  actor TEXT NOT NULL,
                  resource_id TEXT,
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
            con.execute("CREATE INDEX IF NOT EXISTS idx_side_effect_idem_expires_at ON side_effect_idempotency(expires_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_side_effect_idem_route_actor ON side_effect_idempotency(route, actor)")

    def begin(self, *, route: str, actor: str, key: str, payload: Any, resource_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        now = time.time()
        scope = side_effect_scope_key(route=route, actor=actor, resource_id=resource_id, key=key)
        payload_hash = hash_payload(payload)
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM side_effect_idempotency WHERE expires_at <= ?", (now,))
            row = con.execute(
                "SELECT payload_hash, status, response_json FROM side_effect_idempotency WHERE scope_key=?",
                (scope,),
            ).fetchone()
            if row:
                if str(row[0]) != payload_hash:
                    raise SideEffectIdempotencyConflict("idempotency key was reused with a different side-effect payload")
                if str(row[1]) == "completed" and row[2]:
                    return json.loads(str(row[2]))
                raise SideEffectIdempotencyInProgress("idempotent side-effect request is already in progress")
            con.execute(
                """
                INSERT INTO side_effect_idempotency(scope_key, route, actor, resource_id, request_key, payload_hash, status, response_json, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, 'running', NULL, ?, ?, ?)
                """,
                (scope, route, actor, resource_id or "", key, payload_hash, now, now, now + self.ttl_seconds),
            )
        return None

    def complete(self, *, route: str, actor: str, key: str, response: Any, resource_id: Optional[str] = None) -> None:
        now = time.time()
        scope = side_effect_scope_key(route=route, actor=actor, resource_id=resource_id, key=key)
        normalized = normalize_idempotency_response(response)
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                UPDATE side_effect_idempotency
                SET status='completed', response_json=?, updated_at=?, expires_at=?
                WHERE scope_key=? AND status='running'
                """,
                (json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str), now, now + self.ttl_seconds, scope),
            )

    def fail(self, *, route: str, actor: str, key: str, resource_id: Optional[str] = None) -> None:
        scope = side_effect_scope_key(route=route, actor=actor, resource_id=resource_id, key=key)
        with connect_sqlite(self.db_path) as con:
            con.execute("DELETE FROM side_effect_idempotency WHERE scope_key=? AND status='running'", (scope,))

    def stats(self) -> dict[str, Any]:
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            con.execute("DELETE FROM side_effect_idempotency WHERE expires_at <= ?", (now,))
            rows = con.execute("SELECT status, COUNT(*) FROM side_effect_idempotency GROUP BY status").fetchall()
        by_status = {str(status): int(count) for status, count in rows}
        return {"backend": "sqlite", "running": by_status.get("running", 0), "completed": by_status.get("completed", 0)}

    def close(self) -> None:
        return None


class JsonStateSideEffectIdempotencyStore:
    namespace = "side_effect_idempotency"

    def __init__(self, state: Any, *, ttl_seconds: int = 1800):
        self.state = state
        self.ttl_seconds = max(60, int(ttl_seconds))

    def begin(self, *, route: str, actor: str, key: str, payload: Any, resource_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        now = time.time()
        scope = side_effect_scope_key(route=route, actor=actor, resource_id=resource_id, key=key)
        payload_hash = hash_payload(payload)
        self._cleanup(now)
        row = {
            "scope_key": scope,
            "route": route,
            "actor": actor,
            "resource_id": resource_id or "",
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
            raise SideEffectIdempotencyConflict("idempotency key was reused with a different side-effect payload")
        if existing.get("status") == "completed" and isinstance(existing.get("response"), dict):
            return dict(existing["response"])
        raise SideEffectIdempotencyInProgress("idempotent side-effect request is already in progress")

    def complete(self, *, route: str, actor: str, key: str, response: Any, resource_id: Optional[str] = None) -> None:
        now = time.time()
        scope = side_effect_scope_key(route=route, actor=actor, resource_id=resource_id, key=key)
        normalized = normalize_idempotency_response(response)
        try:
            self.state.update_locked(
                self.namespace,
                scope,
                lambda row: {**row, "status": "completed", "response": normalized, "updated_at": now, "expires_at": now + self.ttl_seconds},
            )
        except KeyError:
            return

    def fail(self, *, route: str, actor: str, key: str, resource_id: Optional[str] = None) -> None:
        scope = side_effect_scope_key(route=route, actor=actor, resource_id=resource_id, key=key)
        self.state.delete(self.namespace, scope)

    def _cleanup(self, now: float) -> None:
        for row in self.state.list(self.namespace, limit=250000):
            if float(row.get("expires_at") or 0.0) <= now:
                self.state.delete(self.namespace, str(row.get("scope_key") or ""))

    def stats(self) -> dict[str, Any]:
        now = time.time()
        self._cleanup(now)
        by_status: dict[str, int] = {}
        for row in self.state.list(self.namespace, limit=250000):
            status = str(row.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        return {"backend": "json_state", "running": by_status.get("running", 0), "completed": by_status.get("completed", 0)}

    def close(self) -> None:
        return None
