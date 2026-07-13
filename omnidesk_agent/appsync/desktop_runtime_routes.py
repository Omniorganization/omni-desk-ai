from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from omnidesk_agent.appsync.routes import (
    _actor,
    _json_body_and_raw,
    _require_signed_device_request,
)


def _renew_in_memory(
    store: Any,
    *,
    actor: str,
    task_id: str,
    device_id: str,
    lease_seconds: int,
) -> dict[str, Any]:
    now = time.time()
    lease_seconds = max(15, min(int(lease_seconds or 60), 600))
    with store._lock:
        task = store.tasks.get(task_id)
        if not task:
            raise KeyError("task not found")
        store._require_actor_org(actor, task.organization_id)
        if task.status != "running":
            raise ValueError("task is not running")
        if task.claimed_by_device_id != device_id:
            raise PermissionError("task lease belongs to another runtime")
        if task.lease_expires_at is not None and task.lease_expires_at < now:
            raise ValueError("task lease expired")
        task.lease_expires_at = now + lease_seconds
        task.updated_at = now
        store._event(
            "task.lease_renewed",
            actor,
            {
                "task_id": task_id,
                "device_id": device_id,
                "lease_expires_at": task.lease_expires_at,
                "organization_id": task.organization_id,
            },
        )
        store._persist()
        return store._record(task)


def _task_control(store: Any, *, actor: str, task_id: str, device_id: str) -> dict[str, Any]:
    with store._lock:
        task = store.tasks.get(task_id)
        if not task:
            raise KeyError("task not found")
        store._require_actor_org(actor, task.organization_id)
        owner = task.claimed_by_device_id or task.assigned_runtime_device_id
        if owner and owner != device_id:
            raise PermissionError("task belongs to another runtime")
        now = time.time()
        lease_expired = bool(task.lease_expires_at and task.lease_expires_at <= now)
        cancel_requested = task.status == "cancelled"
        return {
            "task_id": task.task_id,
            "status": task.status,
            "cancel_requested": cancel_requested,
            "lease_expired": lease_expired,
            "lease_expires_at": task.lease_expires_at,
            "attempt_count": task.attempt_count,
            "updated_at": task.updated_at,
        }


def register_desktop_runtime_control_routes(
    app: FastAPI,
    cfg: Any,
    runtime: Any,
    metrics: Any,
    admin: Any,
) -> None:
    store = runtime.app_sync

    @app.post("/app/runtime/desktop/tasks/{task_id}/lease")
    async def renew_desktop_task_lease(task_id: str, request: Request):
        decision = await admin(request, "operator")
        payload, raw_body = await _json_body_and_raw(request)
        device_id = str(payload.get("device_id") or "").strip()
        if not device_id:
            raise HTTPException(422, "device_id is required")
        await _require_signed_device_request(
            cfg=cfg,
            store=store,
            request=request,
            raw_body=raw_body,
            required_device_types={"desktop"},
            expected_device_id=device_id,
            metrics=metrics,
        )
        renew = getattr(store, "renew_task_lease", None)
        try:
            task = (
                renew(
                    actor=_actor(decision),
                    task_id=task_id,
                    device_id=device_id,
                    lease_seconds=int(payload.get("lease_seconds") or 60),
                )
                if callable(renew)
                else _renew_in_memory(
                    store,
                    actor=_actor(decision),
                    task_id=task_id,
                    device_id=device_id,
                    lease_seconds=int(payload.get("lease_seconds") or 60),
                )
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if metrics:
            metrics.inc("omnidesk_app_desktop_lease_renewed_total")
        return {"ok": True, "task": task}

    @app.post("/app/runtime/desktop/tasks/{task_id}/control")
    async def desktop_task_control(task_id: str, request: Request):
        decision = await admin(request, "operator")
        payload, raw_body = await _json_body_and_raw(request)
        device_id = str(payload.get("device_id") or "").strip()
        if not device_id:
            raise HTTPException(422, "device_id is required")
        await _require_signed_device_request(
            cfg=cfg,
            store=store,
            request=request,
            raw_body=raw_body,
            required_device_types={"desktop"},
            expected_device_id=device_id,
            metrics=metrics,
        )
        try:
            control = _task_control(
                store,
                actor=_actor(decision),
                task_id=task_id,
                device_id=device_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"ok": True, "control": control}
