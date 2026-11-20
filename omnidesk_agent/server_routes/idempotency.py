"""Idempotency for side-effecting HTTP routes.

Route modules (agent, approval and learning routes) share these: a client must
send an ``Idempotency-Key``; a repeated key with the same payload replays the
stored response, a different payload or a call still in flight is a 409, and a
failed call can be retried with the same key.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException, Request

from omnidesk_agent.security.idempotency import (
    SideEffectIdempotencyConflict,
    SideEffectIdempotencyInProgress,
    normalize_idempotency_response,
)


def require_idempotency_key(request: Request, body_key: Optional[str] = None) -> str:
    key = str(request.headers.get("idempotency-key") or body_key or "").strip()
    if not key:
        raise HTTPException(status_code=428, detail="idempotency-key required")
    if len(key) > 256:
        raise HTTPException(status_code=422, detail="idempotency-key is too long")
    return key


async def idempotent_side_effect(
    rt,
    *,
    route: str,
    actor: str,
    key: str,
    payload: dict[str, Any],
    call: Callable[[], Awaitable[Any]],
    resource_id: Optional[str] = None,
) -> Any:
    store = getattr(rt, "side_effect_idempotency", None)
    if store is None:
        return await call()
    try:
        cached = store.begin(route=route, actor=actor, key=key, resource_id=resource_id, payload=payload)
    except SideEffectIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SideEffectIdempotencyInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if cached is not None:
        return cached
    try:
        result = await call()
    except Exception:
        store.fail(route=route, actor=actor, key=key, resource_id=resource_id)
        raise
    store.complete(route=route, actor=actor, key=key, resource_id=resource_id, response=result)
    return normalize_idempotency_response(result)
