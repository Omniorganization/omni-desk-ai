from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from omnidesk_agent.appsync.chat_service import ChatStreamEvent, ChatTurnService
from omnidesk_agent.appsync.factory import create_appsync_store
from omnidesk_agent.models.completion_streaming import CompletionOnlyStreamingRouter

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}
HEARTBEAT_SECONDS = 15.0
STREAM_QUEUE_SIZE = 64


def _actor(decision: Any) -> str:
    return str(getattr(decision, "actor", "app-client") or "app-client")


def _encode(event: ChatStreamEvent) -> bytes:
    body = json.dumps(event.data, separators=(",", ":"), ensure_ascii=False)
    return (
        f"id: {event.sequence}\nevent: {event.event}\ndata: {body}\n\n"
    ).encode("utf-8")


def _store(runtime: Any, cfg: Any):
    store = getattr(runtime, "app_sync", None)
    if store is None:
        store = create_appsync_store(cfg)
        runtime.app_sync = store
    return store


def register_first_class_chat_routes(
    app: FastAPI,
    cfg: Any,
    runtime: Any,
    metrics: Any,
    admin: Any,
) -> ChatTurnService:
    """Register canonical chat routes before legacy AppSync route collections."""

    store = _store(runtime, cfg)
    service = ChatTurnService(
        cfg=cfg,
        runtime=runtime,
        store=store,
        metrics=metrics,
    )
    app.state.chat_turn_service = service

    @app.post("/app/conversations/{conversation_id}/ask", name="app_chat_turn")
    async def app_chat_turn(conversation_id: str, request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        return await service.complete(
            request=request,
            payload=payload,
            actor=_actor(decision),
            role=str(getattr(decision, "role", "operator")),
            conversation_id=conversation_id,
            default_title="Conversation chat",
        )

    @app.post("/api/chat", name="api_chat_turn")
    async def api_chat_turn(request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        return await service.complete(
            request=request,
            payload=payload,
            actor=_actor(decision),
            role=str(getattr(decision, "role", "operator")),
        )

    @app.post("/api/chat/stream", name="api_chat_stream")
    async def api_chat_stream(request: Request):
        decision = await admin(request, "operator")
        payload = await request.json()
        raw_last_event_id = (
            request.headers.get("last-event-id", "0").strip() or "0"
        )
        try:
            last_event_id = int(raw_last_event_id)
        except ValueError as exc:
            raise HTTPException(400, "last-event-id must be an integer") from exc
        if last_event_id < 0:
            raise HTTPException(400, "last-event-id cannot be negative")

        # A short-lived service picks up runtime router replacements used by tests,
        # hot configuration and failover. Its semaphore is deliberately shared with
        # the registered service so all active streams obey one process-wide limit.
        stream_service = ChatTurnService(
            cfg=cfg,
            runtime=runtime,
            store=store,
            metrics=metrics,
        )
        stream_service.stream_limit = service.stream_limit
        active_router = getattr(runtime, "model_router", None)
        if active_router is not None and not callable(
            getattr(active_router, "route_plan", None)
        ):
            stream_service.streaming_router = CompletionOnlyStreamingRouter(
                active_router
            )

        actor = _actor(decision)
        role = str(getattr(decision, "role", "operator"))
        # This runs before StreamingResponse is created. Missing content,
        # idempotency, resume state, conversation access and model-profile policy
        # therefore preserve normal 4xx/503 HTTP semantics.
        prepared = stream_service.prepare_stream(
            request=request,
            payload=payload,
            actor=actor,
            role=role,
            last_event_id=last_event_id,
        )

        async def events():
            queue: asyncio.Queue[ChatStreamEvent | BaseException | None] = (
                asyncio.Queue(maxsize=STREAM_QUEUE_SIZE)
            )

            async def produce() -> None:
                cancelled = False
                try:
                    async for event in stream_service.stream_prepared(
                        request=request,
                        prepared=prepared,
                        actor=actor,
                        last_event_id=last_event_id,
                    ):
                        await queue.put(event)
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except BaseException as exc:
                    # Provider failures must not be dropped when the queue is full;
                    # backpressure lets the consumer drain before the failure.
                    await queue.put(exc)
                finally:
                    # On normal completion/error, wait for queue capacity so the
                    # terminator cannot be lost. On client cancellation the consumer
                    # is leaving, so do not block the cancelled producer on a full
                    # queue that will never be drained.
                    if not cancelled:
                        await queue.put(None)

            producer = asyncio.create_task(
                produce(),
                name="omnidesk-chat-stream",
            )
            emitted_sequence = last_event_id
            try:
                while True:
                    if await request.is_disconnected():
                        producer.cancel()
                        break
                    try:
                        item = await asyncio.wait_for(
                            queue.get(),
                            timeout=HEARTBEAT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        yield b": heartbeat\n\n"
                        continue
                    if item is None:
                        break
                    if isinstance(item, BaseException):
                        failure_sequence = emitted_sequence + 1
                        if isinstance(item, HTTPException):
                            detail = (
                                item.detail
                                if isinstance(item.detail, dict)
                                else {"code": "chat_stream_rejected"}
                            )
                        else:
                            detail = {"code": "chat_stream_failed"}
                        yield _encode(
                            ChatStreamEvent(
                                failure_sequence,
                                "chat.failed",
                                detail,
                            )
                        )
                        break
                    emitted_sequence = max(emitted_sequence, item.sequence)
                    yield _encode(item)
            finally:
                if not producer.done():
                    producer.cancel()
                with suppress(asyncio.CancelledError):
                    await producer

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return service
