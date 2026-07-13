from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute

from omnidesk_agent.appsync.routes import _require_idempotency
from omnidesk_agent.security import resource_guard as resource_guard_module


logger = logging.getLogger(__name__)

CHAT_PATH = "/api/chat"
STREAM_PATH = "/api/chat/stream"
STREAM_TIMEOUT_SECONDS = 120
STREAM_CHUNK_CHARACTERS = 256


def _post_endpoint(app: FastAPI, path: str) -> Callable[[Request], Awaitable[Any]]:
    matches = [
        route
        for route in app.router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and "POST" in (route.methods or set())
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one POST route for {path}, found {len(matches)}")
    return cast(Callable[[Request], Awaitable[Any]], matches[0].endpoint)


def _remove_post_route(app: FastAPI, path: str) -> None:
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            isinstance(route, APIRoute)
            and route.path == path
            and "POST" in (route.methods or set())
        )
    ]
    app.openapi_schema = None


def _install_stream_chat_classification() -> None:
    current = resource_guard_module._route_class
    if bool(getattr(current, "_omnidesk_stream_aware", False)):
        return

    def stream_aware_route_class(path: str) -> str:
        if path == STREAM_PATH:
            return "chat"
        return current(path)

    setattr(stream_aware_route_class, "_omnidesk_stream_aware", True)
    resource_guard_module._route_class = stream_aware_route_class


def _replace_request_json(request: Request, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    setattr(request, "_body", encoded)
    setattr(request, "_json", payload)


def _encode_sse(sequence: int, event: str, data: dict[str, Any]) -> bytes:
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"id: {sequence}\nevent: {event}\ndata: {body}\n\n".encode()


async def _result_events(
    request: Request,
    result: dict[str, Any],
    last_event_id: int,
) -> AsyncIterator[bytes]:
    sequence = 1
    conversation_id = str(result.get("conversation_id") or "")
    if sequence > last_event_id:
        yield _encode_sse(sequence, "chat.started", {"conversation_id": conversation_id})
    sequence += 1

    assistant_message = result.get("assistant_message")
    message = assistant_message if isinstance(assistant_message, dict) else {}
    text = str(message.get("content") or "")
    for offset in range(0, len(text), STREAM_CHUNK_CHARACTERS):
        if await request.is_disconnected():
            return
        if sequence > last_event_id:
            yield _encode_sse(
                sequence,
                "chat.delta",
                {"text": text[offset : offset + STREAM_CHUNK_CHARACTERS]},
            )
        sequence += 1

    if sequence > last_event_id:
        usage = result.get("usage")
        yield _encode_sse(sequence, "chat.usage", usage if isinstance(usage, dict) else {})
    sequence += 1

    if sequence > last_event_id:
        yield _encode_sse(
            sequence,
            "chat.completed",
            {
                "conversation_id": conversation_id,
                "audit_trace_id": result.get("audit_trace_id"),
            },
        )


async def _failure_events(code: str, last_event_id: int) -> AsyncIterator[bytes]:
    if 1 > last_event_id:
        yield _encode_sse(1, "chat.failed", {"code": code})


def install_audited_stream_route(app: FastAPI, cfg: Any) -> None:
    """Replace the provisional stream route with the audited `/api/chat` pipeline.

    The non-streaming endpoint remains the single source of truth for authentication,
    idempotent conversation creation, persisted messages, model routing, budget
    accounting and audit traces. This adapter only delivers the completed audited
    result as bounded SSE events; it does not claim provider-native token streaming.
    """

    api_chat = _post_endpoint(app, CHAT_PATH)
    _remove_post_route(app, STREAM_PATH)
    _install_stream_chat_classification()

    guard_cfg = getattr(cfg, "api_resource_guard", None)
    configured_limit = int(getattr(guard_cfg, "max_inflight_chat_requests", 8) or 8)
    stream_limit = asyncio.Semaphore(max(1, configured_limit))

    @app.post(STREAM_PATH)
    async def api_chat_stream(request: Request):
        payload = await request.json()
        content = str(payload.get("content") or payload.get("message") or "").strip()
        if not content:
            raise HTTPException(422, "content is required")

        # Validate the key before any conversation write. The delegated `/api/chat`
        # endpoint then binds that key to both conversation creation and the turn.
        _require_idempotency(cfg, request, payload)

        try:
            last_event_id = max(
                0,
                int(request.headers.get("last-event-id", "0") or "0"),
            )
        except ValueError as exc:
            raise HTTPException(400, "last-event-id must be an integer") from exc

        normalized_payload = {**payload, "stream": False}
        _replace_request_json(request, normalized_payload)

        try:
            # Complete the audited turn before returning the response so the HTTP
            # resource-guard lease covers model execution and spend accounting.
            async with stream_limit:
                raw_result = await asyncio.wait_for(
                    api_chat(request),
                    timeout=STREAM_TIMEOUT_SECONDS,
                )
        except HTTPException:
            raise
        except asyncio.TimeoutError:
            return StreamingResponse(
                _failure_events("stream_timeout", last_event_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
            )
        except Exception:
            logger.exception("audited chat stream failed before delivery")
            return StreamingResponse(
                _failure_events("stream_failed", last_event_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
            )

        if not isinstance(raw_result, dict):
            raise HTTPException(502, {"code": "invalid_chat_result"})
        result = cast(dict[str, Any], raw_result)
        return StreamingResponse(
            _result_events(request, result, last_event_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )
