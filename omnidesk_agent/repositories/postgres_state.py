from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Union

from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.core.token_budget import TokenBudgetConfig, TokenBudgetManager
from omnidesk_agent.security.dual_approval import DualApprovalDecision
from omnidesk_agent.security.break_glass import BreakGlassSession
from omnidesk_agent.security.webhook_security import WebhookSecurityConfig
from omnidesk_agent.repositories.postgres import PostgresUnavailable

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS omnidesk_core_state (
  namespace TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json JSONB NOT NULL,
  created_at DOUBLE PRECISION NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY(namespace, key)
);
CREATE INDEX IF NOT EXISTS idx_omnidesk_core_state_namespace_updated
  ON omnidesk_core_state(namespace, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_omnidesk_core_state_namespace_status
  ON omnidesk_core_state(namespace, ((value_json->>'status')), updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_omnidesk_jobs_dedupe
  ON omnidesk_core_state ((value_json->>'dedupe_key')) WHERE namespace='jobs';
CREATE UNIQUE INDEX IF NOT EXISTS uniq_omnidesk_outbound_idempotency
  ON omnidesk_core_state ((value_json->>'idempotency_key')) WHERE namespace='outbound_messages';
CREATE TABLE IF NOT EXISTS omnidesk_model_cost_events (
  id TEXT PRIMARY KEY,
  task_id TEXT,
  run_id TEXT,
  actor TEXT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  profile TEXT NOT NULL,
  task TEXT,
  input_tokens BIGINT NOT NULL DEFAULT 0,
  output_tokens BIGINT NOT NULL DEFAULT 0,
  estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_omnidesk_model_cost_created
  ON omnidesk_model_cost_events(created_at);
CREATE INDEX IF NOT EXISTS idx_omnidesk_model_cost_provider
  ON omnidesk_model_cost_events(provider, model, created_at);
CREATE INDEX IF NOT EXISTS idx_omnidesk_model_cost_actor
  ON omnidesk_model_cost_events(actor, created_at);
CREATE INDEX IF NOT EXISTS idx_omnidesk_model_cost_task
  ON omnidesk_model_cost_events(task_id, created_at);
"""


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        return json.loads(value)
    return json.loads(json.dumps(value, default=str))


class _PostgresJsonState:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._ensure_schema()

    def _connect(self):  # type: ignore[no-untyped-def]
        try:
            import psycopg  # type: ignore
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            raise PostgresUnavailable("Install psycopg[binary] to use postgres runtime state stores") from exc
        return psycopg.connect(self.dsn)

    def _ensure_schema(self) -> None:
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(STATE_SCHEMA)

    def put(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        now = time.time()
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omnidesk_core_state(namespace, key, value_json, created_at, updated_at)
                    VALUES (%s, %s, %s::jsonb, %s, %s)
                    ON CONFLICT(namespace, key) DO UPDATE
                    SET value_json=EXCLUDED.value_json, updated_at=EXCLUDED.updated_at
                    """,
                    (namespace, key, json.dumps(value, ensure_ascii=False, default=str), now, now),
                )

    def insert_once(self, namespace: str, key: str, value: dict[str, Any]) -> bool:
        now = time.time()
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omnidesk_core_state(namespace, key, value_json, created_at, updated_at)
                    VALUES (%s, %s, %s::jsonb, %s, %s)
                    ON CONFLICT(namespace, key) DO NOTHING
                    """,
                    (namespace, key, json.dumps(value, ensure_ascii=False, default=str), now, now),
                )
                return int(cur.rowcount or 0) == 1

    def get(self, namespace: str, key: str) -> Optional[dict[str, Any]]:
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute("SELECT value_json FROM omnidesk_core_state WHERE namespace=%s AND key=%s", (namespace, key))
                row = cur.fetchone()
        return _loads(row[0]) if row else None

    def delete(self, namespace: str, key: str) -> bool:
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute("DELETE FROM omnidesk_core_state WHERE namespace=%s AND key=%s", (namespace, key))
                return int(cur.rowcount or 0) > 0

    def list(self, namespace: str, *, status: Optional[str] = None, limit: int = 50, ascending: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        order = "ASC" if ascending else "DESC"
        with self._connect() as con:
            with con.cursor() as cur:
                if status:
                    cur.execute(
                        f"""
                        SELECT value_json FROM omnidesk_core_state
                        WHERE namespace=%s AND value_json->>'status'=%s
                        ORDER BY created_at {order} LIMIT %s
                        """,  # nosec B608 - order is constrained above
                        (namespace, status, limit),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT value_json FROM omnidesk_core_state
                        WHERE namespace=%s ORDER BY created_at {order} LIMIT %s
                        """,  # nosec B608 - order is constrained above
                        (namespace, limit),
                    )
                return [_loads(row[0]) for row in cur.fetchall()]

    def stats_by_status(self, namespace: str) -> dict[str, int]:
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    SELECT value_json->>'status', COUNT(*) FROM omnidesk_core_state
                    WHERE namespace=%s GROUP BY value_json->>'status'
                    """,
                    (namespace,),
                )
                return {str(status): int(count) for status, count in cur.fetchall() if status is not None}

    def find_by_field(self, namespace: str, field: str, value: str) -> Optional[dict[str, Any]]:
        allowed_fields = {"id", "dedupe_key", "idempotency_key", "status", "waiting_approval_id"}
        if field not in allowed_fields:
            raise ValueError(f"unsupported JSON field lookup: {field}")
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT value_json FROM omnidesk_core_state WHERE namespace=%s AND value_json->>%s=%s LIMIT 1",
                    (namespace, field, value),
                )
                row = cur.fetchone()
        return _loads(row[0]) if row else None

    def update_locked_by_field(self, namespace: str, field: str, value: str, updater) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        allowed_fields = {"id", "dedupe_key", "idempotency_key", "status", "waiting_approval_id"}
        if field not in allowed_fields:
            raise ValueError(f"unsupported JSON field update: {field}")
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT key, value_json FROM omnidesk_core_state WHERE namespace=%s AND value_json->>%s=%s FOR UPDATE",
                    (namespace, field, value),
                )
                row = cur.fetchone()
                if not row:
                    raise KeyError(value)
                key, raw = row
                updated = updater(_loads(raw))
                cur.execute(
                    "UPDATE omnidesk_core_state SET value_json=%s::jsonb, updated_at=%s WHERE namespace=%s AND key=%s",
                    (json.dumps(updated, ensure_ascii=False, default=str), time.time(), namespace, key),
                )
                return updated

    def update_locked(self, namespace: str, key: str, updater) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT value_json FROM omnidesk_core_state WHERE namespace=%s AND key=%s FOR UPDATE",
                    (namespace, key),
                )
                row = cur.fetchone()
                if not row:
                    raise KeyError(key)
                current = _loads(row[0])
                updated = updater(dict(current))
                cur.execute(
                    """
                    UPDATE omnidesk_core_state SET value_json=%s::jsonb, updated_at=%s
                    WHERE namespace=%s AND key=%s
                    """,
                    (json.dumps(updated, ensure_ascii=False, default=str), time.time(), namespace, key),
                )
                return updated

    def claim_one(self, namespace: str, predicate, updater) -> Optional[dict[str, Any]]:  # type: ignore[no-untyped-def]
        with self._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    SELECT key, value_json FROM omnidesk_core_state
                    WHERE namespace=%s
                    ORDER BY created_at ASC
                    FOR UPDATE SKIP LOCKED
                    """,
                    (namespace,),
                )
                for key, raw in cur.fetchall():
                    current = _loads(raw)
                    if not predicate(current):
                        continue
                    updated = updater(dict(current))
                    cur.execute(
                        """
                        UPDATE omnidesk_core_state SET value_json=%s::jsonb, updated_at=%s
                        WHERE namespace=%s AND key=%s
                        """,
                        (json.dumps(updated, ensure_ascii=False, default=str), time.time(), namespace, key),
                    )
                    return current
        return None


class PostgresDualApprovalStore:
    namespace = "dual_approvals"

    def __init__(self, state: _PostgresJsonState):
        self.state = state

    def open(self, approval_id: str, proposal: dict[str, Any]) -> None:
        now = time.time()
        self.state.insert_once(
            self.namespace,
            approval_id,
            {
                "approval_id": approval_id,
                "proposal": proposal,
                "first_approver": None,
                "second_approver": None,
                "created_at": now,
                "updated_at": now,
            },
        )

    def approve(self, approval_id: str, approver: str) -> DualApprovalDecision:
        approver = str(approver).strip()
        if not approver:
            raise ValueError("approver is required")

        def update(row: dict[str, Any]) -> dict[str, Any]:
            proposer = str((row.get("proposal") or {}).get("created_by") or (row.get("proposal") or {}).get("proposer") or "")
            if proposer and hmac.compare_digest(proposer, approver):
                raise PermissionError("proposal creator cannot approve their own critical proposal")
            first = row.get("first_approver")
            second = row.get("second_approver")
            if first and hmac.compare_digest(str(first), approver):
                raise PermissionError("second approver must be distinct from first approver")
            if not first:
                row["first_approver"] = approver
            elif not second:
                row["second_approver"] = approver
            row["updated_at"] = time.time()
            return row

        self.state.update_locked(self.namespace, approval_id, update)
        return self.status(approval_id)

    def status(self, approval_id: str) -> DualApprovalDecision:
        row = self.state.get(self.namespace, approval_id)
        if not row:
            raise KeyError(f"dual approval not found: {approval_id}")
        first = row.get("first_approver")
        second = row.get("second_approver")
        return DualApprovalDecision(
            bool(first and second), approval_id, first, second, "ready" if first and second else "waiting_for_second_approver"
        )


class PostgresApprovalStore:
    namespace = "approvals"

    def __init__(self, state: _PostgresJsonState, ttl_seconds: int = 600, dual_approval_store: Any | None = None):
        self.state = state
        self.ttl_seconds = ttl_seconds
        self.dual_approval_store = dual_approval_store

    def attach_dual_approval_store(self, dual_approval_store: Any | None) -> None:
        self.dual_approval_store = dual_approval_store

    def create(self, proposal: dict[str, Any], ttl_seconds: Optional[int] = None) -> str:
        aid = str(uuid.uuid4())
        now = time.time()
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        row = {
            "id": aid,
            "status": "pending",
            "proposal": proposal,
            "result": None,
            "created_at": now,
            "expires_at": now + ttl if ttl and ttl > 0 else None,
            "decided_at": None,
            "consumed_at": None,
            "consumed_by_run_id": None,
            "consumed_scope_hash": None,
        }
        self.state.put(self.namespace, aid, row)
        if proposal.get("requires_dual_approval"):
            if self.dual_approval_store is None:
                raise PermissionError("dual approval store is required for critical approval proposals")
            self.dual_approval_store.open(aid, proposal)
        return aid

    def get(self, approval_id: str) -> Optional[dict[str, Any]]:
        return self.state.get(self.namespace, approval_id)

    def list(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        return self.state.list(self.namespace, status=status, limit=1000)

    def decide(self, approval_id: str, status: str, result: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if status not in {"approved", "denied"}:
            raise ValueError("status must be approved or denied")

        def update(row: dict[str, Any]) -> dict[str, Any]:
            if row.get("expires_at") and time.time() > float(row["expires_at"]):
                raise PermissionError(f"approval expired: {approval_id}")
            if row.get("status") != "pending":
                raise PermissionError(f"approval already decided: {approval_id}")
            if status == "approved":
                self._require_dual_approval_ready(approval_id, row.get("proposal") or {})
            row["status"] = status
            row["result"] = result or {}
            row["decided_at"] = time.time()
            return row

        return self.state.update_locked(self.namespace, approval_id, update)

    def require_approved(self, approval_id: str, expected_proposal: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        approval = self.get(approval_id)
        if not approval:
            raise PermissionError(f"approval not found: {approval_id}")
        if approval.get("expires_at") and time.time() > float(approval["expires_at"]):
            raise PermissionError(f"approval expired: {approval_id}")
        if approval.get("status") != "approved":
            raise PermissionError(f"approval is not approved: {approval_id}")
        self._require_dual_approval_ready(approval_id, approval.get("proposal") or {})
        self._validate_expected_proposal(approval, expected_proposal)
        return approval

    def consume_approved(
        self, approval_id: str, expected_proposal: Optional[dict[str, Any]] = None, *, consumed_by_run_id: Optional[str] = None
    ) -> dict[str, Any]:
        def update(row: dict[str, Any]) -> dict[str, Any]:
            if row.get("expires_at") and time.time() > float(row["expires_at"]):
                raise PermissionError(f"approval expired: {approval_id}")
            if row.get("status") != "approved":
                if row.get("status") == "consumed":
                    raise PermissionError(f"approval already consumed: {approval_id}")
                raise PermissionError(f"approval is not approved: {approval_id}")
            self._require_dual_approval_ready(approval_id, row.get("proposal") or {})
            self._validate_expected_proposal(row, expected_proposal)
            proposal = expected_proposal or row.get("proposal") or {}
            row["status"] = "consumed"
            row["consumed_at"] = time.time()
            row["consumed_by_run_id"] = consumed_by_run_id
            row["consumed_scope_hash"] = str(proposal.get("scope_hash") or "") or None
            return row

        return self.state.update_locked(self.namespace, approval_id, update)

    def _require_dual_approval_ready(self, approval_id: str, proposal: dict[str, Any]) -> None:
        if not proposal.get("requires_dual_approval"):
            return
        if self.dual_approval_store is None:
            raise PermissionError("dual approval store is required for critical approval proposals")
        decision = self.dual_approval_store.status(approval_id)
        if not decision.ready:
            raise PermissionError(f"dual approval is not satisfied: {decision.reason}")

    @staticmethod
    def _validate_expected_proposal(approval: dict[str, Any], expected_proposal: Optional[dict[str, Any]]) -> None:
        if not expected_proposal:
            return
        proposal = approval.get("proposal") or {}
        for key in ("tool", "action", "source", "actor", "run_id", "plan_id", "step_index", "scope_hash"):
            if expected_proposal.get(key) is not None and proposal.get(key) != expected_proposal.get(key):
                raise PermissionError(f"approval proposal mismatch on {key}")


class PostgresRunStore:
    namespace = "runs"
    UPDATE_FIELDS = {
        "approval_proposal_json",
        "current_step_index",
        "plan_json",
        "results_json",
        "resume_token",
        "status",
        "updated_at",
        "waiting_approval_id",
    }

    def __init__(self, state: _PostgresJsonState):
        self.state = state

    def create(self, original_message: dict[str, Any]) -> str:
        run_id = str(uuid.uuid4())
        now = time.time()
        self.state.put(
            self.namespace,
            run_id,
            {
                "id": run_id,
                "status": "planned",
                "original_message": original_message,
                "plan_json": None,
                "current_step_index": 0,
                "results": [],
                "waiting_approval_id": None,
                "approval_proposal": None,
                "resume_token": None,
                "created_at": now,
                "updated_at": now,
            },
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
            if not hmac.compare_digest(str(resume_token or ""), str(expected)):
                raise PermissionError("invalid resume_token")
            return
        if resume_token is not None:
            raise PermissionError("run is not waiting for approval")
        if expected:
            raise PermissionError("resume_token should not exist for non-waiting run")

    def save_waiting(
        self,
        run_id: str,
        plan_json: dict[str, Any],
        current_step_index: int,
        results: list[dict[str, Any]],
        approval_id: str,
        approval_proposal: Optional[dict[str, Any]] = None,
    ) -> str:
        token = secrets.token_urlsafe(32)
        self.update(
            run_id,
            {
                "status": "waiting_approval",
                "plan_json": plan_json,
                "current_step_index": current_step_index,
                "results_json": results,
                "waiting_approval_id": approval_id,
                "approval_proposal_json": approval_proposal or {},
                "resume_token": token,
            },
        )
        return token

    def consume_resume_token(self, run_id: str, resume_token: Optional[str]) -> None:
        if resume_token is None:
            raise PermissionError("invalid resume_token")

        def update(row: dict[str, Any]) -> dict[str, Any]:
            if row.get("status") != "waiting_approval":
                raise PermissionError("run is not waiting for approval")
            if not row.get("resume_token") or not hmac.compare_digest(str(resume_token or ""), str(row.get("resume_token"))):
                raise PermissionError("invalid resume_token")
            row["status"] = "resuming"
            row["resume_token"] = None
            row["updated_at"] = time.time()
            return row

        self.state.update_locked(self.namespace, run_id, update)

    def mark_resume_failed(self, run_id: str, error: str) -> None:
        run = self.get(run_id) or {}
        results = list(run.get("results") or [])
        results.append({"ok": False, "status": "resume_failed", "error": str(error)})
        self.update(run_id, {"status": "resume_failed", "results_json": results, "resume_token": None})

    def list_resuming(self, *, older_than_seconds: float = 0, limit: int = 100) -> list[dict[str, Any]]:
        cutoff = time.time() - max(0.0, float(older_than_seconds))
        rows = self.state.list(self.namespace, status="resuming", limit=limit)
        return [row for row in rows if float(row.get("updated_at") or 0) <= cutoff]

    def complete(self, run_id: str, status: str, results: list[dict[str, Any]]) -> None:
        self.update(
            run_id,
            {"status": status, "results_json": results, "waiting_approval_id": None, "approval_proposal_json": None, "resume_token": None},
        )

    def update(self, run_id: str, fields: dict[str, Any]) -> None:
        unknown = set(fields) - self.UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unsupported run update field(s): {', '.join(sorted(unknown))}")

        def update(row: dict[str, Any]) -> dict[str, Any]:
            for key, value in fields.items():
                if key == "plan_json":
                    row["plan_json"] = value if isinstance(value, dict) or value is None else json.loads(value)
                elif key == "results_json":
                    row["results"] = value if isinstance(value, list) or value is None else json.loads(value)
                elif key == "approval_proposal_json":
                    row["approval_proposal"] = value if isinstance(value, dict) or value is None else json.loads(value)
                else:
                    row[key] = value
            row["updated_at"] = time.time()
            return row

        self.state.update_locked(self.namespace, run_id, update)

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        return self.state.get(self.namespace, run_id)

    def get_by_approval(self, approval_id: str) -> Optional[dict[str, Any]]:
        rows = self.state.list(self.namespace, limit=1000)
        for row in rows:
            if row.get("waiting_approval_id") == approval_id:
                return row
        return None


class PostgresBreakGlassStore:
    namespace = "break_glass_sessions"

    def __init__(self, state: _PostgresJsonState, *, audit_log: Path):
        self.state = state
        self.audit_log = Path(audit_log).expanduser()
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)

    def open(
        self, *, session_id: str, actor: str, reason: str, approved_by: str, ttl_seconds: int = 900, metadata: dict[str, Any] | None = None
    ) -> BreakGlassSession:
        actor = actor.strip()
        approved_by = approved_by.strip()
        reason = reason.strip()
        if not actor or not approved_by or not reason:
            raise ValueError("actor, approved_by, and reason are required")
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("break-glass ttl must be between 1 and 3600 seconds")
        now = time.time()
        expires_at = now + int(ttl_seconds)
        row = {
            "session_id": session_id,
            "actor": actor,
            "reason": reason,
            "approved_by": approved_by,
            "created_at": now,
            "expires_at": expires_at,
            "revoked_at": None,
            "metadata": metadata or {},
        }
        if not self.state.insert_once(self.namespace, session_id, row):
            raise ValueError("break-glass session already exists")
        session = BreakGlassSession(session_id, actor, reason, expires_at, approved_by, True)
        self._audit("break_glass.open", session=session, metadata=metadata or {})
        return session

    def revoke(self, session_id: str, *, revoked_by: str) -> None:
        revoked_by = revoked_by.strip()
        if not revoked_by:
            raise ValueError("revoked_by is required")

        def update(row: dict[str, Any]) -> dict[str, Any]:
            if row.get("revoked_at") is None:
                row["revoked_at"] = time.time()
            return row

        self.state.update_locked(self.namespace, session_id, update)
        self._audit("break_glass.revoke", session_id=session_id, revoked_by=revoked_by)

    def get(self, session_id: str) -> BreakGlassSession:
        row = self.state.get(self.namespace, session_id)
        if not row:
            raise KeyError(f"break-glass session not found: {session_id}")
        active = bool(row.get("revoked_at") is None and float(row.get("expires_at") or 0) > time.time())
        return BreakGlassSession(
            str(row["session_id"]), str(row["actor"]), str(row["reason"]), float(row["expires_at"]), str(row["approved_by"]), active
        )

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


class PostgresWebhookSecurity:
    def __init__(self, state: _PostgresJsonState, cfg: Optional[WebhookSecurityConfig] = None):
        self.state = state
        self.cfg = cfg or WebhookSecurityConfig()

    def guard(
        self, *, channel: str, body: bytes, source_key: str, message_id: Optional[str] = None, timestamp: Optional[float] = None
    ) -> dict[str, Any]:
        self._check_timestamp(timestamp)
        self._check_rate(channel, source_key)
        digest = hashlib.sha256(channel.encode("utf-8") + b":" + (message_id.encode("utf-8") if message_id else body)).hexdigest()
        inserted = self.state.insert_once(
            "webhook_seen", digest, {"digest": digest, "channel": channel, "created_at": time.time(), "status": "seen"}
        )
        if not inserted:
            raise PermissionError(f"duplicate webhook blocked: {channel}")
        return {"ok": True, "digest": digest}

    def _check_timestamp(self, timestamp: Optional[float]) -> None:
        if timestamp is not None and abs(time.time() - timestamp) > self.cfg.replay_ttl_seconds:
            raise PermissionError("webhook timestamp outside replay window")

    def _check_rate(self, channel: str, source_key: str) -> None:
        bucket = int(time.time() // self.cfg.rate_limit_window_seconds)
        key = f"{channel}:{source_key}:{bucket}"

        def update(row: dict[str, Any]) -> dict[str, Any]:
            row["count"] = int(row.get("count") or 0) + 1
            return row

        if not self.state.insert_once("webhook_rate", key, {"key": key, "count": 1, "bucket": bucket, "status": "active"}):
            row = self.state.update_locked("webhook_rate", key, update)
        else:
            row = self.state.get("webhook_rate", key) or {"count": 1}
        if int(row.get("count") or 0) > self.cfg.rate_limit_max_requests:
            raise PermissionError(f"webhook rate limit exceeded for {channel}:{source_key}")


class PostgresJobQueue:
    namespace = "jobs"

    def __init__(self, state: _PostgresJsonState, *, max_retries: int = 3, base_retry_seconds: int = 30):
        self.state = state
        self.max_retries = max_retries
        self.base_retry_seconds = base_retry_seconds
        self.metrics: Any = None

    def enqueue(self, message: ChannelMessage, *, source_key: Optional[str] = None) -> dict[str, Any]:
        now = time.time()
        source = source_key or message.thread_id or message.sender_id or "unknown"
        payload_json = json.dumps(asdict(message), ensure_ascii=False, sort_keys=True, default=str)
        seed = f"{message.channel}:{source}:{message.message_id or hashlib.sha256(payload_json.encode('utf-8')).hexdigest()}"
        dedupe_key = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        job_id = str(uuid.uuid4())
        row = {
            "id": job_id,
            "dedupe_key": dedupe_key,
            "channel": message.channel,
            "message_id": message.message_id,
            "source_key": source,
            "payload_json": payload_json,
            "status": "pending",
            "retry_count": 0,
            "max_retries": self.max_retries,
            "next_retry_at": 0.0,
            "locked_at": None,
            "created_at": now,
            "updated_at": now,
            "last_error": None,
            "result_json": None,
        }
        existing = self.state.find_by_field(self.namespace, "dedupe_key", dedupe_key)
        if existing:
            return {"job_id": existing["id"], "created": False, "dedupe_key": dedupe_key}
        try:
            created = self.state.insert_once(self.namespace, job_id, row)
        except Exception:
            existing = self.state.find_by_field(self.namespace, "dedupe_key", dedupe_key)
            if existing:
                return {"job_id": existing["id"], "created": False, "dedupe_key": dedupe_key}
            raise
        return {"job_id": job_id, "created": created, "dedupe_key": dedupe_key}

    def claim_next(self) -> Optional[dict[str, Any]]:
        now = time.time()

        def pred(row: dict[str, Any]) -> bool:
            return row.get("status") in {"pending", "retry"} and float(row.get("next_retry_at") or 0) <= now

        def upd(row: dict[str, Any]) -> dict[str, Any]:
            row["status"] = "running"
            row["locked_at"] = now
            row["updated_at"] = now
            return row

        return self.state.claim_one(self.namespace, pred, upd)

    def recover_stale_running(self, *, lease_seconds: int = 300) -> int:
        cutoff = time.time() - max(0, int(lease_seconds))
        recovered = 0
        for row in self.state.list(self.namespace, status="running", limit=1000):
            if row.get("locked_at") is not None and float(row.get("locked_at") or 0) <= cutoff:
                self.fail(row["id"], f"stale running job recovered after {lease_seconds}s lease")
                recovered += 1
        return recovered

    def complete(self, job_id: str, result: Any = None) -> None:
        def upd(row: dict[str, Any]) -> dict[str, Any]:
            row["status"] = "completed"
            row["result_json"] = json.dumps(result, ensure_ascii=False, default=str)
            row["locked_at"] = None
            row["updated_at"] = time.time()
            return row

        self.state.update_locked_by_field(self.namespace, "id", job_id, upd)

    def fail(self, job_id: str, error: Union[BaseException, str]) -> dict[str, Any]:
        def upd(row: dict[str, Any]) -> dict[str, Any]:
            retry_count = int(row.get("retry_count") or 0) + 1
            max_retries = int(row.get("max_retries") or self.max_retries)
            status = "dead_letter" if retry_count > max_retries else "retry"
            row.update(
                {
                    "status": status,
                    "retry_count": retry_count,
                    "next_retry_at": 0
                    if status == "dead_letter"
                    else time.time() + self.base_retry_seconds * (2 ** max(0, retry_count - 1)),
                    "locked_at": None,
                    "updated_at": time.time(),
                    "last_error": str(error)[:4000],
                }
            )
            return row

        row = self.state.update_locked_by_field(self.namespace, "id", job_id, upd)
        return {"job_id": row["id"], "status": row["status"], "retry_count": row["retry_count"], "next_retry_at": row["next_retry_at"]}

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        return self.state.find_by_field(self.namespace, "id", job_id)

    def list(self, *, status: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.state.list(self.namespace, status=status, limit=limit)

    def list_dead_letters(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.list(status="dead_letter", limit=limit)

    def requeue_dead_letter(self, job_id: str) -> dict[str, Any]:
        def upd(row: dict[str, Any]) -> dict[str, Any]:
            if row.get("status") != "dead_letter":
                raise ValueError(f"job is not dead_letter: {job_id}")
            row.update(
                {
                    "status": "pending",
                    "retry_count": 0,
                    "next_retry_at": 0,
                    "locked_at": None,
                    "last_error": None,
                    "updated_at": time.time(),
                }
            )
            return row

        self.state.update_locked_by_field(self.namespace, "id", job_id, upd)
        return {"job_id": job_id, "status": "pending"}

    def purge_dead_letter(self, job_id: str) -> dict[str, Any]:
        row = self.get(job_id)
        if not row:
            raise KeyError(job_id)
        if row.get("status") != "dead_letter":
            raise ValueError(f"job is not dead_letter: {job_id}")
        # The generic state table is keyed by UUID job id for jobs.
        self.state.delete(self.namespace, job_id)
        return {"job_id": job_id, "purged": True}

    def stats(self) -> dict[str, int]:
        return self.state.stats_by_status(self.namespace)


class PostgresOutboundMessageStore:
    namespace = "outbound_messages"

    def __init__(self, state: _PostgresJsonState, *, max_retries: int = 3, base_retry_seconds: int = 30):
        self.state = state
        self.max_retries = max(0, int(max_retries))
        self.base_retry_seconds = max(1, int(base_retry_seconds))
        self.metrics: Any = None

    def create(
        self,
        *,
        channel: str,
        recipient: str,
        payload: dict[str, Any],
        max_retries: Optional[int] = None,
        delivery_deadline_at: Optional[float] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        now = time.time()
        message_id = str(uuid.uuid4())
        idem_key = str(idempotency_key or message_id)
        row = {
            "id": message_id,
            "idempotency_key": idem_key,
            "channel": channel,
            "recipient": recipient,
            "payload_json": json.dumps(payload, ensure_ascii=False, default=str),
            "status": "pending",
            "provider_message_id": None,
            "provider_request_id": None,
            "retry_count": 0,
            "max_retries": self.max_retries if max_retries is None else max(0, int(max_retries)),
            "next_retry_at": 0.0,
            "locked_at": None,
            "delivery_deadline_at": delivery_deadline_at,
            "last_error": None,
            "error_category": None,
            "created_at": now,
            "updated_at": now,
        }
        existing = self.state.find_by_field(self.namespace, "idempotency_key", idem_key)
        if existing:
            return existing["id"]
        try:
            self.state.insert_once(self.namespace, idem_key, row)
        except Exception:
            existing = self.state.find_by_field(self.namespace, "idempotency_key", idem_key)
            if existing:
                return existing["id"]
            raise
        return row["id"]

    def claim_next(self) -> Optional[dict[str, Any]]:
        now = time.time()

        def pred(row: dict[str, Any]) -> bool:
            return (
                row.get("status") in {"pending", "retry"}
                and float(row.get("next_retry_at") or 0) <= now
                and (row.get("delivery_deadline_at") is None or float(row.get("delivery_deadline_at") or 0) >= now)
            )

        def upd(row: dict[str, Any]) -> dict[str, Any]:
            row["status"] = "running"
            row["locked_at"] = now
            row["updated_at"] = now
            return row

        return self.state.claim_one(self.namespace, pred, upd)

    def mark_sent(self, message_id: str, *, provider_message_id: Optional[str] = None, provider_request_id: Optional[str] = None) -> None:
        self._update_by_id(
            message_id,
            lambda row: {
                **row,
                "status": "sent",
                "provider_message_id": provider_message_id,
                "provider_request_id": provider_request_id,
                "locked_at": None,
                "updated_at": time.time(),
                "last_error": None,
            },
        )

    def mark_sent_by_idempotency_key(
        self, idempotency_key: str, *, provider_message_id: Optional[str] = None, provider_request_id: Optional[str] = None
    ) -> None:
        self.state.update_locked(
            self.namespace,
            idempotency_key,
            lambda row: {
                **row,
                "status": "sent",
                "provider_message_id": provider_message_id,
                "provider_request_id": provider_request_id,
                "locked_at": None,
                "updated_at": time.time(),
                "last_error": None,
            },
        )

    def mark_failed(self, message_id: str, error: str, *, dead_letter: bool = False, category: str = "unknown") -> dict[str, Any]:
        def upd(row: dict[str, Any]) -> dict[str, Any]:
            retry_count = int(row.get("retry_count") or 0) + 1
            max_retries = int(row.get("max_retries") or self.max_retries)
            status = "dead_letter" if dead_letter or retry_count > max_retries else "retry"
            row.update(
                {
                    "status": status,
                    "retry_count": retry_count,
                    "next_retry_at": 0
                    if status == "dead_letter"
                    else time.time() + self.base_retry_seconds * (2 ** max(0, retry_count - 1)),
                    "locked_at": None,
                    "last_error": str(error)[:4000],
                    "error_category": category,
                    "updated_at": time.time(),
                }
            )
            return row

        row = self._update_by_id(message_id, upd)
        return {"id": message_id, "status": row["status"], "retry_count": row["retry_count"], "next_retry_at": row["next_retry_at"]}

    def mark_ambiguous(
        self, message_id: str, error: str, *, category: str = "ambiguous_send", provider_request_id: Optional[str] = None
    ) -> dict[str, Any]:
        row = self._update_by_id(
            message_id,
            lambda r: {
                **r,
                "status": "ambiguous",
                "provider_request_id": provider_request_id or r.get("provider_request_id"),
                "locked_at": None,
                "next_retry_at": 0,
                "last_error": str(error)[:4000],
                "error_category": category,
                "updated_at": time.time(),
            },
        )
        return {"id": message_id, "status": "ambiguous", "retry_count": int(row.get("retry_count") or 0), "requires_reconciliation": True}

    def requeue(self, message_id: str) -> dict[str, Any]:
        row = self.get(message_id)
        if not row:
            raise KeyError(message_id)
        if row.get("status") == "sent":
            raise ValueError(f"sent outbound message cannot be retried: {message_id}")
        self._update_by_id(
            message_id,
            lambda r: {
                **r,
                "status": "pending",
                "retry_count": 0,
                "next_retry_at": 0,
                "locked_at": None,
                "last_error": None,
                "error_category": None,
                "updated_at": time.time(),
            },
        )
        return {"id": message_id, "status": "pending"}

    def cancel(self, message_id: str) -> dict[str, Any]:
        row = self.get(message_id)
        if not row:
            raise KeyError(message_id)
        if row.get("status") in {"sent", "cancelled"}:
            raise ValueError(f"outbound message cannot be cancelled from status {row.get('status')}: {message_id}")
        self._update_by_id(
            message_id,
            lambda r: {
                **r,
                "status": "cancelled",
                "locked_at": None,
                "updated_at": time.time(),
                "last_error": None,
                "error_category": None,
            },
        )
        return {"id": message_id, "status": "cancelled"}

    def recover_stale_running(self, *, lease_seconds: int = 300) -> int:
        cutoff = time.time() - max(0, int(lease_seconds))
        count = 0
        for row in self.state.list(self.namespace, status="running", limit=1000):
            if row.get("locked_at") is not None and float(row.get("locked_at") or 0) <= cutoff:
                self.mark_failed(row["id"], f"stale running outbound recovered after {lease_seconds}s lease")
                count += 1
        return count

    def get(self, message_id: str) -> Optional[dict[str, Any]]:
        for row in self.state.list(self.namespace, limit=1000):
            if row.get("id") == message_id:
                return row
        return None

    def find_by_idempotency_key(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        return self.state.get(self.namespace, idempotency_key)

    def list(self, *, status: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.state.list(self.namespace, status=status, limit=limit)

    def list_ambiguous(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.list(status="ambiguous", limit=limit)

    def stats(self) -> dict[str, int]:
        return self.state.stats_by_status(self.namespace)

    def _update_by_id(self, message_id: str, updater) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        row = self.get(message_id)
        if not row:
            raise KeyError(message_id)
        return self.state.update_locked(self.namespace, row["idempotency_key"], updater)


class PostgresTokenBudgetManager(TokenBudgetManager):
    namespace_cache = "llm_cache"
    namespace_usage = "llm_usage"

    def __init__(
        self,
        state: _PostgresJsonState,
        config: TokenBudgetConfig | None = None,
    ) -> None:
        self.state = state
        self.config = config or TokenBudgetConfig()

    def get_cached(self, cache_key: str) -> Optional[str]:
        if not self.config.enable_cache:
            return None
        row = self.state.get(self.namespace_cache, cache_key)
        if not row:
            return None
        if float(row.get("created_at") or 0) < time.time() - self.config.cache_ttl_seconds:
            return None
        return str(row.get("response") or "")

    def put_cached(self, *, cache_key: str, model: str, response: str) -> None:
        if not self.config.enable_cache:
            return
        self.state.put(
            self.namespace_cache, cache_key, {"cache_key": cache_key, "model": model, "response": response, "created_at": time.time()}
        )

    def record_call(
        self,
        *,
        task_id: str,
        model: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        verified_required: bool,
        budget_overridden: bool,
        reason: str,
    ) -> None:
        event_id = str(uuid.uuid4())
        self.state.put(
            self.namespace_usage,
            event_id,
            {
                "id": event_id,
                "task_id": task_id,
                "model": model,
                "estimated_input_tokens": int(estimated_input_tokens),
                "estimated_output_tokens": int(estimated_output_tokens),
                "verified_required": bool(verified_required),
                "budget_overridden": bool(budget_overridden),
                "reason": reason,
                "created_at": time.time(),
            },
        )

    def close(self) -> None:
        return None


class PostgresModelCostStore:
    namespace = "model_cost_events"

    def __init__(self, state: _PostgresJsonState):
        self.state = state
        self._sql_backed = callable(getattr(state, "_connect", None))

    def record(self, **kwargs: Any) -> str:
        event_id = str(kwargs.get("id") or uuid.uuid4())
        row = {
            "id": event_id,
            "task_id": kwargs.get("task_id"),
            "run_id": kwargs.get("run_id"),
            "actor": kwargs.get("actor"),
            "provider": str(kwargs.get("provider") or "unknown"),
            "model": str(kwargs.get("model") or "unknown"),
            "profile": str(kwargs.get("profile") or "unknown"),
            "task": kwargs.get("task"),
            "input_tokens": int(kwargs.get("input_tokens") or 0),
            "output_tokens": int(kwargs.get("output_tokens") or 0),
            "estimated_cost_usd": float(kwargs.get("estimated_cost_usd") or kwargs.get("estimated_cost") or 0.0),
            "cache_hit": bool(kwargs.get("cache_hit")),
            "created_at": float(kwargs.get("created_at") or time.time()),
        }
        if self._sql_backed:
            with self.state._connect() as con:  # type: ignore[attr-defined]
                with con.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO omnidesk_model_cost_events(
                          id, task_id, run_id, actor, provider, model, profile, task,
                          input_tokens, output_tokens, estimated_cost_usd, cache_hit, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            row["id"],
                            row["task_id"],
                            row["run_id"],
                            row["actor"],
                            row["provider"],
                            row["model"],
                            row["profile"],
                            row["task"],
                            row["input_tokens"],
                            row["output_tokens"],
                            row["estimated_cost_usd"],
                            row["cache_hit"],
                            row["created_at"],
                        ),
                    )
            return event_id
        self.state.put(self.namespace, event_id, row)
        return event_id

    def summary(self, *, days: int = 7, group_by: Optional[str] = None) -> dict[str, Any]:
        days = max(1, int(days))
        since = time.time() - days * 86400
        if self._sql_backed:
            return self._summary_sql(days=days, since=since, group_by=group_by)
        rows = [r for r in self.state.list(self.namespace, limit=250000) if float(r.get("created_at") or 0) >= since]
        calls = len(rows)
        input_tokens = sum(int(r.get("input_tokens") or 0) for r in rows)
        output_tokens = sum(int(r.get("output_tokens") or 0) for r in rows)
        cost = sum(float(r.get("estimated_cost_usd") or 0.0) for r in rows)
        cache_hits = sum(1 for r in rows if r.get("cache_hit"))
        grouped: dict[str, dict[str, Any]] = {}
        if group_by in {"provider", "actor", "task", "profile", "model"}:
            key_field = "task_id" if group_by == "task" else group_by
            for r in rows:
                key = str(r.get(key_field) or "unknown")
                bucket = grouped.setdefault(key, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0})
                bucket["calls"] += 1
                bucket["input_tokens"] += int(r.get("input_tokens") or 0)
                bucket["output_tokens"] += int(r.get("output_tokens") or 0)
                bucket["estimated_cost_usd"] += float(r.get("estimated_cost_usd") or 0.0)
        return {
            "days": days,
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": cost,
            "cache_hits": cache_hits,
            "group_by": group_by,
            "groups": grouped,
        }

    def _summary_sql(self, *, days: int, since: float, group_by: Optional[str]) -> dict[str, Any]:
        allowed_columns = {"provider": "provider", "actor": "actor", "task": "task_id", "profile": "profile", "model": "model"}
        with self.state._connect() as con:  # type: ignore[attr-defined]
            with con.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                           COALESCE(SUM(estimated_cost_usd),0), COALESCE(SUM(CASE WHEN cache_hit THEN 1 ELSE 0 END),0)
                    FROM omnidesk_model_cost_events WHERE created_at >= %s
                    """,
                    (since,),
                )
                row = cur.fetchone() or (0, 0, 0, 0.0, 0)
                grouped: dict[str, dict[str, Any]] = {}
                column = allowed_columns.get(str(group_by or ""))
                if column:
                    from psycopg import sql

                    column_identifier = sql.Identifier(column)
                    cur.execute(
                        sql.SQL(
                            """
                            SELECT COALESCE({column}, ''), COUNT(*), COALESCE(SUM(input_tokens),0),
                                   COALESCE(SUM(output_tokens),0), COALESCE(SUM(estimated_cost_usd),0)
                            FROM omnidesk_model_cost_events
                            WHERE created_at >= %s
                            GROUP BY COALESCE({column}, '')
                            """
                        ).format(column=column_identifier),
                        (since,),
                    )
                    for item in cur.fetchall():
                        grouped[str(item[0] or "unknown")] = {
                            "calls": int(item[1]),
                            "input_tokens": int(item[2]),
                            "output_tokens": int(item[3]),
                            "estimated_cost_usd": float(item[4]),
                        }
        return {
            "days": days,
            "calls": int(row[0]),
            "input_tokens": int(row[1]),
            "output_tokens": int(row[2]),
            "estimated_cost_usd": float(row[3]),
            "cache_hits": int(row[4]),
            "group_by": group_by,
            "groups": grouped,
        }

    def close(self) -> None:
        return None


class PostgresExperimentManager:
    namespace_experiments = "learning_experiments"
    namespace_observations = "learning_experiment_observations"

    def __init__(self, state: _PostgresJsonState):
        from omnidesk_agent.self_learning.experiments.cohort_assignment import CohortAssigner
        from omnidesk_agent.self_learning.experiments.metric_collector import ExperimentMetricCollector
        from omnidesk_agent.self_learning.experiments.winner_selector import WinnerSelector

        self.state = state
        self.assigner = CohortAssigner()
        self.collector = ExperimentMetricCollector()
        self.selector = WinnerSelector()

    def create(self, spec) -> dict[str, Any]:
        if spec.treatment_percent < 0 or spec.treatment_percent > 100:
            raise ValueError("treatment_percent must be between 0 and 100")
        payload = spec.to_dict()
        self.state.put(self.namespace_experiments, str(payload["experiment_id"]), payload)
        return payload

    def get(self, experiment_id: str) -> Optional[dict[str, Any]]:
        return self.state.get(self.namespace_experiments, experiment_id)

    def assign(self, experiment_id: str, unit_id: str):
        spec = self.get(experiment_id)
        if not spec or spec.get("status") != "running":
            raise ValueError("experiment is not running")
        return self.assigner.assign(experiment_id, unit_id, treatment_percent=float(spec["treatment_percent"]))

    def record(self, observation) -> dict[str, Any]:
        payload = observation.to_dict()
        payload["created_at"] = time.time()
        self.state.put(self.namespace_observations, str(uuid.uuid4()), payload)
        return payload

    def observations(self, experiment_id: str) -> list[Any]:
        from omnidesk_agent.self_learning.experiments.metric_collector import ExperimentObservation

        rows = [
            r for r in self.state.list(self.namespace_observations, limit=10000, ascending=True) if r.get("experiment_id") == experiment_id
        ]
        return [
            ExperimentObservation(
                experiment_id=str(r["experiment_id"]),
                unit_id=str(r["unit_id"]),
                arm=str(r["arm"]),
                success=bool(r["success"]),
                reward=float(r["reward"]),
                cost=float(r["cost"]),
                latency_ms=float(r["latency_ms"]),
                safety_violation=bool(r["safety_violation"]),
                metadata=dict(r.get("metadata") or {}),
            )
            for r in rows
        ]

    def summary(self, experiment_id: str) -> dict[str, dict[str, float]]:
        return self.collector.summarize(self.observations(experiment_id))

    def select_winner(self, experiment_id: str, **kwargs: Any):
        return self.selector.select(self.summary(experiment_id), **kwargs)

    def close(self) -> None:
        return None


class PostgresExperienceStore:
    namespace_legacy = "memory_experiences"
    namespace_structured = "structured_experiences"
    namespace_metrics = "learning_metrics"
    namespace_audit = "memory_governance_audit"

    SENSITIVE_LEGACY_FIELDS = ["task", "plan", "outcome", "tags"]
    SENSITIVE_STRUCTURED_FIELDS = [
        "goal",
        "failure_reason",
        "solution_attempted",
        "recommended_next_action",
        "human_feedback",
        "tags",
        "raw_trace",
    ]

    def __init__(self, state: _PostgresJsonState, privacy_config=None, encryption=None):
        from omnidesk_agent.config import MemoryPrivacyConfig
        from omnidesk_agent.privacy.encryption import EncryptionProvider
        from omnidesk_agent.privacy.redaction import MemoryPrivacyFilter
        from omnidesk_agent.memory.governed_writer import GovernedMemoryWriter

        self.state = state
        self.privacy = MemoryPrivacyFilter()
        self.privacy_config = privacy_config or MemoryPrivacyConfig()
        if encryption is not None:
            self.encryption = encryption
        elif self.privacy_config.encrypt_at_rest:
            self.encryption = EncryptionProvider.from_env(
                self.privacy_config.encryption_key_env, required=True, key_id=self.privacy_config.encryption_key_id
            )
        else:
            self.encryption = EncryptionProvider.disabled()
        self.governed_writer = GovernedMemoryWriter()

    def _next_id(self, namespace: str) -> int:
        # Monotonic enough for runtime IDs, and stored as string keys for JSONB state.
        return int(time.time() * 1_000_000)

    def _encrypt_text(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        if self.encryption.enabled:
            return self.encryption.encrypt_text(str(value))
        return value

    def _decrypt_text(self, value: Any) -> Any:
        if value is None:
            return None
        if self.encryption.enabled:
            return self.encryption.decrypt_text(str(value))
        return value

    def _audit_memory_governance(self, event: dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("created_at", time.time())
        self.state.put(self.namespace_audit, str(uuid.uuid4()), event)

    def add(
        self,
        task: str,
        plan: str = "",
        outcome: str = "",
        tags: Optional[list[str]] = None,
        *,
        channel: str = "unknown",
        actor: str = "unknown",
        privacy_level: str = "normal",
    ) -> int:
        self._audit_memory_governance(
            {
                "event": "memory_governance_decision",
                "allow_write": True,
                "namespace": f"{channel}:{actor}",
                "privacy_level": privacy_level,
                "reason": "legacy task trace redacted before persistence",
            }
        )
        eid = self._next_id(self.namespace_legacy)
        row = {
            "id": eid,
            "created_at": time.time(),
            "task": self._encrypt_text(self.privacy.redact_text(task)),
            "plan": self._encrypt_text(self.privacy.redact_text(plan)),
            "outcome": self._encrypt_text(self.privacy.redact_text(outcome)),
            "tags": self._encrypt_text(json.dumps(tags or [], ensure_ascii=False)),
        }
        self.state.put(self.namespace_legacy, str(eid), row)
        return eid

    def _decode_legacy(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for field in self.SENSITIVE_LEGACY_FIELDS:
            out[field] = self._decrypt_text(out.get(field))
        try:
            out["tags"] = json.loads(out.get("tags") or "[]")
        except Exception:
            out["tags"] = []
        return out

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        q = query.lower()
        rows = [self._decode_legacy(r) for r in self.state.list(self.namespace_legacy, limit=max(limit * 20, limit))]
        return [
            r
            for r in rows
            if q in str(r.get("task", "")).lower()
            or q in str(r.get("plan", "")).lower()
            or q in str(r.get("outcome", "")).lower()
            or q in str(r.get("tags", "")).lower()
        ][:limit]

    def add_experience(self, experience: dict[str, Any], *, channel: str = "unknown", actor: str = "unknown") -> int:
        governed = self.governed_writer.prepare(
            experience,
            channel=channel,
            actor=actor,
            privacy_level=str(experience.get("privacy_level", "normal")),
            audit=self._audit_memory_governance,
        )
        if not governed.ok:
            return -1
        experience = governed.payload
        now = time.time()
        eid = self._next_id(self.namespace_structured)
        values = {
            "id": eid,
            "created_at": now,
            "updated_at": now,
            "task_type": experience.get("task_type", "unknown"),
            "goal": experience.get("goal", ""),
            "success": bool(experience.get("success")),
            "failure_reason": experience.get("failure_reason"),
            "solution_attempted": json.dumps(experience.get("solution_attempted", []), ensure_ascii=False),
            "recommended_next_action": experience.get("recommended_next_action"),
            "risk_level": experience.get("risk_level", "medium"),
            "reusable_skill": bool(experience.get("reusable_skill")),
            "tool_cost": float(experience.get("tool_cost", 0) or 0),
            "success_score": float(experience.get("success_score", 1.0 if experience.get("success") else 0.0)),
            "human_feedback": experience.get("human_feedback"),
            "last_used_at": experience.get("last_used_at"),
            "privacy_level": experience.get("privacy_level", "normal"),
            "expires_at": experience.get("expires_at"),
            "memory_status": experience.get("memory_status", "candidate"),
            "confidence": float(experience.get("confidence", 0.5) or 0.5),
            "validation_count": int(experience.get("validation_count", 0) or 0),
            "negative_example_count": int(experience.get("negative_example_count", 0) or 0),
            "last_reviewed_at": experience.get("last_reviewed_at"),
            "promotion_reason": experience.get("promotion_reason"),
            "tags": json.dumps(experience.get("tags", []), ensure_ascii=False),
            "raw_trace": json.dumps(experience.get("raw_trace", {}), ensure_ascii=False),
            "channel": str(channel or "unknown"),
            "actor": str(actor or "unknown"),
            "namespace": f"{channel or 'unknown'}:{actor or 'unknown'}",
        }
        for field in self.SENSITIVE_STRUCTURED_FIELDS:
            values[field] = self._encrypt_text(values.get(field))
        self.state.put(self.namespace_structured, str(eid), values)
        return eid

    def _decode_structured(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for field in self.SENSITIVE_STRUCTURED_FIELDS:
            out[field] = self._decrypt_text(out.get(field))
        for field in ("solution_attempted", "tags", "raw_trace"):
            try:
                out[field] = json.loads(out.get(field) or ("{}" if field == "raw_trace" else "[]"))
            except Exception:
                out[field] = {} if field == "raw_trace" else []
        return out

    def search_similar(self, query: str, limit: int = 5, *, only_reusable: bool = False) -> list[dict[str, Any]]:
        q = query.lower()
        rows = [self._decode_structured(r) for r in self.state.list(self.namespace_structured, limit=max(limit * 20, limit))]
        if only_reusable:
            rows = [r for r in rows if r.get("reusable_skill")]
        matched = [
            r
            for r in rows
            if q in str(r.get("goal", "")).lower()
            or q in str(r.get("failure_reason", "")).lower()
            or q in str(r.get("recommended_next_action", "")).lower()
            or q in str(r.get("tags", "")).lower()
        ]
        return matched[:limit]

    def retrieve_for_task(self, task: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.search_similar(task, limit=limit)
        now = time.time()
        for row in rows:
            key = str(row["id"])
            current = self.state.get(self.namespace_structured, key)
            if current:
                current["last_used_at"] = now
                current["updated_at"] = now
                self.state.put(self.namespace_structured, key, current)
        return rows

    def summarize_failures(self, days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
        since = time.time() - days * 86400
        counts: dict[str, int] = {}
        for row in self.state.list(self.namespace_structured, limit=10000):
            dec = self._decode_structured(row)
            if not dec.get("success") and float(dec.get("created_at") or 0) >= since:
                key = str(dec.get("failure_reason") or "unknown")
                counts[key] = counts.get(key, 0) + 1
        return [{"failure_reason": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]]

    def list_structured(self, *, days: int = 30, limit: int = 200, statuses: Optional[list[str]] = None) -> list[dict[str, Any]]:
        since = time.time() - days * 86400
        rows = [
            self._decode_structured(r)
            for r in self.state.list(self.namespace_structured, limit=max(limit, 1000))
            if float(r.get("created_at") or 0) >= since
        ]
        if statuses:
            rows = [r for r in rows if r.get("memory_status") in statuses]
        return rows[:limit]

    def update_memory_review(
        self,
        experience_id: int,
        *,
        memory_status: str,
        confidence: float,
        reason: str = "",
        contradiction: bool = False,
        stale: bool = False,
    ) -> None:
        key = str(experience_id)
        current = self.state.get(self.namespace_structured, key)
        if not current:
            return
        current["memory_status"] = memory_status
        current["confidence"] = max(0.0, min(1.0, confidence))
        current["validation_count"] = int(current.get("validation_count") or 0) + (1 if memory_status in {"validated", "trusted"} else 0)
        current["negative_example_count"] = int(current.get("negative_example_count") or 0) + (
            1 if memory_status in {"deprecated", "blocked"} or contradiction else 0
        )
        current["last_reviewed_at"] = time.time()
        current["promotion_reason"] = reason
        current["updated_at"] = time.time()
        self.state.put(self.namespace_structured, key, current)
        self._audit_memory_governance(
            {
                "event": "memory_curator_review",
                "experience_id": experience_id,
                "memory_status": memory_status,
                "confidence": current["confidence"],
                "reason": reason,
                "contradiction": contradiction,
                "stale": stale,
            }
        )

    def purge_expired(
        self,
        *,
        dry_run: bool = True,
        now: Optional[float] = None,
        channel: Optional[str] = None,
        actor: Optional[str] = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        cutoff = time.time() if now is None else float(now)
        candidates = []
        for raw in self.state.list(self.namespace_structured, limit=max(limit, 10000)):
            if raw.get("expires_at") is None or float(raw.get("expires_at") or 0) > cutoff:
                continue
            if channel is not None and raw.get("channel") != channel:
                continue
            if actor is not None and raw.get("actor") != actor:
                continue
            candidates.append(raw)
            if len(candidates) >= limit:
                break
        ids = [int(r["id"]) for r in candidates]
        deleted_count = 0
        if not dry_run:
            for eid in ids:
                if self.state.delete(self.namespace_structured, str(eid)):
                    deleted_count += 1
        event = {
            "event": "memory_retention_purge",
            "dry_run": bool(dry_run),
            "cutoff": cutoff,
            "candidate_count": len(candidates),
            "deleted_count": deleted_count,
            "ids": ids,
            "channel": channel,
            "actor": actor,
            "namespace_filter_applied": channel is not None or actor is not None,
        }
        self._audit_memory_governance(event)
        return {
            "dry_run": bool(dry_run),
            "cutoff": cutoff,
            "candidate_count": len(candidates),
            "deleted_count": deleted_count,
            "ids": ids,
            "items": candidates,
            "channel": channel,
            "actor": actor,
            "namespace_filter_applied": channel is not None or actor is not None,
        }

    def record_metric(
        self,
        *,
        success: bool,
        manual_intervention: bool = False,
        tool_error: bool = False,
        repeat_failure: bool = False,
        skill_reuse: bool = False,
        rollback: bool = False,
        security_violation: bool = False,
    ) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        current = self.state.get(self.namespace_metrics, day) or {
            "day": day,
            "task_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "manual_intervention_count": 0,
            "tool_error_count": 0,
            "repeat_failure_count": 0,
            "skill_reuse_count": 0,
            "rollback_count": 0,
            "security_violation_count": 0,
        }
        current["task_count"] += 1
        current["success_count" if success else "failure_count"] += 1
        for flag, field in [
            (manual_intervention, "manual_intervention_count"),
            (tool_error, "tool_error_count"),
            (repeat_failure, "repeat_failure_count"),
            (skill_reuse, "skill_reuse_count"),
            (rollback, "rollback_count"),
            (security_violation, "security_violation_count"),
        ]:
            if flag:
                current[field] += 1
        current["updated_at"] = time.time()
        self.state.put(self.namespace_metrics, day, current)

    def metrics_report(self, days: int = 7) -> dict[str, Any]:
        cutoff = time.time() - days * 86400
        rows = [r for r in self.state.list(self.namespace_metrics, limit=days + 10) if float(r.get("updated_at") or 0) >= cutoff]
        totals: dict[str, Any] = {
            "task_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "manual_intervention_count": 0,
            "tool_error_count": 0,
            "repeat_failure_count": 0,
            "skill_reuse_count": 0,
            "rollback_count": 0,
            "security_violation_count": 0,
        }
        for r in rows:
            for k in totals:
                totals[k] += int(r.get(k) or 0)
        task_count = totals["task_count"] or 1
        totals["success_rate"] = totals["success_count"] / task_count
        totals["days"] = days
        totals["daily"] = rows
        return totals

    def close(self) -> None:
        return None


class PostgresRuntimeStateStores:
    def __init__(self, dsn: str):
        self.state = _PostgresJsonState(dsn)
        self._dual: PostgresDualApprovalStore | None = None

    def dual_approval_store(self) -> PostgresDualApprovalStore:
        if self._dual is None:
            self._dual = PostgresDualApprovalStore(self.state)
        return self._dual

    def approval_store(self, *, ttl_seconds: int, dual_approval_store: Any | None = None) -> PostgresApprovalStore:
        return PostgresApprovalStore(
            self.state, ttl_seconds=ttl_seconds, dual_approval_store=dual_approval_store or self.dual_approval_store()
        )

    def break_glass_store(self, *, audit_log: Path) -> PostgresBreakGlassStore:
        return PostgresBreakGlassStore(self.state, audit_log=audit_log)

    def run_store(self) -> PostgresRunStore:
        return PostgresRunStore(self.state)

    def agent_run_idempotency_store(self):
        from omnidesk_agent.security.agent_run_idempotency import JsonStateAgentRunIdempotencyStore

        return JsonStateAgentRunIdempotencyStore(self.state)

    def side_effect_idempotency_store(self):
        from omnidesk_agent.security.idempotency import JsonStateSideEffectIdempotencyStore

        return JsonStateSideEffectIdempotencyStore(self.state)

    def job_queue(self) -> PostgresJobQueue:
        return PostgresJobQueue(self.state)

    def outbound_messages(self) -> PostgresOutboundMessageStore:
        return PostgresOutboundMessageStore(self.state)

    def webhook_security(self) -> PostgresWebhookSecurity:
        return PostgresWebhookSecurity(self.state)

    def learning_experiments(self) -> PostgresExperimentManager:
        return PostgresExperimentManager(self.state)

    def memory_store(self, privacy_config=None) -> PostgresExperienceStore:
        return PostgresExperienceStore(self.state, privacy_config=privacy_config)

    def token_budget_manager(self, config=None) -> PostgresTokenBudgetManager:
        return PostgresTokenBudgetManager(self.state, config=config)

    def model_cost_store(self) -> PostgresModelCostStore:
        return PostgresModelCostStore(self.state)

    def health_check(self) -> dict[str, Any]:
        # Exercise every runtime state namespace so readiness fails if schema or
        # permissions are incomplete.
        self.state.stats_by_status("approvals")
        self.state.stats_by_status("dual_approvals")
        self.state.stats_by_status("break_glass_sessions")
        self.state.stats_by_status("webhook_replay")
        self.state.stats_by_status("jobs")
        self.state.stats_by_status("outbound_messages")
        self.state.stats_by_status("runs")
        self.state.list("agent_run_idempotency", limit=1)
        self.state.list("side_effect_idempotency", limit=1)
        self.state.list("memory_experiences", limit=1)
        self.state.list("structured_experiences", limit=1)
        self.state.list("llm_cache", limit=1)
        self.state.list("llm_usage", limit=1)
        self.model_cost_store().summary(days=1)
        self.state.list("learning_experiments", limit=1)
        return {
            "ok": True,
            "stores": [
                "approvals",
                "dual_approvals",
                "break_glass",
                "webhooks",
                "jobs",
                "outbound",
                "runs",
                "agent_run_idempotency",
                "side_effect_idempotency",
                "memory",
                "token_budget",
                "model_cost",
                "learning_experiments",
            ],
        }
