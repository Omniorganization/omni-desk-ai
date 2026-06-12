from __future__ import annotations

import hmac
import os
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, HTTPException, Request

from omnidesk_agent.core.models import ChannelMessage

AdminVerifier = Callable[[Request, str], Awaitable[None]]


def register_agent_routes(app: FastAPI, cfg, rt, approvals, admin: AdminVerifier) -> None:
    @app.post("/agent/run")
    async def run_agent(request: Request, body: dict):
        await admin(request, "operator")
        secret = os.getenv(cfg.gateway.shared_secret_env, "")
        provided = body.get("secret", "")
        if secret and provided and not hmac.compare_digest(secret, provided):
            raise HTTPException(401, "bad secret")
        msg = ChannelMessage(channel="local-api", sender_id=str(body.get("actor", "owner")), text=str(body["message"]))
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
        if callable(inc) and isinstance(result, dict) and result.get("status") not in {"resume_denied", "approval_not_satisfied", "not_found", "missing_approval_id", "missing_plan"}:
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
    async def gmail_oauth_start(request: Request, redirect_uri: str, state: Optional[str] = None):
        await admin(request, "owner")
        return rt.adapters["gmail"].oauth.build_authorization_url(redirect_uri=redirect_uri, state=None)

    @app.get("/oauth/gmail/callback")
    async def gmail_oauth_callback(request: Request, code: str, redirect_uri: str, state: Optional[str] = None):
        await admin(request, "owner")
        try:
            token = rt.adapters["gmail"].oauth.exchange_code(code=code, redirect_uri=redirect_uri, state=state)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"ok": True, "token_saved": True, "keys": sorted(token.keys())}

    @app.post("/approvals")
    async def create_approval(request: Request, body: dict):
        await admin(request, "operator")
        approval_id = approvals.create(body)
        return {"ok": True, "id": approval_id}

    @app.get("/approvals")
    async def list_approvals(request: Request, status: Optional[str] = None):
        await admin(request, "viewer")
        return {"ok": True, "approvals": approvals.list(status)}

    @app.post("/approvals/{approval_id}/approve")
    async def approve_request(approval_id: str, request: Request, body: Optional[dict] = None):
        await admin(request, "owner")
        return {"ok": True, "approval": approvals.decide(approval_id, "approved", body or {})}

    @app.post("/approvals/{approval_id}/deny")
    async def deny_request(approval_id: str, request: Request, body: Optional[dict] = None):
        await admin(request, "owner")
        return {"ok": True, "approval": approvals.decide(approval_id, "denied", body or {})}
