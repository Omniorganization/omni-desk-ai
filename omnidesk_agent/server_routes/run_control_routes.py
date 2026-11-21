"""Stopping agent work over HTTP: cancel one run, or engage the emergency stop.

- ``POST /agent/runs/{run_id}/cancel`` (operator or above): an operator may
  cancel the runs started with their own identity, an owner any run. The
  orchestrator records ``agent_run_cancelled`` in the audit log and denies the
  run's pending approval. If that entry cannot be written after the run was
  cancelled, the answer says so in ``audit_error``.
- ``GET /admin/emergency-stop`` (viewer): whether the stop is engaged.
- ``POST /admin/emergency-stop/engage`` (operator or above): stopping only takes
  capability away, so anyone who can start a run can stop them all.
- ``POST /admin/emergency-stop/release`` (owner, never through a break-glass
  elevation): giving the agent its tools back is the deliberate step.

Both stop changes are recorded as ``agent_emergency_stop``. As with the CLI, an
engage never waits on the audit log: the response says whether it was recorded.
A release is recorded before the stop is lifted; if the audit log cannot take
the entry, the stop stays engaged. The routes answer 501 until the runtime
provides cancellation and the emergency stop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from omnidesk_agent.security.audit_errors import AuditIntegrityError
from omnidesk_agent.server_routes.agent_routes import (
    AdminVerifier,
    _authenticated_actor,
    _idempotent_side_effect,
    _reject_break_glass_elevation,
    _require_idempotency_key,
)

log = logging.getLogger(__name__)


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(default="", max_length=500)
    idempotency_key: Optional[str] = Field(default=None, max_length=256)


def _started_by(run: dict[str, Any]) -> str:
    message = run.get("original_message")
    return str(message.get("sender_id") or "") if isinstance(message, dict) else ""


def _record_stop_change(rt: Any, *, engaged: bool, actor: str, reason: str) -> None:
    record = getattr(getattr(rt, "permissions", None), "record_event", None)
    if not callable(record):
        raise RuntimeError("the permission manager has no record_event")
    record("agent_emergency_stop", {"engaged": engaged, "actor": actor, "reason": reason, "via": "http"})


def _emergency_stop(rt: Any) -> Any:
    stop = getattr(rt, "emergency_stop", None)
    if stop is None:
        raise HTTPException(501, "this runtime has no emergency stop")
    return stop


async def _stopping(rt: Any, *, route: str, actor: str, key: str, payload: dict[str, Any], call: Any, resource_id: Optional[str] = None) -> Any:
    """Run a stopping action idempotently, but never let bookkeeping prevent it.

    Cancelling a run and engaging the emergency stop only take capability away.
    If the idempotency store cannot be written (unavailable, read-only, full),
    the action still runs, without replay protection. The action itself
    failing is still an error.
    """
    outcome: dict[str, Any] = {}

    async def tracked():
        outcome["started"] = True
        outcome["result"] = await call()
        return outcome["result"]

    try:
        return await _idempotent_side_effect(rt, route=route, actor=actor, key=key, payload=payload, call=tracked, resource_id=resource_id)
    except HTTPException:
        raise
    except Exception as exc:
        if "result" in outcome:  # done; only recording its answer failed
            log.warning("%s ran, but its idempotency record failed: %s", route, type(exc).__name__)
            return outcome["result"]
        if outcome.get("started"):  # the action itself failed
            raise
        log.warning("%s runs without idempotency: the store failed: %s", route, type(exc).__name__)
        return await call()


def register_run_control_routes(app: FastAPI, cfg: Any, rt: Any, admin: AdminVerifier) -> None:
    @app.post("/agent/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request, body: Optional[StopRequest] = None):
        decision = await admin(request, "operator")
        actor = _authenticated_actor(decision)
        body = body or StopRequest()
        orchestrator: Any = getattr(rt, "orchestrator", None)
        if not callable(getattr(orchestrator, "cancel", None)):
            raise HTTPException(501, "this runtime cannot cancel runs")
        if getattr(decision, "role", "") != "owner":
            run_store = getattr(rt, "run_store", None)
            run = await asyncio.to_thread(run_store.get, run_id) if run_store is not None else None
            # One answer for "no such run" and "someone else's run", so run ids cannot be probed.
            if not run or _started_by(run) != actor:
                raise HTTPException(404, "no run with this id was started by you")
        key = _require_idempotency_key(request, body.idempotency_key)

        async def call():
            try:
                return await orchestrator.cancel(run_id, reason=body.reason, actor=actor)
            except AuditIntegrityError as exc:
                # cancel() marks the run cancelled before it writes agent_run_cancelled.
                # A cancel that took effect is reported as such, never as a failure.
                run_store = getattr(rt, "run_store", None)
                run = await asyncio.to_thread(run_store.get, run_id) if run_store is not None else None
                if not run or run.get("status") != "cancelled":
                    raise HTTPException(503, "the audit log is unavailable and the run was not cancelled") from exc
                return {"ok": True, "status": "cancelled", "run_id": run_id, "audit_error": type(exc).__name__}

        return await _stopping(
            rt,
            route="/agent/runs/{run_id}/cancel",
            actor=actor,
            key=key,
            resource_id=run_id,
            payload={"run_id": run_id, "reason": body.reason},
            call=call,
        )

    @app.get("/admin/emergency-stop")
    async def emergency_stop_status(request: Request):
        await admin(request, "viewer")
        state = await asyncio.to_thread(_emergency_stop(rt).status)
        return {"ok": True, **state.to_dict()}

    @app.post("/admin/emergency-stop/engage")
    async def engage_emergency_stop(request: Request, body: Optional[StopRequest] = None):
        decision = await admin(request, "operator")
        actor = _authenticated_actor(decision)
        body = body or StopRequest()
        stop = _emergency_stop(rt)
        key = _require_idempotency_key(request, body.idempotency_key)
        reason = body.reason or "engaged over HTTP"

        async def call():
            state = await asyncio.to_thread(stop.engage, reason=reason, actor=actor)
            try:
                await asyncio.to_thread(_record_stop_change, rt, engaged=True, actor=actor, reason=reason)
                audit = "recorded"
            except Exception as exc:  # the stop itself never depends on the audit log
                log.warning("emergency stop engaged by %s but not audited: %s", actor, type(exc).__name__)
                audit = f"not recorded: {type(exc).__name__}"
            return {"ok": True, **state.to_dict(), "audit": audit}

        return await _stopping(rt, route="/admin/emergency-stop/engage", actor=actor, key=key, payload={"reason": reason}, call=call)

    @app.post("/admin/emergency-stop/release")
    async def release_emergency_stop(request: Request, body: Optional[StopRequest] = None):
        decision = await admin(request, "owner")
        _reject_break_glass_elevation(decision, "release the emergency stop")
        actor = _authenticated_actor(decision)
        body = body or StopRequest()
        stop = _emergency_stop(rt)
        key = _require_idempotency_key(request, body.idempotency_key)

        async def call():
            current = await asyncio.to_thread(stop.status)
            if not current.engaged:
                return {"ok": True, **current.to_dict()}
            try:
                await asyncio.to_thread(_record_stop_change, rt, engaged=False, actor=actor, reason=body.reason)
            except Exception as exc:
                raise HTTPException(503, "the emergency stop stays engaged: the audit log cannot record its release") from exc
            state = await asyncio.to_thread(stop.release)
            return {"ok": True, **state.to_dict()}

        return await _idempotent_side_effect(rt, route="/admin/emergency-stop/release", actor=actor, key=key, payload={"reason": body.reason}, call=call)
