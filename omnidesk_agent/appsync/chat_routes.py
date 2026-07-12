from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from omnidesk_agent.appsync.chat_service import ChatStreamEvent, ChatTurnService
from omnidesk_agent.appsync.factory import create_appsync_store

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}
HEARTBEAT_SECONDS = 15.0


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

        # Bind each request to the current Runtime router. This supports controlled
        # profile reloads and test/staging router replacement without retaining a
        # stale provider graph from application construction time. Store identity
        # and the outer API resource-guard lease remain shared.
        stream_service = ChatTurnService(
            cfg=cfg,
            runtime=runtime,
            store=store,
            metrics=metrics,
        )

        async def events():
            queue: asyncio.Queue[ChatStreamEvent | BaseException | None] = (
                asyncio.Queue(maxsize=64)
            )

            def signal_done() -> None:
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(None)

            async def signal_error(exc: BaseException) -> None:
                try:
                    await asyncio.wait_for(queue.put(exc), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

            async def produce() -> None:
                try:
                    async for event in stream_service.stream(
                        request=request,
                        payload=payload,
                        actor=_actor(decision),
                        role=str(getattr(decision, "role", "operator")),
                        last_event_id=last_event_id,
                    ):
                        await queue.put(event)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    await signal_error(exc)
                finally:
                    signal_done()

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
                producer.cancel()
                with suppress(asyncio.CancelledError):
                    await producer

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return service
