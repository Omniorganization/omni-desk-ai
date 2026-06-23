from __future__ import annotations

import hmac
import os
import uuid
from typing import Any, Awaitable, Callable, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.security.agent_run_idempotency import AgentRunIdempotencyConflict, AgentRunIdempotencyInProgress

AdminVerifier = Callable[[Request, str], Awaitable[object]]


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=12000)
    secret: Optional[str] = Field(default=None, max_length=4096)
    idempotency_key: Optional[str] = Field(default=None, max_length=256)
    source_device: Optional[str] = Field(default=None, max_length=160)


class AgentResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    resume_token: Optional[str] = Field(default=None, max_length=512)
    idempotency_key: Optional[str] = Field(default=None, max_length=256)


class UpgradeEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_permissions: Optional[list[str]] = Field(default=None, max_length=200)
    new_permissions: Optional[list[str]] = Field(default=None, max_length=200)
    stable_plan: Optional[dict[str, Any]] = None
    shadow_plan: Optional[dict[str, Any]] = None
    allow_canary: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=256)


class CreateApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tool: str = Field(min_length=1, max_length=160)
    action: Optional[str] = Field(default=None, max_length=240)
    args: dict[str, Any] = Field(default_factory=dict)
    risk: Optional[Literal["low", "medium", "high", "critical"]] = None
    reason: Optional[str] = Field(default=None, max_length=1000)
    source: Optional[str] = Field(default=None, max_length=160)
    actor: Optional[str] = Field(default=None, max_length=128)
    scope_hash: Optional[str] = Field(default=None, max_length=256)
    requires_dual_approval: bool = False
    expires_at: Optional[float] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    by: Optional[str] = Field(default=None, max_length=128)
    decided_by: Optional[str] = Field(default=None, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DualApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approver: Optional[str] = Field(default=None, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=1000)


class BreakGlassOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: Optional[str] = Field(default=None, max_length=128)
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=8, max_length=1000)
    ttl_seconds: int = Field(default=900, ge=1, le=3600)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BreakGlassRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    revoked_by: Optional[str] = Field(default=None, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=1000)


def _require_idempotency_key(request: Request, body_key: Optional[str] = None) -> str:
    key = str(request.headers.get("idempotency-key") or body_key or "").strip()
    if not key:
        raise HTTPException(status_code=428, detail="idempotency-key required")
    if len(key) > 256:
        raise HTTPException(status_code=422, detail="idempotency-key is too long")
    return key


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
        idempotency_key = _require_idempotency_key(request, body.idempotency_key)
        message = body.message
        actor = str(getattr(decision, "actor", "") or "operator")
        payload = {"route": "/agent/run", "actor": actor, "source_device": body.source_device or "", "message": message}
        idempotency = getattr(rt, "agent_run_idempotency", None)
        if idempotency is not None:
            try:
                cached = idempotency.begin(actor=actor, key=idempotency_key, source_device=body.source_device, payload=payload)
            except AgentRunIdempotencyConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except AgentRunIdempotencyInProgress as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if cached is not None:
                return cached
        msg = ChannelMessage(channel="local-api", sender_id=actor, text=message)
        try:
            result = await rt.orchestrator.handle_message(msg)
        except Exception:
            if idempotency is not None:
                idempotency.fail(actor=actor, key=idempotency_key, source_device=body.source_device)
            raise
        if idempotency is not None and isinstance(result, dict):
            idempotency.complete(actor=actor, key=idempotency_key, source_device=body.source_device, response=result)
        return result

    @app.post("/agent/resume/{run_id}")
    async def resume_agent(run_id: str, request: Request, body: AgentResumeRequest | None = None):
        await admin(request, "owner")
        body = body or AgentResumeRequest()
        _require_idempotency_key(request, body.idempotency_key)
        metrics = getattr(rt, "metrics", None)
        inc = getattr(metrics, "inc", None)
        if callable(inc):
            inc("omnidesk_resume_attempts_total")
        result = await rt.orchestrator.resume(run_id, resume_token=body.resume_token)
        if callable(inc) and isinstance(result, dict):
            status = str(result.get("status", "unknown"))
            failure_statuses = {"resume_denied", "approval_not_satisfied", "not_found", "missing_approval_id", "missing_plan"}
            if status in failure_statuses:
                inc("omnidesk_approval_resume_failures_total", status=status)
            else:
                inc("omnidesk_resume_success_total")
        return result

    @app.post("/self-upgrade/proposals/{proposal_id}/evaluate")
    async def evaluate_upgrade_proposal(proposal_id: str, request: Request, body: UpgradeEvaluateRequest | None = None):
        body = body or UpgradeEvaluateRequest()
        allow_canary = bool(body.allow_canary)
        await admin(request, "owner" if allow_canary else "operator")
        _require_idempotency_key(request, body.idempotency_key)
        return await rt.governance.evaluate_proposal(
            proposal_id,
            old_permissions=body.old_permissions,
            new_permissions=body.new_permissions,
            stable_plan=body.stable_plan,
            shadow_plan=body.shadow_plan,
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
    async def create_approval(request: Request, body: CreateApprovalRequest):
        decision = await admin(request, "operator")
        _require_idempotency_key(request)
        proposal = body.model_dump(exclude_none=True)
        actor = getattr(decision, "actor", request.headers.get("x-omnidesk-actor", "operator"))
        proposal.setdefault("created_by", actor)
        proposal.setdefault("proposer", actor)
        approval_id = approvals.create(proposal)
        return {"ok": True, "id": approval_id, "requires_dual_approval": bool(proposal.get("requires_dual_approval")), "created_by": actor}

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
    async def dual_approve_request(approval_id: str, request: Request, body: DualApproveRequest | None = None):
        decision = await admin(request, "owner")
        _require_idempotency_key(request)
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
    async def approve_request(approval_id: str, request: Request, body: ApprovalDecisionRequest | None = None):
        decision = await admin(request, "owner")
        body = body or ApprovalDecisionRequest()
        _require_idempotency_key(request)
        result = body.model_dump(exclude_none=True)
        result.setdefault("decided_by", getattr(decision, "actor", "owner"))
        try:
            return {"ok": True, "approval": approvals.decide(approval_id, "approved", result)}
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/approvals/{approval_id}/deny")
    async def deny_request(approval_id: str, request: Request, body: ApprovalDecisionRequest | None = None):
        decision = await admin(request, "owner")
        body = body or ApprovalDecisionRequest()
        _require_idempotency_key(request)
        result = body.model_dump(exclude_none=True)
        result.setdefault("decided_by", getattr(decision, "actor", "owner"))
        return {"ok": True, "approval": approvals.decide(approval_id, "denied", result)}


# NOTE: break-glass routes are registered here to keep approval/emergency access
# API semantics close to the agent execution routes.
def register_break_glass_routes(app: FastAPI, cfg, rt, admin: AdminVerifier) -> None:
    @app.post("/admin/break-glass/open")
    async def open_break_glass(request: Request, body: BreakGlassOpenRequest):
        decision = await admin(request, "owner")
        _require_idempotency_key(request)
        if not getattr(cfg.permissions, "break_glass_enabled", False):
            raise HTTPException(403, "break-glass is disabled")
        store = getattr(rt, "break_glass_store", None)
        if store is None:
            raise HTTPException(404, "break-glass store not available")
        session_id = str(body.session_id or uuid.uuid4())
        approved_by = str(getattr(decision, "actor", "") or request.headers.get("x-omnidesk-actor") or "")
        try:
            session = store.open(
                session_id=session_id,
                actor=body.actor,
                approved_by=approved_by,
                reason=body.reason,
                ttl_seconds=body.ttl_seconds,
                metadata=body.metadata,
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
    async def revoke_break_glass(session_id: str, request: Request, body: BreakGlassRevokeRequest | None = None):
        decision = await admin(request, "owner")
        body = body or BreakGlassRevokeRequest()
        _require_idempotency_key(request)
        store = getattr(rt, "break_glass_store", None)
        if store is None:
            raise HTTPException(404, "break-glass store not available")
        try:
            store.revoke(session_id, revoked_by=str(getattr(decision, "actor", "") or body.revoked_by or request.headers.get("x-omnidesk-actor") or "owner"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "session_id": session_id, "revoked": True}
