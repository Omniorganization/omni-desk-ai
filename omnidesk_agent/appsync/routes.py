from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional, cast

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from omnidesk_agent.appsync.factory import create_appsync_store
from omnidesk_agent.appsync.store import AppSyncStore, IdempotencyConflict
from omnidesk_agent.appsync.push import dispatch_pending_push
from omnidesk_agent.models.base import ModelRequest
from omnidesk_agent.validation.production import is_production_mode


def _actor(decision: Any) -> str:
    return getattr(decision, "actor", "app-client") or "app-client"


def _store(rt: Any) -> AppSyncStore:
    store = getattr(rt, "app_sync", None)
    if store is None:
        store = create_appsync_store(rt.cfg)
        rt.app_sync = store
    return store


def _idempotency_key(
    request: Request, payload: dict[str, Any] | None = None
) -> str | None:
    header = request.headers.get("idempotency-key") or request.headers.get(
        "x-idempotency-key"
    )
    body_value = (payload or {}).get("idempotency_key")
    value = str(header or body_value or "").strip()
    return value[:180] or None


def _require_idempotency(
    cfg: Any, request: Request, payload: dict[str, Any] | None = None
) -> str | None:
    key = _idempotency_key(request, payload)
    app_sync = getattr(cfg, "app_sync", None)
    if getattr(app_sync, "require_idempotency", False) and not key:
        raise HTTPException(428, "idempotency-key is required for this write operation")
    return key


def _is_production(cfg: Any) -> bool:
    return is_production_mode(cfg)


def _is_predictable_device_id(value: str) -> bool:
    lowered = (value or "").strip().lower()
    if not lowered:
        return False
    banned = {
        "desktop",
        "desktop-1",
        "desktop-runtime",
        "desktop-device",
        "mobile",
        "mobile-1",
        "mobile-device",
        "flutter-mobile",
        "test-device",
        "demo-device",
        "sample-device",
        "web-admin-console",
    }
    return (
        lowered in banned or lowered.startswith("test-") or lowered.startswith("demo-")
    )


async def _json_body_and_raw(request: Request) -> tuple[dict[str, Any], bytes]:
    raw = await request.body()
    if not raw:
        return {}, b""
    try:
        return json.loads(raw.decode("utf-8")), raw
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "invalid JSON body") from exc


def _device_signature_enforced(cfg: Any) -> bool:
    app_sync = getattr(cfg, "app_sync", None)
    return _is_production(cfg) and bool(
        getattr(app_sync, "require_device_signed_requests_in_production", True)
    )


async def _require_signed_device_request(
    *,
    cfg: Any,
    store: AppSyncStore,
    request: Request,
    raw_body: bytes,
    required_device_types: set[str],
    expected_device_id: str | None = None,
    metrics: Any = None,
) -> None:
    if not _device_signature_enforced(cfg):
        return
    headers = request.headers
    device_id = str(
        headers.get("x-omnidesk-device-id") or expected_device_id or ""
    ).strip()
    if expected_device_id and device_id != expected_device_id:
        if metrics:
            metrics.inc(
                "omnidesk_device_signature_failures_total", reason="device_id_mismatch"
            )
        raise HTTPException(401, "device signature rejected: device_id_mismatch")
    path = request.url.path
    ok, reason = store.verify_device_request_signature(
        device_id=device_id,
        method=request.method,
        path=path,
        body=raw_body,
        timestamp=str(headers.get("x-omnidesk-timestamp") or ""),
        nonce=str(headers.get("x-omnidesk-nonce") or ""),
        signature=str(headers.get("x-omnidesk-device-signature") or ""),
        required_device_types=required_device_types,
        max_skew_seconds=int(
            getattr(
                getattr(cfg, "app_sync", None), "device_signature_max_skew_seconds", 300
            )
        ),
        nonce_ttl_seconds=int(
            getattr(
                getattr(cfg, "app_sync", None), "device_request_nonce_ttl_seconds", 600
            )
        ),
    )
    if not ok:
        if metrics:
            metrics.inc("omnidesk_device_signature_failures_total", reason=reason)
        raise HTTPException(401, f"device signature rejected: {reason}")
    if metrics:
        metrics.inc(
            "omnidesk_device_signed_requests_total",
            surface="app",
            device_type=",".join(sorted(required_device_types)),
        )


def _websocket_headers(websocket: WebSocket, cfg: Any) -> dict[str, str]:
    headers = {k.lower(): v for k, v in websocket.headers.items()}
    app_sync = getattr(cfg, "app_sync", None)
    allow_query_auth = bool(
        getattr(app_sync, "allow_websocket_query_auth", False)
    ) and not _is_production(cfg)
    token = str(websocket.query_params.get("token", "")).strip()
    if token and "authorization" not in headers and allow_query_auth:
        headers["authorization"] = f"Bearer {token}"
    actor = str(websocket.query_params.get("actor", "")).strip()
    if actor and "x-omnidesk-actor" not in headers and allow_query_auth:
        headers["x-omnidesk-actor"] = actor
    return headers


def _chat_system_prompt() -> str:
    return (
        "You are OmniDesk AI inside the enterprise Gateway. "
        "Answer the operator directly, keep security and approval boundaries explicit, "
        "and do not claim that desktop, mobile, push, signing, or external production evidence exists unless it was supplied in the request."
    )


def register_appsync_routes(
    app: FastAPI, cfg: Any, rt: Any, metrics: Any, admin: Any
) -> None:
    store = _store(rt)

    async def _complete_chat_turn(
        conversation_id: str, request: Request, payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        content = str(payload.get("content") or payload.get("message") or "").strip()
        if not content:
            raise HTTPException(422, "content is required")
        if bool(payload.get("stream", False)):
            raise HTTPException(
                422,
                "streaming chat is not enabled for this endpoint; use /api/chat/stream",
            )
        idem_key = _require_idempotency(cfg, request, payload)
        idem_payload = {**payload, "conversation_id": conversation_id}
        try:
            cached = store.get_idempotency_response(
                actor=actor,
                endpoint="conversations.ask",
                key=idem_key,
                payload=idem_payload,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if cached is not None:
            return {"ok": True, **cached}
        source_device_id = payload.get("source_device_id")
        try:
            user_message = store.add_chat_user_message(
                actor=actor,
                conversation_id=conversation_id,
                content=content,
                source_device_id=source_device_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

        router = getattr(rt, "model_router", None)
        complete = getattr(router, "complete", None)
        if not callable(complete):
            raise HTTPException(503, "model router is not configured")
        model_complete = cast(Callable[[ModelRequest], Awaitable[Any]], complete)
        model_profile = (
            str(payload.get("model_profile") or payload.get("profile") or "").strip()
            or None
        )
        metadata = {
            "actor": actor,
            "conversation_id": conversation_id,
            "source_device_id": source_device_id,
        }
        if model_profile:
            metadata["profile"] = model_profile
        try:
            response = await model_complete(
                ModelRequest(
                    system=_chat_system_prompt(),
                    user=content,
                    task="chat",
                    task_id=f"chat-{conversation_id}-{user_message['message_id']}",
                    metadata=metadata,
                )
            )
        except Exception as exc:
            metrics.inc("omnidesk_app_chat_model_errors_total") if metrics else None
            raise HTTPException(502, f"model router failed: {exc}") from exc
        try:
            assistant_message = store.add_assistant_message(
                actor=actor,
                conversation_id=conversation_id,
                content=response.text,
                provider=response.provider,
                model=response.model,
                profile=response.profile,
                usage=response.usage or {},
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        result = {
            "conversation_id": conversation_id,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "usage": response.usage or {},
            "audit_trace_id": assistant_message.get("trace_id"),
        }
        store.put_idempotency_response(
            actor=actor,
            endpoint="conversations.ask",
            key=idem_key,
            payload=idem_payload,
            response=result,
        )
        metrics.inc("omnidesk_app_chat_ask_total") if metrics else None
        return {"ok": True, **result}

    @app.get("/app/bootstrap")
    async def app_bootstrap(request: Request):
        decision = await admin(request, "viewer")
        metrics.inc(
            "omnidesk_app_requests_total", surface="shared", operation="bootstrap"
        ) if metrics else None
        return {"ok": True, **store.bootstrap(_actor(decision))}

    @app.post("/app/devices/register")
    async def app_register_device(request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        device_type = payload.get("device_type")
        if device_type not in {"desktop", "mobile", "web_admin"}:
            raise HTTPException(
                422, "device_type must be desktop, mobile, or web_admin"
            )
        idem_key = _require_idempotency(cfg, request, payload)
        app_sync_cfg = getattr(cfg, "app_sync", None)
        public_key = str(payload.get("public_key") or "").strip()
        supplied_device_id = str(payload.get("device_id") or "").strip() or None
        if _is_production(cfg) and device_type in {"desktop", "mobile"}:
            if (
                getattr(app_sync_cfg, "require_device_public_key_in_production", True)
                and not public_key
            ):
                raise HTTPException(
                    422,
                    "public_key is required for desktop/mobile device enrollment in production",
                )
            if (
                getattr(
                    app_sync_cfg, "reject_predictable_device_ids_in_production", True
                )
                and supplied_device_id
                and _is_predictable_device_id(supplied_device_id)
            ):
                raise HTTPException(
                    422,
                    "predictable device_id values are forbidden in production; use a per-install generated id",
                )
        actor = _actor(decision)
        try:
            device = store.register_device(
                actor=actor,
                device_id=supplied_device_id,
                device_type=device_type,
                name=payload.get("name") or device_type,
                platform=payload.get("platform") or "unknown",
                push_token=payload.get("push_token"),
                capabilities=payload.get("capabilities") or [],
                public_key=public_key or None,
                token_hash=payload.get("token_hash"),
                organization_id=payload.get("organization_id") or "org_default",
                idempotency_key=idem_key,
                idempotency_payload=payload,
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        metrics.inc(
            "omnidesk_app_device_registered_total", device_type=device_type
        ) if metrics else None
        return {
            "ok": True,
            "device": device,
            "sync_seq": store.sync_since(0, actor=actor)["sync_seq"],
        }

    @app.get("/app/conversations")
    async def app_list_conversations(request: Request):
        decision = await admin(request, "viewer")
        return {"ok": True, "conversations": store.list_conversations(_actor(decision))}

    @app.post("/app/conversations")
    async def app_create_conversation(request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        idem_key = _require_idempotency(cfg, request, payload)
        try:
            conversation = store.create_conversation(
                actor=_actor(decision),
                title=payload.get("title") or "New conversation",
                source_device_id=payload.get("source_device_id"),
                idempotency_key=idem_key,
                idempotency_payload=payload,
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"ok": True, "conversation": conversation}

    @app.post("/app/conversations/{conversation_id}/messages")
    async def app_add_message(conversation_id: str, request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        content = str(payload.get("content") or "").strip()
        if not content:
            raise HTTPException(422, "content is required")
        try:
            result = store.add_message_and_task(
                actor=_actor(decision),
                conversation_id=conversation_id,
                content=content,
                source_device_id=payload.get("source_device_id"),
                requires_desktop_runtime=bool(
                    payload.get("requires_desktop_runtime", False)
                ),
                risk=payload.get("risk") or "medium",
                idempotency_key=_require_idempotency(cfg, request, payload),
                idempotency_payload=payload,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        metrics.inc("omnidesk_app_task_created_total") if metrics else None
        return {"ok": True, **result}

    @app.get("/app/conversations/{conversation_id}/messages")
    async def app_list_messages(conversation_id: str, request: Request):
        decision = await admin(request, "viewer")
        try:
            messages = store.list_messages(conversation_id, actor=_actor(decision))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "messages": messages}

    @app.post("/app/conversations/{conversation_id}/ask")
    async def app_ask_conversation(conversation_id: str, request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        return await _complete_chat_turn(
            conversation_id, request, payload, _actor(decision)
        )

    @app.post("/api/chat")
    async def api_chat(request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        actor = _actor(decision)
        idem_key = _require_idempotency(cfg, request, payload)
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not conversation_id:
            idem_payload = {
                "title": payload.get("title") or "API chat",
                "source_device_id": payload.get("source_device_id"),
                "content": payload.get("content"),
                "message": payload.get("message"),
                "model_profile": payload.get("model_profile"),
                "profile": payload.get("profile"),
            }
            try:
                conversation = store.create_conversation(
                    actor=actor,
                    title=idem_payload["title"],
                    source_device_id=idem_payload["source_device_id"],
                    idempotency_key=f"chat:{actor}:{idem_key}:conversation"
                    if idem_key
                    else None,
                    idempotency_payload=idem_payload,
                )
            except IdempotencyConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
            conversation_id = conversation["conversation_id"]
        payload = {**payload, "conversation_id": conversation_id}
        return await _complete_chat_turn(conversation_id, request, payload, actor)

    @app.post("/api/chat/stream")
    async def api_chat_stream(request: Request):
        await admin(request, "operator")
        raise HTTPException(
            501,
            "streaming chat is not enabled in the source-gated candidate; use /api/chat for audited non-streaming chat",
        )

    @app.get("/app/tasks/{task_id}")
    async def app_get_task(task_id: str, request: Request):
        decision = await admin(request, "viewer")
        try:
            return {"ok": True, "task": store.get_task(task_id, actor=_actor(decision))}
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/app/tasks/{task_id}/status")
    async def app_update_task_status(task_id: str, request: Request):
        decision = await admin(request, "operator")
        payload, raw_body = await _json_body_and_raw(request)
        status = payload.get("status")
        await _require_signed_device_request(
            cfg=cfg,
            store=store,
            request=request,
            raw_body=raw_body,
            required_device_types={"desktop"},
            expected_device_id=str(
                payload.get("assigned_runtime_device_id")
                or request.headers.get("x-omnidesk-device-id")
                or ""
            ),
            metrics=metrics,
        )
        if status not in {
            "queued",
            "running",
            "blocked",
            "completed",
            "failed",
            "cancelled",
        }:
            raise HTTPException(422, "invalid task status")
        try:
            task = store.update_task_status(
                task_id=task_id,
                actor=_actor(decision),
                status=status,
                result_summary=payload.get("result_summary"),
                assigned_runtime_device_id=payload.get("assigned_runtime_device_id"),
                idempotency_key=_require_idempotency(cfg, request, payload),
                idempotency_payload=payload,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"ok": True, "task": task}

    @app.get("/app/approvals")
    async def app_list_approvals(request: Request, status: Optional[str] = None):
        decision = await admin(request, "viewer")
        return {
            "ok": True,
            "approvals": store.list_approvals(status=status, actor=_actor(decision)),
        }

    @app.post("/app/approvals/{approval_id}/decide")
    async def app_decide_approval(approval_id: str, request: Request):
        decision = await admin(request, "owner")
        payload, raw_body = await _json_body_and_raw(request)
        await _require_signed_device_request(
            cfg=cfg,
            store=store,
            request=request,
            raw_body=raw_body,
            required_device_types={"mobile", "web_admin"},
            expected_device_id=str(
                payload.get("source_device_id")
                or request.headers.get("x-omnidesk-device-id")
                or ""
            ),
            metrics=metrics,
        )
        verdict = payload.get("decision")
        if verdict not in {"approved", "rejected"}:
            raise HTTPException(422, "decision must be approved or rejected")
        try:
            approval = store.decide_approval(
                approval_id=approval_id,
                actor=_actor(decision),
                decision=verdict,
                reason=payload.get("reason"),
                idempotency_key=_require_idempotency(cfg, request, payload),
                idempotency_payload=payload,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        metrics.inc(
            "omnidesk_app_approval_decision_total", decision=verdict
        ) if metrics else None
        return {"ok": True, "approval": approval}

    @app.get("/app/notifications")
    async def app_notifications(
        request: Request, audience: Optional[str] = None, unread_only: bool = False
    ):
        decision = await admin(request, "viewer")
        return {
            "ok": True,
            "notifications": store.list_notifications(
                audience=audience, unread_only=unread_only, actor=_actor(decision)
            ),
        }

    @app.post("/app/devices/{device_id}/push-token")
    async def app_register_push_token(device_id: str, request: Request):
        decision = await admin(request, "operator")
        payload, raw_body = await _json_body_and_raw(request)
        await _require_signed_device_request(
            cfg=cfg,
            store=store,
            request=request,
            raw_body=raw_body,
            required_device_types={"desktop", "mobile"},
            expected_device_id=device_id,
            metrics=metrics,
        )
        push_token = str(payload.get("push_token") or "").strip()
        if not push_token:
            raise HTTPException(422, "push_token is required")
        try:
            device = store.register_push_token(
                actor=_actor(decision),
                device_id=device_id,
                push_token=push_token,
                platform=payload.get("platform") or "unknown",
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"ok": True, "device": device}

    @app.post("/app/devices/enrollment/start")
    async def app_start_device_enrollment(request: Request):
        decision = await admin(request, "owner")
        payload = await request.json()
        device_type = payload.get("device_type")
        pairing_code = str(payload.get("pairing_code") or "").strip()
        if device_type not in {"desktop", "mobile", "web_admin"}:
            raise HTTPException(
                422, "device_type must be desktop, mobile, or web_admin"
            )
        if len(pairing_code) < 8:
            raise HTTPException(422, "pairing_code must be at least 8 characters")
        try:
            enrollment = store.start_device_enrollment(
                actor=_actor(decision),
                device_type=device_type,
                pairing_code=pairing_code,
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"ok": True, "enrollment": enrollment}

    @app.post("/app/devices/enrollment/{enrollment_id}/complete")
    async def app_complete_device_enrollment(enrollment_id: str, request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        try:
            enrollment = store.complete_device_enrollment(
                actor=_actor(decision),
                enrollment_id=enrollment_id,
                pairing_code=str(payload.get("pairing_code") or ""),
                device_id=str(payload.get("device_id") or ""),
                public_key=str(payload.get("public_key") or ""),
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "enrollment": enrollment}

    @app.post("/app/devices/enrollment/{enrollment_id}/challenge")
    async def app_issue_device_challenge(enrollment_id: str, request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        try:
            challenge = store.issue_device_challenge(
                actor=_actor(decision),
                enrollment_id=enrollment_id,
                device_id=str(payload.get("device_id") or ""),
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "challenge": challenge}

    @app.post("/app/devices/enrollment/{enrollment_id}/verify")
    async def app_verify_device_challenge(enrollment_id: str, request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        try:
            credential = store.verify_device_challenge(
                actor=_actor(decision),
                enrollment_id=enrollment_id,
                challenge_id=str(payload.get("challenge_id") or ""),
                device_id=str(payload.get("device_id") or ""),
                signature=str(payload.get("signature") or ""),
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "credential": credential}

    @app.post("/app/devices/{device_id}/rotate-token")
    async def app_rotate_device_token(device_id: str, request: Request):
        decision = await admin(request, "operator")
        raw_body = await request.body()
        await _require_signed_device_request(
            cfg=cfg,
            store=store,
            request=request,
            raw_body=raw_body,
            required_device_types={"desktop", "mobile", "web_admin"},
            expected_device_id=device_id,
            metrics=metrics,
        )
        try:
            device = store.rotate_device_token(
                actor=_actor(decision), device_id=device_id
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "device": device}

    @app.post("/app/devices/{device_id}/revoke")
    async def app_revoke_device(device_id: str, request: Request):
        decision = await admin(request, "owner")
        payload, raw_body = await _json_body_and_raw(request)
        await _require_signed_device_request(
            cfg=cfg,
            store=store,
            request=request,
            raw_body=raw_body,
            required_device_types={"web_admin", "mobile"},
            expected_device_id=str(request.headers.get("x-omnidesk-device-id") or ""),
            metrics=metrics,
        )
        try:
            device = store.revoke_device(
                actor=_actor(decision),
                device_id=device_id,
                reason=payload.get("reason"),
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"ok": True, "device": device}

    @app.post("/app/push/dispatch")
    async def app_dispatch_push(request: Request):
        await admin(request, "owner")
        payload = await request.json()
        results = dispatch_pending_push(store, limit=int(payload.get("limit") or 100))
        return {"ok": True, "results": [result.__dict__ for result in results]}

    @app.post("/app/runtime/desktop/heartbeat")
    async def app_desktop_heartbeat(request: Request):
        decision = await admin(request, "operator")
        payload, raw_body = await _json_body_and_raw(request)
        device_id = payload.get("device_id")
        await _require_signed_device_request(
            cfg=cfg,
            store=store,
            request=request,
            raw_body=raw_body,
            required_device_types={"desktop"},
            expected_device_id=str(device_id or ""),
            metrics=metrics,
        )
        if not device_id:
            raise HTTPException(422, "device_id is required")
        status = payload.get("status") or "online"
        if status not in {"online", "offline", "degraded"}:
            raise HTTPException(422, "invalid runtime status")
        try:
            runtime = store.heartbeat_runtime(
                actor=_actor(decision),
                device_id=device_id,
                status=status,
                version=payload.get("version"),
                hostname=payload.get("hostname"),
                active_task_id=payload.get("active_task_id"),
                capabilities=payload.get("capabilities") or [],
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"ok": True, "runtime": runtime}

    @app.post("/app/runtime/desktop/claim")
    async def app_desktop_claim(request: Request):
        decision = await admin(request, "operator")
        payload, raw_body = await _json_body_and_raw(request)
        device_id = payload.get("device_id")
        await _require_signed_device_request(
            cfg=cfg,
            store=store,
            request=request,
            raw_body=raw_body,
            required_device_types={"desktop"},
            expected_device_id=str(device_id or ""),
            metrics=metrics,
        )
        if not device_id:
            raise HTTPException(422, "device_id is required")
        try:
            task = store.claim_next_task(
                actor=_actor(decision),
                device_id=device_id,
                lease_seconds=int(
                    payload.get("lease_seconds")
                    or getattr(cfg.app_sync, "task_lease_seconds", 60)
                ),
                capabilities=payload.get("capabilities") or [],
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        metrics.inc(
            "omnidesk_app_desktop_task_claim_total", claimed="true" if task else "false"
        ) if metrics else None
        return {"ok": True, "task": task}

    @app.get("/app/sync")
    async def app_sync(request: Request, since_seq: int = 0):
        decision = await admin(request, "viewer")
        return {
            "ok": True,
            **store.sync_since(int(since_seq or 0), actor=_actor(decision)),
        }

    @app.post("/app/sync")
    async def app_sync_bidirectional(request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        actor = _actor(decision)
        device_id = str(payload.get("device_id") or "").strip() or None
        remote = str(payload.get("remote") or "default")
        conflict_policy = str(payload.get("conflict_policy") or "manual-review")
        if conflict_policy not in {
            "server-wins",
            "client-wins",
            "manual-review",
            "merge",
        }:
            raise HTTPException(422, "invalid conflict_policy")
        operations = payload.get("operations") or []
        remote_events = payload.get("remote_events") or []
        if not isinstance(operations, list) or not isinstance(remote_events, list):
            raise HTTPException(422, "operations and remote_events must be arrays")
        uploaded = (
            store.receive_outbox_operations(
                actor=actor,
                operations=operations,
                remote=remote,
                device_id=device_id,
                conflict_policy=conflict_policy,
            )
            if operations
            else {"applied": 0, "duplicates": 0, "conflicts": [], "cursor": 0}
        )
        pulled = (
            store.record_remote_events(
                actor=actor,
                events=remote_events,
                remote=remote,
                device_id=device_id,
                conflict_policy=conflict_policy,
            )
            if remote_events
            else {"applied": 0, "duplicates": 0, "conflicts": [], "cursor": 0}
        )
        since_seq = int(payload.get("since_seq") or 0)
        sync = store.sync_since(since_seq, actor=actor)
        if payload.get("cursor") is not None:
            store.record_sync_cursor(
                actor=actor,
                remote=remote,
                since_seq=int(payload.get("cursor") or 0),
                device_id=device_id,
            )
        return {
            "ok": True,
            "uploaded": uploaded,
            "pulled": pulled,
            "state": store.sync_state(actor=actor),
            **sync,
        }

    @app.get("/app/sync/state")
    async def app_sync_state(request: Request):
        decision = await admin(request, "viewer")
        return {"ok": True, **store.sync_state(actor=_actor(decision))}

    @app.websocket("/app/ws")
    async def app_ws(websocket: WebSocket):
        headers = _websocket_headers(websocket, cfg)
        client_host = getattr(getattr(websocket, "client", None), "host", None)
        auth_decision = rt.admin_auth.verify_headers(
            headers, client_host=client_host, required_role="viewer", path="/app/ws"
        )
        if not auth_decision.ok:
            await websocket.close(code=1008, reason="unauthorized")
            return
        actor = _actor(auth_decision)
        await websocket.accept()
        since_seq = 0
        try:
            try:
                since_seq = int(websocket.query_params.get("since_seq", "0"))
            except ValueError:
                since_seq = 0
            while True:
                sync = store.sync_since(since_seq, actor=actor)
                await websocket.send_json({"ok": True, **sync})
                since_seq = int(sync["sync_seq"])
                await asyncio.sleep(2)
        except WebSocketDisconnect:
            return
