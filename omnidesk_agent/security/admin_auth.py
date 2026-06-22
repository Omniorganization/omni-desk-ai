from __future__ import annotations

import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class AdminAuthDecision:
    ok: bool
    reason: str
    actor: str = "admin"
    role: str = "viewer"


class AdminAuth:
    """Unified authentication gate for all management APIs.

    Credentials:
      - Authorization: Bearer <token>
      - X-OmniDesk-Admin-Token: <token>
      - X-OmniDesk-Gateway-Secret: legacy secret

    Role model:
      - viewer: dashboard/status/metrics/read-only
      - operator: run/evaluate/generate/test
      - owner: approve/reject/resume/oauth/write-sensitive admin operations
    """

    ROLE_LEVEL = {"viewer": 1, "operator": 2, "owner": 3}

    def __init__(
        self,
        *,
        admin_token_env: str = "OMNIDESK_ADMIN_TOKEN",
        viewer_token_env: str = "OMNIDESK_VIEWER_TOKEN",
        operator_token_env: str = "OMNIDESK_OPERATOR_TOKEN",
        owner_token_env: str = "OMNIDESK_OWNER_TOKEN",
        admin_actor_env: str = "OMNIDESK_ADMIN_ACTOR",
        viewer_actor_env: str = "OMNIDESK_VIEWER_ACTOR",
        operator_actor_env: str = "OMNIDESK_OPERATOR_ACTOR",
        owner_actor_env: str = "OMNIDESK_OWNER_ACTOR",
        legacy_actor_env: str = "OMNIDESK_LEGACY_ADMIN_ACTOR",
        legacy_secret_env: Optional[str] = None,
        allow_local_without_token: bool = False,
        allowed_ips: Optional[list[str]] = None,
        audit_log: Optional[Path] = None,
        break_glass_store: Any | None = None,
        break_glass_enabled: bool = False,
    ):
        self.admin_token_env = admin_token_env
        self.role_token_envs = {
            "viewer": viewer_token_env,
            "operator": operator_token_env,
            "owner": owner_token_env,
        }
        self.admin_actor_env = admin_actor_env
        self.role_actor_envs = {
            "viewer": viewer_actor_env,
            "operator": operator_actor_env,
            "owner": owner_actor_env,
        }
        self.legacy_actor_env = legacy_actor_env
        self.legacy_secret_env = legacy_secret_env
        self.allow_local_without_token = allow_local_without_token
        self.allowed_ips = set(allowed_ips or [])
        self.audit_log = Path(audit_log).expanduser() if audit_log else None
        self.break_glass_store = break_glass_store
        self.break_glass_enabled = bool(break_glass_enabled)
        if self.audit_log:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)

    def verify_headers(self, headers: Any, client_host: Optional[str] = None, required_role: str = "viewer", path: str = "") -> AdminAuthDecision:
        legacy = os.getenv(self.legacy_secret_env, "") if self.legacy_secret_env else ""
        client_host = client_host or "unknown"

        provided = ""
        auth = headers.get("authorization", "") if headers else ""
        if auth.lower().startswith("bearer "):
            provided = auth.split(" ", 1)[1].strip()
        provided = provided or (headers.get("x-omnidesk-admin-token", "") if headers else "")
        legacy_provided = headers.get("x-omnidesk-gateway-secret", "") if headers else ""

        if self.allowed_ips and client_host not in self.allowed_ips:
            decision = AdminAuthDecision(False, "client IP is not allowed", actor="unknown", role="unknown")
            self._audit(decision, client_host, required_role, path)
            return decision

        configured_tokens = self._configured_role_tokens()
        if configured_tokens:
            matching = [(role, env_name) for token, role, env_name in configured_tokens if provided and hmac.compare_digest(provided, token)]
            matching_roles = {role for role, _ in matching}
            if len(matching_roles) > 1:
                decision = AdminAuthDecision(False, "ambiguous admin token configuration", actor="unknown", role="unknown")
                self._audit(decision, client_host, required_role, path)
                return decision

            if matching:
                role, env_name = matching[0]
                decision = self._role_decision(True, "admin token accepted", self._actor_for_token(role, env_name), role, required_role)
                decision = self._maybe_elevate_break_glass(decision, headers, required_role, path)
                self._audit(decision, client_host, required_role, path)
                return decision

            if not legacy or not (legacy_provided and hmac.compare_digest(legacy_provided, legacy)):
                decision = AdminAuthDecision(False, "missing or invalid admin token", actor="unknown", role="unknown")
                self._audit(decision, client_host, required_role, path)
                return decision

            decision = self._role_decision(True, "legacy gateway secret accepted", self._actor_for_legacy_secret(), "owner", required_role)
            decision = self._maybe_elevate_break_glass(decision, headers, required_role, path)
            self._audit(decision, client_host, required_role, path)
            return decision

        if legacy:
            if legacy_provided and hmac.compare_digest(legacy_provided, legacy):
                decision = self._role_decision(True, "legacy gateway secret accepted", self._actor_for_legacy_secret(), "owner", required_role)
                decision = self._maybe_elevate_break_glass(decision, headers, required_role, path)
                self._audit(decision, client_host, required_role, path)
                return decision
            decision = AdminAuthDecision(False, "missing or invalid legacy gateway secret", actor="unknown", role="unknown")
            self._audit(decision, client_host, required_role, path)
            return decision

        if self.allow_local_without_token and (client_host in {"127.0.0.1", "::1", "localhost"} or client_host in self.allowed_ips):
            decision = self._role_decision(True, "local development without admin token", "local-development", "owner", required_role)
            decision = self._maybe_elevate_break_glass(decision, headers, required_role, path)
            self._audit(decision, client_host, required_role, path)
            return decision

        decision = AdminAuthDecision(False, "admin token is not configured", actor="unknown", role="unknown")
        self._audit(decision, client_host, required_role, path)
        return decision

    async def verify_request(self, request: Any, required_role: str = "viewer") -> AdminAuthDecision:
        host = getattr(getattr(request, "client", None), "host", None)
        path = str(getattr(getattr(request, "url", None), "path", ""))
        return self.verify_headers(request.headers, host, required_role=required_role, path=path)

    @staticmethod
    def _actor_from_headers(headers: Any) -> str:
        raw = (headers.get("x-omnidesk-actor", "") if headers else "").strip()
        if not raw:
            return ""
        return AdminAuth._sanitize_actor(raw)

    @staticmethod
    def _sanitize_actor(raw: str) -> str:
        safe = "".join(ch for ch in raw if ch.isalnum() or ch in {"@", ".", "_", "-", ":"})
        return safe[:128]

    def _actor_for_token(self, role: str, env_name: str) -> str:
        actor_env = self.admin_actor_env if env_name == self.admin_token_env else self.role_actor_envs.get(role, "")
        configured = self._sanitize_actor(os.getenv(actor_env, "")) if actor_env else ""
        return configured or f"token:{env_name}"

    def _actor_for_legacy_secret(self) -> str:
        configured = self._sanitize_actor(os.getenv(self.legacy_actor_env, ""))
        return configured or f"token:{self.legacy_secret_env or 'legacy-gateway-secret'}"

    def _maybe_elevate_break_glass(self, decision: AdminAuthDecision, headers: Any, required_role: str, path: str) -> AdminAuthDecision:
        if not self.break_glass_enabled or self.break_glass_store is None:
            return decision
        if self.ROLE_LEVEL.get(decision.role, 0) >= self.ROLE_LEVEL.get(required_role, 0):
            return decision
        session_id = (headers.get("x-omnidesk-break-glass-session", "") if headers else "").strip()
        if not session_id:
            return decision
        actor = decision.actor
        try:
            session = self.break_glass_store.assert_active(session_id, actor=actor)
        except Exception as exc:
            return AdminAuthDecision(False, f"break-glass denied: {exc}", actor=actor, role=decision.role)
        self._audit_break_glass_use(session.session_id, actor, required_role, path)
        return AdminAuthDecision(True, f"break-glass session accepted for {required_role}", actor=actor, role=required_role)

    def _audit_break_glass_use(self, session_id: str, actor: str, required_role: str, path: str) -> None:
        if not self.audit_log:
            return
        event = {
            "ts": time.time(),
            "event": "break_glass.use",
            "session_id": session_id,
            "actor": actor,
            "required_role": required_role,
            "path": path,
        }
        with self.audit_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def _configured_role_tokens(self) -> list[tuple[str, str, str]]:
        tokens: list[tuple[str, str, str]] = []
        for role, env_name in self.role_token_envs.items():
            token = os.getenv(env_name, "")
            if token:
                tokens.append((token, role, env_name))

        legacy_admin_token = os.getenv(self.admin_token_env, "")
        if legacy_admin_token:
            tokens.append((legacy_admin_token, "owner", self.admin_token_env))
        return tokens

    def _role_decision(self, ok: bool, reason: str, actor: str, role: str, required_role: str) -> AdminAuthDecision:
        role = role if role in self.ROLE_LEVEL else "viewer"
        required_role = required_role if required_role in self.ROLE_LEVEL else "viewer"
        if ok and self.ROLE_LEVEL[role] < self.ROLE_LEVEL[required_role]:
            return AdminAuthDecision(False, f"admin role {role} is below required role {required_role}", actor=actor, role=role)
        return AdminAuthDecision(ok, reason, actor=actor, role=role)

    def _audit(self, decision: AdminAuthDecision, client_host: str, required_role: str, path: str) -> None:
        if not self.audit_log:
            return
        event = {
            "ts": time.time(),
            "event": "admin_auth",
            "ok": decision.ok,
            "reason": decision.reason,
            "actor": decision.actor,
            "role": decision.role,
            "required_role": required_role,
            "client_host": client_host,
            "path": path,
        }
        with self.audit_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
