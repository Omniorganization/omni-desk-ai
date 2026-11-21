"""Data-subject export and erasure routes.

- ``GET /admin/privacy/subjects/{actor}/export`` (owner): everything the stores
  hold about the actor, plus the audit entries that are retained.
- ``POST /admin/privacy/subjects/{actor}/erase-requests`` (operator or above):
  opens a critical approval bound to this one actor. Requesting erases
  nothing; two owners other than the requester must approve it
  (``/approvals/{id}/dual-approve``), then it is approved
  (``/approvals/{id}/approve``).
- ``POST /admin/privacy/subjects/{actor}/erase/{approval_id}`` (owner): uses up
  that approval and erases. An approval for one actor cannot erase another.

Every export and erasure is written to the security audit log, with counts,
never content. Results say which stores could not take part (``unsupported``)
and which records are kept on purpose (``retained``).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from omnidesk_agent.privacy.data_subject import AuditLogSubjectSource, DataSubjectService
from omnidesk_agent.security.dual_approval import stamp_requester
from omnidesk_agent.server_routes.agent_routes import (
    AdminVerifier,
    _idempotent_side_effect,
    _reject_break_glass_elevation,
    _require_idempotency_key,
)

_RETAINED_REASON = "tamper-evident security audit record: kept for its retention period and never rewritten"


def erase_scope_hash(actor: str) -> str:
    return hashlib.sha256(f"privacy.erase_subject:{actor}".encode("utf-8")).hexdigest()


def _service(cfg: Any, rt: Any) -> DataSubjectService:
    sources = {
        "approvals": getattr(rt, "approval_store", None),
        "memory": getattr(rt, "memory", None),
        "runs": getattr(rt, "run_store", None),
        "outbound_messages": getattr(rt, "outbound_messages", None),
        "app_sync": getattr(rt, "app_sync", None),
        # Every inbound task message (sender, text, raw payload) and its result.
        "jobs": getattr(rt, "job_queue", None),
        # Replay caches of side-effecting requests: the actor, the request key and the stored answer.
        "agent_run_idempotency": getattr(rt, "agent_run_idempotency", None),
        "side_effect_idempotency": getattr(rt, "side_effect_idempotency", None),
    }
    admin_log = getattr(getattr(rt, "admin_auth", None), "audit_log", None)
    retained = {
        "security_audit_log": (AuditLogSubjectSource(cfg.permissions.audit_log), _RETAINED_REASON),
        "admin_auth_audit_log": (AuditLogSubjectSource(admin_log) if admin_log else None, _RETAINED_REASON),
    }
    return DataSubjectService(sources, retained=retained, audit=rt.permissions._audit)


def _require_dual_controlled(approvals: Any, approval_id: str) -> None:
    approval = approvals.get(approval_id) if callable(getattr(approvals, "get", None)) else None
    proposal = (approval or {}).get("proposal") or {}
    if proposal.get("requires_dual_approval") is not True or proposal.get("risk") != "critical":
        raise HTTPException(403, "an erasure needs a dual-approved critical erase request")
    dual: Any = getattr(approvals, "dual_approval_store", None)
    try:
        ready = bool(dual is not None and dual.status(approval_id).ready)
    except KeyError:
        ready = False
    if not ready:
        raise HTTPException(403, "an erasure needs two approvers other than the requester")


def register_privacy_routes(app: FastAPI, cfg: Any, rt: Any, approvals: Any, admin: AdminVerifier) -> None:
    @app.get("/admin/privacy/subjects/{actor}/export")
    async def export_subject(actor: str, request: Request):
        decision = await admin(request, "owner")
        try:
            return await asyncio.to_thread(_service(cfg, rt).export, actor, requested_by=str(getattr(decision, "actor", "")))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/admin/privacy/subjects/{actor}/erase-requests")
    async def request_erase(actor: str, request: Request):
        decision = await admin(request, "operator")
        _reject_break_glass_elevation(decision, "request a data-subject erasure")
        requester = str(getattr(decision, "actor", "") or "")
        key = _require_idempotency_key(request)

        async def call():
            proposal = stamp_requester({
                "tool": "privacy",
                "action": "erase_subject",
                "risk": "critical",
                "args": {"actor": actor},
                "scope_hash": erase_scope_hash(actor),
                "requires_dual_approval": True,
                "reason": "erase a data subject's records",
            }, requester)
            approval_id = await asyncio.to_thread(approvals.create, proposal)
            return {"ok": True, "approval_id": approval_id, "requires_dual_approval": True}

        return await _idempotent_side_effect(rt, route="/admin/privacy/erase-requests", actor=requester, key=key, payload={"actor": actor}, call=call)

    @app.post("/admin/privacy/subjects/{actor}/erase/{approval_id}")
    async def execute_erase(actor: str, approval_id: str, request: Request):
        decision = await admin(request, "owner")
        _reject_break_glass_elevation(decision, "execute a data-subject erasure")
        caller = str(getattr(decision, "actor", "") or "")
        key = _require_idempotency_key(request)

        async def call():
            expected = {"tool": "privacy", "action": "erase_subject", "scope_hash": erase_scope_hash(actor)}
            # Whatever route created the approval, an erasure needs the four eyes the
            # erase-request route asks for: a critical, dual-controlled proposal whose
            # dual approval is complete. An owner cannot create a single-approver
            # privacy approval through /approvals and approve it alone.
            await asyncio.to_thread(_require_dual_controlled, approvals, approval_id)
            try:
                await asyncio.to_thread(approvals.consume_approved, approval_id, expected)
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
            return await asyncio.to_thread(_service(cfg, rt).erase, actor, requested_by=caller, approval_id=approval_id)

        return await _idempotent_side_effect(rt, route="/admin/privacy/erase", actor=caller, key=key, payload={"actor": actor, "approval_id": approval_id}, call=call, resource_id=approval_id)
