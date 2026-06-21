from __future__ import annotations

import hmac
import os
import uuid
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from omnidesk_agent.core.models import ChannelMessage

AdminVerifier = Callable[[Request, str], Awaitable[object]]


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=12000)
    secret: Optional[str] = Field(default=None, max_length=4096)
    idempotency_key: Optional[str] = Field(default=None, max_length=256)
    source_device: Optional[str] = Field(default=None, max_length=160)


def register_agent_routes(app: FastAPI, cfg, rt, approvals, admin: AdminVerifier) -> None:
    @app.post("/agent/run")
    async def run_agent(request: Request, body: AgentRunRequest):
        decision = await admin(request, "operator")
        secret = os.getenv(cfg.gateway.shared_secret_env, "")
        provided = str(body.secret or "")
        if secret and not provided:
            raise HTTPException(401, "missing secret")
        if secret and not hmac.compare_digest(secret, provided):
            raise HTTPException(401, "bad secret")
        if provided and not secret:
            raise HTTPException(400, "secret provided but gateway shared secret is not configured")
        message = body.message
        actor = str(getattr(decision, "actor", "") or "operator")
        msg = ChannelMessage(channel="local-api", sender_id=actor, text=message)
        return await rt.orchestrator.handle_message(msg)

    @app.post("/agent/resume/{run_id}")
    async def resume_agent(run_id: str, request: Request, body: Optional[dict] = None):
        await admin(request, "owner")
        body = body or {}
        metrics = getattr(rt, "metrics", None)
        inc = getattr(metrics, "inc", None)
        if callable(inc):
            inc("omnidesk_resume_attempts_total")
        result = await rt.orchestrator.resume(run_id, resume_token=body.get("resume_token"))
        if callable(inc) and isinstance(result, dict):
            status = str(result.get("status", "unknown"))
            failure_statuses = {"resume_denied", "approval_not_satisfied", "not_found", "missing_approval_id", "missing_plan"}
            if status in failure_statuses:
                inc("omnidesk_approval_resume_failures_total", status=status)
            else:
                inc("omnidesk_resume_success_total")
        return result

    @app.post("/self-upgrade/proposals/{proposal_id}/evaluate")
    async def evaluate_upgrade_proposal(proposal_id: str, request: Request, body: Optional[dict] = None):
        body = body or {}
        allow_canary = bool(body.get("allow_canary", False))
        await admin(request, "owner" if allow_canary else "operator")
        return await rt.governance.evaluate_proposal(
            proposal_id,
            old_permissions=body.get("old_permissions"),
            new_permissions=body.get("new_permissions"),
            stable_plan=body.get("stable_plan"),
            shadow_plan=body.get("shadow_plan"),
            allow_canary=allow_canary,
        )

    @app.get("/validate/connectors")
    async def validate_connectors_route(request: Request):
        await admin(request, "operator")
        from omnidesk_agent.validation.connectors import validate_connectors
        return validate_connectors(rt)

    @app.get("/validate/extensions")
    async def validate_extensions_route(request: Request):
        await admin(request, "operator")
        from omnidesk_agent.validation.extensions import validate_extensions
        return validate_extensions(rt)

    @app.get("/oauth/gmail/start")
    async def gmail_oauth_start(request: Request, redirect_uri: str):
        decision = await admin(request, "owner")
        actor = str(getattr(decision, "actor", "") or "owner")
        return rt.adapters["gmail"].oauth.build_authorization_url(redirect_uri=redirect_uri, actor=actor)

    @app.get("/oauth/gmail/callback")
    async def gmail_oauth_callback(request: Request, code: str, redirect_uri: str, state: Optional[str] = None):
        decision = await admin(request, "owner")
        actor = str(getattr(decision, "actor", "") or "owner")
        try:
            token = rt.adapters["gmail"].oauth.exchange_code(code=code, redirect_uri=redirect_uri, state=state, actor=actor)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"ok": True, "token_saved": True, "keys": sorted(token.keys())}

    @app.post("/approvals")
    async def create_approval(request: Request, body: dict):
        decision = await admin(request, "operator")
        body = dict(body)
        actor = getattr(decision, "actor", request.headers.get("x-omnidesk-actor", "operator"))
        body.setdefault("created_by", actor)
        body.setdefault("proposer", actor)
        approval_id = approvals.create(body)
        return {"ok": True, "id": approval_id, "requires_dual_approval": bool(body.get("requires_dual_approval")), "created_by": actor}

    @app.get("/approvals")
    async def list_approvals(request: Request, status: Optional[str] = None):
        await admin(request, "viewer")
        return {"ok": True, "approvals": approvals.list(status)}

    @app.get("/approvals/{approval_id}/dual-status")
    async def dual_status_request(approval_id: str, request: Request):
        await admin(request, "viewer")
        store = getattr(rt, "dual_approval_store", None)
        if store is None:
            raise HTTPException(404, "dual approval store not available")
        try:
            decision = store.status(approval_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "dual_approval": decision.__dict__}

    @app.post("/approvals/{approval_id}/dual-approve")
    async def dual_approve_request(approval_id: str, request: Request, body: Optional[dict] = None):
        decision = await admin(request, "owner")
        body = body or {}
        store = getattr(rt, "dual_approval_store", None)
        if store is None:
            raise HTTPException(404, "dual approval store not available")
        approver = str(getattr(decision, "actor", "") or request.headers.get("x-omnidesk-actor") or "owner")
        try:
            decision = store.approve(approval_id, approver)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "dual_approval": decision.__dict__}

    @app.post("/approvals/{approval_id}/approve")
    async def approve_request(approval_id: str, request: Request, body: Optional[dict] = None):
        decision = await admin(request, "owner")
        result = dict(body or {})
        result.setdefault("decided_by", getattr(decision, "actor", "owner"))
        try:
            return {"ok": True, "approval": approvals.decide(approval_id, "approved", result)}
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/approvals/{approval_id}/deny")
    async def deny_request(approval_id: str, request: Request, body: Optional[dict] = None):
        decision = await admin(request, "owner")
        result = dict(body or {})
        result.setdefault("decided_by", getattr(decision, "actor", "owner"))
        return {"ok": True, "approval": approvals.decide(approval_id, "denied", result)}


# NOTE: break-glass routes are registered here to keep approval/emergency access
# API semantics close to the agent execution routes.
def register_break_glass_routes(app: FastAPI, cfg, rt, admin: AdminVerifier) -> None:
    @app.post("/admin/break-glass/open")
    async def open_break_glass(request: Request, body: dict):
        decision = await admin(request, "owner")
        if not getattr(cfg.permissions, "break_glass_enabled", False):
            raise HTTPException(403, "break-glass is disabled")
        store = getattr(rt, "break_glass_store", None)
        if store is None:
            raise HTTPException(404, "break-glass store not available")
        session_id = str(body.get("session_id") or uuid.uuid4())
        try:
            session = store.open(
                session_id=session_id,
                actor=str(body.get("actor", "")),
                approved_by=str(getattr(decision, "actor", "") or body.get("approved_by", "")),
                reason=str(body.get("reason", "")),
                ttl_seconds=int(body.get("ttl_seconds", 900)),
                metadata={k: v for k, v in body.items() if k not in {"session_id", "actor", "approved_by", "reason", "ttl_seconds"}},
            )
        except (ValueError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "session": session.__dict__}

    @app.get("/admin/break-glass/status/{session_id}")
    async def get_break_glass_status(session_id: str, request: Request):
        await admin(request, "viewer")
        store = getattr(rt, "break_glass_store", None)
        if store is None:
            raise HTTPException(404, "break-glass store not available")
        try:
            return {"ok": True, "session": store.get(session_id).__dict__}
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/admin/break-glass/revoke/{session_id}")
    async def revoke_break_glass(session_id: str, request: Request, body: Optional[dict] = None):
        decision = await admin(request, "owner")
        body = body or {}
        store = getattr(rt, "break_glass_store", None)
        if store is None:
            raise HTTPException(404, "break-glass store not available")
        try:
            store.revoke(session_id, revoked_by=str(getattr(decision, "actor", "") or body.get("revoked_by") or request.headers.get("x-omnidesk-actor") or "owner"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "session_id": session_id, "revoked": True}
