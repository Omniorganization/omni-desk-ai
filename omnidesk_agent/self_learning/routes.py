"""Admin HTTP routes for learned artefacts (the API twin of ``self_learning.cli``).

    GET  /admin/learning/artifacts                          operator
    GET  /admin/learning/policy                             operator
    POST /admin/learning/artifacts/{id}/request-approval    operator, idempotency key
    POST /admin/learning/approvals/{id}/decide              owner, idempotency key
    POST /admin/learning/artifacts/{id}/apply               owner, idempotency key
    POST /admin/learning/policy/rollback                    owner, idempotency key

The reviewer or actor recorded is the authenticated admin identity, never a
field of the request. The gateway registers these with
``register_learning_policy_routes(app, rt, admin, open_registry=...,
require_idempotency_key=..., idempotent_side_effect=...)`` and passes its own
admin check and idempotency helpers: ``self_learning`` must not import the
HTTP layers (``scripts/check_import_graph.py``).
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Awaitable, Callable, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from omnidesk_agent.self_learning.approval import is_system_identity
from omnidesk_agent.self_learning.policy_registry import PolicyRegistry

RegistryOpener = Callable[[], AbstractContextManager[PolicyRegistry]]
AdminVerifier = Callable[[Request, str], Awaitable[object]]


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approved", "rejected"]
    reason: str = Field(default="", max_length=2000)
    idempotency_key: Optional[str] = Field(default=None, max_length=256)


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(default="", max_length=2000)
    idempotency_key: Optional[str] = Field(default=None, max_length=256)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=2000)
    to_version: Optional[str] = Field(default=None, max_length=64)
    idempotency_key: Optional[str] = Field(default=None, max_length=256)


def _actor(decision: Any) -> str:
    actor = str(getattr(decision, "actor", "") or "").strip()
    if is_system_identity(actor):
        raise HTTPException(status_code=403, detail="an authenticated human admin identity is required")
    return actor


def _run(opener: RegistryOpener, fn: Callable[[PolicyRegistry], Any]) -> Any:
    try:
        with opener() as registry:
            return fn(registry)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"not found: {exc}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def register_learning_policy_routes(
    app: FastAPI,
    rt: Any,
    admin: AdminVerifier,
    *,
    open_registry: RegistryOpener,
    require_idempotency_key: Callable[..., str],
    idempotent_side_effect: Callable[..., Awaitable[Any]],
) -> None:
    @app.get("/admin/learning/artifacts")
    async def list_learned_artifacts(request: Request, status: Optional[str] = None, limit: int = 50):
        await admin(request, "operator")
        statuses = [status] if status else None
        return {"artifacts": _run(open_registry, lambda r: [a.to_dict() for a in r.list_artifacts(statuses=statuses, limit=max(1, min(limit, 200)))])}

    @app.get("/admin/learning/policy")
    async def learning_policy(request: Request):
        await admin(request, "operator")
        return _run(open_registry, lambda r: {"active": r.active_policy().to_dict(), "history": r.history(limit=20)})

    @app.post("/admin/learning/artifacts/{artifact_id}/request-approval")
    async def request_learning_approval(artifact_id: str, request: Request):
        decision = await admin(request, "operator")
        key = require_idempotency_key(request)

        async def call():
            return _run(open_registry, lambda r: r.request_approval(artifact_id).to_dict())

        return await idempotent_side_effect(rt, route="/admin/learning/artifacts/{artifact_id}/request-approval", actor=_actor(decision),
                                            key=key, resource_id=artifact_id, payload={"artifact_id": artifact_id}, call=call)

    @app.post("/admin/learning/approvals/{approval_id}/decide")
    async def decide_learning_approval(approval_id: str, request: Request, body: DecideRequest):
        decision = await admin(request, "owner")
        key = require_idempotency_key(request, body.idempotency_key)
        reviewer = _actor(decision)

        async def call():
            return _run(open_registry, lambda r: r.decide(approval_id, body.decision, reviewer=reviewer, reason=body.reason).to_dict())

        return await idempotent_side_effect(rt, route="/admin/learning/approvals/{approval_id}/decide", actor=reviewer, key=key,
                                            resource_id=approval_id, payload={"approval_id": approval_id, "decision": body.decision, "reason": body.reason}, call=call)

    @app.post("/admin/learning/artifacts/{artifact_id}/apply")
    async def apply_learned_artifact(artifact_id: str, request: Request, body: ApplyRequest | None = None):
        body = body or ApplyRequest()
        decision = await admin(request, "owner")
        key = require_idempotency_key(request, body.idempotency_key)
        actor = _actor(decision)

        async def call():
            return _run(open_registry, lambda r: r.apply(artifact_id, actor=actor, reason=body.reason).to_dict())

        return await idempotent_side_effect(rt, route="/admin/learning/artifacts/{artifact_id}/apply", actor=actor, key=key,
                                            resource_id=artifact_id, payload={"artifact_id": artifact_id, "reason": body.reason}, call=call)

    @app.post("/admin/learning/policy/rollback")
    async def rollback_learning_policy(request: Request, body: RollbackRequest):
        decision = await admin(request, "owner")
        key = require_idempotency_key(request, body.idempotency_key)
        actor = _actor(decision)

        async def call():
            return _run(open_registry, lambda r: r.rollback(actor=actor, reason=body.reason, to_version=body.to_version).to_dict())

        return await idempotent_side_effect(rt, route="/admin/learning/policy/rollback", actor=actor, key=key,
                                            payload={"reason": body.reason, "to_version": body.to_version}, call=call)
