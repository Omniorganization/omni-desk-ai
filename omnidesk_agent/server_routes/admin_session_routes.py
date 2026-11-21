"""Admin sessions over HTTP: exchange a role token for a short-lived session token, revoke it.

- ``POST /admin/session/login``: any role token (viewer and up) opens a session
  with that token's actor and role and the organization the gateway assigns to
  the actor. A session token cannot open another session, and a break-glass
  elevation cannot open one. The token is returned once and stored only as an
  HMAC. The login is audited, and a login that cannot be audited is undone (503).
- ``POST /admin/session/revoke``: ``{}`` revokes the session making the request
  (logout), ``{"session_id": ...}`` one of the caller's sessions (an owner may
  name anyone's), ``{"all": true}`` all of the caller's sessions, and
  ``{"all": true, "actor": ...}`` all of another actor's sessions (owner only).
  Revoking only takes access away, so it never waits on the audit log; the
  response says whether the entry was written.

Neither route takes an idempotency key: a cached login response would keep the
session token at rest, and revoking twice is harmless.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from omnidesk_agent.security.admin_auth import AUDIT_UNAVAILABLE_REASON
from omnidesk_agent.security.admin_sessions import MAX_TTL_SECONDS, MIN_TTL_SECONDS, AdminSessionLimit
from omnidesk_agent.server_routes.agent_routes import AdminVerifier, _authenticated_actor, _reject_break_glass_elevation


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ttl_seconds: Optional[int] = Field(default=None, ge=MIN_TTL_SECONDS, le=MAX_TTL_SECONDS)
    label: str = Field(default="", max_length=64)


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: Optional[str] = Field(default=None, max_length=64)
    all: bool = False
    actor: Optional[str] = Field(default=None, max_length=128)


def _sessions(rt: Any) -> Any:
    store = getattr(getattr(rt, "admin_auth", None), "session_store", None)
    if store is None:
        raise HTTPException(501, "admin sessions are not enabled on this gateway")
    return store


def _organization(rt: Any, actor: str) -> str:
    # The organization the gateway itself assigns to the actor, as for quotas.
    resolve = getattr(getattr(rt, "app_sync", None), "_organization_for_actor", None)
    return str(resolve(actor)) if callable(resolve) else "org_default"


# Only a real credential opens a session: not a session (no chaining), and not
# local-development access, which works only from loopback and would otherwise
# become a token usable from anywhere.
_SESSION_CREDENTIALS = frozenset({"token", "legacy_secret"})


async def _revoker(rt: Any, request: Request, admin: AdminVerifier) -> Any:
    """The caller of a revocation. If the admin audit log cannot take the entry,
    the caller is still authenticated (best effort): revoking only takes access
    away, and an audit outage must not keep a leaked session alive."""
    try:
        return await admin(request, "viewer")
    except HTTPException as exc:
        if not str(exc.detail).startswith(AUDIT_UNAVAILABLE_REASON):
            raise
    decision = await rt.admin_auth.verify_request(request, "viewer", audit_best_effort=True)
    if not decision.ok:
        raise HTTPException(403, decision.reason)
    return decision


def _client(request: Request) -> str:
    return str(getattr(getattr(request, "client", None), "host", "") or "unknown")


def register_admin_session_routes(app: FastAPI, cfg: Any, rt: Any, admin: AdminVerifier) -> None:
    @app.post("/admin/session/login")
    async def login(request: Request, body: Optional[LoginRequest] = None):
        decision = await admin(request, "viewer")
        store = _sessions(rt)
        if getattr(decision, "auth_method", "") not in _SESSION_CREDENTIALS:
            raise HTTPException(403, "open an admin session with a role token, not a session token or local-development access")
        _reject_break_glass_elevation(decision, "open an admin session")
        actor = _authenticated_actor(decision)
        role = str(getattr(decision, "role", "") or "")
        body = body or LoginRequest()
        organization = await asyncio.to_thread(_organization, rt, actor)
        try:
            token, session = await asyncio.to_thread(store.create, actor=actor, role=role, organization=organization, ttl_seconds=body.ttl_seconds, label=body.label)
        except AdminSessionLimit as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after_seconds)}) from exc
        event = {**session.public(), "client_host": _client(request)}
        try:
            await asyncio.to_thread(rt.admin_auth.audit_event, "admin_session.login", event)
        except Exception as exc:
            # A login that is not on the record does not stand.
            await asyncio.to_thread(store.revoke, session.session_id)
            raise HTTPException(503, "the admin audit log cannot record this login") from exc
        return JSONResponse({"ok": True, "session_token": token, **session.public()}, headers={"Cache-Control": "no-store"})

    @app.post("/admin/session/revoke")
    async def revoke(request: Request, body: Optional[RevokeRequest] = None):
        store = _sessions(rt)
        decision = await _revoker(rt, request, admin)
        actor = _authenticated_actor(decision)
        body = body or RevokeRequest()
        owner = getattr(decision, "role", "") == "owner" and not getattr(decision, "break_glass_session_id", None)
        if body.all:
            target = body.actor or actor
            if target != actor and not owner:
                raise HTTPException(403, "only an owner can revoke another actor's sessions")
            revoked = await asyncio.to_thread(store.revoke_actor, target)
            reason = "revoke_all"
        elif body.session_id:
            session = await asyncio.to_thread(store.get, body.session_id)
            # One answer for "no such session" and "someone else's session".
            if session is None or (session.actor != actor and not owner):
                raise HTTPException(404, "no live session with this id is yours to revoke")
            target = session.actor
            revoked = [session.session_id] if await asyncio.to_thread(store.revoke, session.session_id) else []
            reason = "revoke"
        else:
            current = getattr(decision, "session_id", None)
            if not current:
                raise HTTPException(422, "this request did not use a session token; send session_id or all")
            target = actor
            revoked = [current] if await asyncio.to_thread(store.revoke, current) else []
            reason = "logout"
        event = {"session_ids": revoked, "actor": target, "revoked_by": actor, "reason": reason, "client_host": _client(request)}
        try:
            await asyncio.to_thread(rt.admin_auth.audit_event, "admin_session.revoke", event)
            audit = "recorded"
        except Exception as exc:  # revoking only takes access away: never undo it
            audit = f"not recorded: {type(exc).__name__}"
        return {"ok": True, "revoked": len(revoked), "audit": audit}
