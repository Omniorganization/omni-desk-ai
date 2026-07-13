from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import HTTPException, Request

from omnidesk_agent.appsync.conversation_context import ConversationContextBuilder
from omnidesk_agent.appsync.store import AppSyncStore, IdempotencyConflict
from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.router_streaming import GovernedStreamingRouter

logger = logging.getLogger(__name__)
STREAM_CHUNK_CHARACTERS = 256


@dataclass(frozen=True)
class ChatStreamEvent:
    sequence: int
    event: str
    data: dict[str, Any]


@dataclass(frozen=True)
class PreparedChatTurn:
    conversation_id: str
    idempotency_key: str | None
    cached: dict[str, Any] | None
    user_message: dict[str, Any] | None = None
    model_request: ModelRequest | None = None
    idempotency_payload: dict[str, Any] | None = None


class ChatTurnService:
    """Own conversation creation, idempotency, model execution and persistence."""

    def __init__(
        self,
        *,
        cfg: Any,
        runtime: Any,
        store: AppSyncStore,
        metrics: Any = None,
    ):
        self.cfg = cfg
        self.runtime = runtime
        self.store = store
        self.metrics = metrics
        self.context_builder = ConversationContextBuilder()
        router = getattr(runtime, "model_router", None)
        self.streaming_router = (
            GovernedStreamingRouter(router) if router is not None else None
        )
        self.stream_limit = asyncio.Semaphore(
            int(
                getattr(
                    getattr(cfg, "api_resource_guard", None),
                    "max_inflight_chat_requests",
                    8,
                )
            )
        )

    def require_idempotency(
        self,
        request: Request,
        payload: dict[str, Any],
    ) -> str | None:
        header = request.headers.get(
            "idempotency-key"
        ) or request.headers.get("x-idempotency-key")
        body_value = payload.get("idempotency_key")
        key = str(header or body_value or "").strip()[:180] or None
        app_sync = getattr(self.cfg, "app_sync", None)
        if getattr(app_sync, "require_idempotency", False) and not key:
            raise HTTPException(
                428,
                "idempotency-key is required for this write operation",
            )
        return key

    @staticmethod
    def content(payload: dict[str, Any]) -> str:
        value = str(
            payload.get("content") or payload.get("message") or ""
        ).strip()
        if not value:
            raise HTTPException(422, "content is required")
        return value

    @staticmethod
    def _idempotency_payload(
        payload: dict[str, Any],
        conversation_id: str,
    ) -> dict[str, Any]:
        # `stream` is a transport preference, not part of the logical chat write.
        # Excluding it preserves fingerprints written before native streaming and
        # allows a stream request to fall back to the non-stream endpoint with the
        # same key before any visible delta is delivered.
        canonical = {
            key: value
            for key, value in payload.items()
            if key != "stream"
        }
        canonical["conversation_id"] = conversation_id
        return canonical

    def ensure_conversation(
        self,
        *,
        actor: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
        default_title: str,
    ) -> str:
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if conversation_id:
            return conversation_id
        idem_payload = {
            "title": payload.get("title") or default_title,
            "source_device_id": payload.get("source_device_id"),
            "content": payload.get("content"),
            "message": payload.get("message"),
            "model_profile": payload.get("model_profile"),
            "profile": payload.get("profile"),
        }
        try:
            conversation = self.store.create_conversation(
                actor=actor,
                title=idem_payload["title"],
                source_device_id=idem_payload["source_device_id"],
                idempotency_key=(
                    f"chat:{actor}:{idempotency_key}:conversation"
                    if idempotency_key
                    else None
                ),
                idempotency_payload=idem_payload,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return str(conversation["conversation_id"])

    def _cached(
        self,
        *,
        actor: str,
        conversation_id: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
        idem_payload = self._idempotency_payload(payload, conversation_id)
        try:
            cached = self.store.get_idempotency_response(
                actor=actor,
                endpoint="conversations.ask",
                key=idempotency_key,
                payload=idem_payload,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if cached is None:
            return None
        if cached.get("stream_status") in {"reserved", "streaming"}:
            raise HTTPException(
                409,
                {
                    "code": "stream_in_progress",
                    "message": "The original stream is still in progress",
                },
            )
        return {"ok": True, **cached}

    def _prepare_turn(
        self,
        *,
        actor: str,
        role: str,
        conversation_id: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], ModelRequest, dict[str, Any]]:
        content = self.content(payload)
        idem_payload = self._idempotency_payload(payload, conversation_id)
        try:
            user_message = self.store.add_chat_user_message(
                actor=actor,
                conversation_id=conversation_id,
                content=content,
                source_device_id=payload.get("source_device_id"),
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

        metadata: dict[str, Any] = {
            "actor": actor,
            "role": role,
            "organization_id": user_message["organization_id"],
            "conversation_id": conversation_id,
            "source_device_id": payload.get("source_device_id"),
        }
        model_profile = str(
            payload.get("model_profile") or payload.get("profile") or ""
        ).strip()
        if model_profile:
            metadata["profile"] = model_profile
        history = self.store.list_messages(conversation_id, actor=actor)
        conversation_context = self.context_builder.build(
            history,
            current_message_id=user_message["message_id"],
        )
        model_request = ModelRequest(
            system=(
                "You are OmniDesk AI inside the enterprise Gateway. "
                "Answer the operator directly, keep security and approval "
                "boundaries explicit, and do not claim that desktop, mobile, "
                "push, signing, or external production evidence exists unless "
                "it was supplied in the request.\n\n"
                f"{conversation_context}"
            ),
            user=content,
            task="chat",
            task_id=(
                f"chat-{conversation_id}-{user_message['message_id']}"
            ),
            metadata=metadata,
        )
        return user_message, model_request, idem_payload

    def prepare_stream(
        self,
        *,
        request: Request,
        payload: dict[str, Any],
        actor: str,
        role: str,
        last_event_id: int = 0,
        default_title: str = "API streaming chat",
    ) -> PreparedChatTurn:
        """Validate and prepare a stream before the HTTP 200 response starts."""
        if not isinstance(payload, dict):
            raise HTTPException(422, "JSON object body is required")
        self.content(payload)
        idempotency_key = self.require_idempotency(request, payload)
        conversation_id = self.ensure_conversation(
            actor=actor,
            payload=payload,
            idempotency_key=idempotency_key,
            default_title=default_title,
        )
        cached = self._cached(
            actor=actor,
            conversation_id=conversation_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        if cached is not None:
            return PreparedChatTurn(
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                cached=cached,
            )
        if last_event_id > 0:
            raise HTTPException(
                409,
                {
                    "code": "stream_resume_state_missing",
                    "message": "No durable stream state exists for Last-Event-ID",
                },
            )
        if self.streaming_router is None:
            raise HTTPException(503, "model router is not configured")
        try:
            conversation = self.store.get_conversation(
                conversation_id,
                actor=actor,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        metadata: dict[str, Any] = {
            "actor": actor,
            "role": role,
            "organization_id": conversation["organization_id"],
            "conversation_id": conversation_id,
            "source_device_id": payload.get("source_device_id"),
        }
        model_profile = str(
            payload.get("model_profile") or payload.get("profile") or ""
        ).strip()
        if model_profile:
            metadata["profile"] = model_profile
        route_plan = getattr(self.streaming_router.router, "route_plan", None)
        if callable(route_plan):
            try:
                route_plan("chat", metadata)
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
        user_message, model_request, idem_payload = self._prepare_turn(
            actor=actor,
            role=role,
            conversation_id=conversation_id,
            payload=payload,
        )
        prepared = PreparedChatTurn(
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            cached=None,
            user_message=user_message,
            model_request=model_request,
            idempotency_payload=idem_payload,
        )
        self.store.put_idempotency_response(
            actor=actor,
            endpoint="conversations.ask",
            key=idempotency_key,
            payload=idem_payload,
            response={
                "conversation_id": conversation_id,
                "stream_status": "reserved",
                "stream_events": [],
            },
        )
        return prepared

    def _append_stream_event(
        self,
        *,
        actor: str,
        prepared: PreparedChatTurn,
        event: ChatStreamEvent,
        status: str = "streaming",
    ) -> None:
        payload = prepared.idempotency_payload or {}
        current = self.store.get_idempotency_response(
            actor=actor,
            endpoint="conversations.ask",
            key=prepared.idempotency_key,
            payload=payload,
        ) or {"conversation_id": prepared.conversation_id}
        events = list(current.get("stream_events") or [])
        if not any(int(item.get("sequence", -1)) == event.sequence for item in events):
            events.append(
                {
                    "sequence": event.sequence,
                    "event": event.event,
                    "data": event.data,
                }
            )
        current.update({"stream_status": status, "stream_events": events})
        self.store.put_idempotency_response(
            actor=actor,
            endpoint="conversations.ask",
            key=prepared.idempotency_key,
            payload=payload,
            response=current,
        )

    def _trace_id(self, request: Request) -> str:
        return str(
            getattr(request.state, "request_id", "")
            or request.headers.get("x-request-id")
            or "unavailable"
        )

    def _persist_result(
        self,
        *,
        actor: str,
        conversation_id: str,
        user_message: dict[str, Any],
        response: ModelResponse,
        idempotency_key: str | None,
        idempotency_payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            assistant_message = self.store.add_assistant_message(
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
        existing = self.store.get_idempotency_response(
            actor=actor,
            endpoint="conversations.ask",
            key=idempotency_key,
            payload=idempotency_payload,
        )
        if isinstance(existing, dict) and isinstance(existing.get("stream_events"), list):
            result["stream_events"] = existing["stream_events"]
            result["stream_status"] = existing.get("stream_status", "streaming")
        self.store.put_idempotency_response(
            actor=actor,
            endpoint="conversations.ask",
            key=idempotency_key,
            payload=idempotency_payload,
            response=result,
        )
        if self.metrics:
            self.metrics.inc("omnidesk_app_chat_ask_total")
        return {"ok": True, **result}

    async def complete(
        self,
        *,
        request: Request,
        payload: dict[str, Any],
        actor: str,
        role: str,
        conversation_id: str | None = None,
        default_title: str = "API chat",
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(422, "JSON object body is required")
        self.content(payload)
        idempotency_key = self.require_idempotency(request, payload)
        conversation_id = conversation_id or self.ensure_conversation(
            actor=actor,
            payload=payload,
            idempotency_key=idempotency_key,
            default_title=default_title,
        )
        cached = self._cached(
            actor=actor,
            conversation_id=conversation_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        if cached is not None:
            return cached
        user_message, model_request, idem_payload = self._prepare_turn(
            actor=actor,
            role=role,
            conversation_id=conversation_id,
            payload=payload,
        )
        router = getattr(self.runtime, "model_router", None)
        complete = getattr(router, "complete", None)
        if not callable(complete):
            raise HTTPException(503, "model router is not configured")
        try:
            response = await complete(model_request)
        except Exception as exc:
            if self.metrics:
                self.metrics.inc("omnidesk_app_chat_model_errors_total")
            trace_id = self._trace_id(request)
            logger.exception(
                "model router failed",
                extra={
                    "trace_id": trace_id,
                    "conversation_id": conversation_id,
                    "error_type": type(exc).__name__,
                },
            )
            raise HTTPException(
                502,
                {
                    "code": "model_provider_unavailable",
                    "trace_id": trace_id,
                },
            ) from exc
        return self._persist_result(
            actor=actor,
            conversation_id=conversation_id,
            user_message=user_message,
            response=response,
            idempotency_key=idempotency_key,
            idempotency_payload=idem_payload,
        )

    async def stream(
        self,
        *,
        request: Request,
        payload: dict[str, Any],
        actor: str,
        role: str,
        last_event_id: int = 0,
        default_title: str = "API streaming chat",
    ) -> AsyncIterator[ChatStreamEvent]:
        prepared = self.prepare_stream(
            request=request,
            payload=payload,
            actor=actor,
            role=role,
            last_event_id=last_event_id,
            default_title=default_title,
        )
        async for event in self.stream_prepared(
            request=request,
            prepared=prepared,
            actor=actor,
            last_event_id=last_event_id,
        ):
            yield event

    async def stream_prepared(
        self,
        *,
        request: Request,
        prepared: PreparedChatTurn,
        actor: str,
        last_event_id: int = 0,
    ) -> AsyncIterator[ChatStreamEvent]:
        conversation_id = prepared.conversation_id
        if prepared.cached is not None:
            async for event in self._replay_cached(
                prepared.cached,
                last_event_id,
            ):
                yield event
            return

        user_message = prepared.user_message
        model_request = prepared.model_request
        idem_payload = prepared.idempotency_payload
        if user_message is None or model_request is None or idem_payload is None:
            raise RuntimeError("prepared chat turn is incomplete")
        if self.streaming_router is None:
            raise HTTPException(503, "model router is not configured")

        sequence = 1
        if sequence > last_event_id:
            event = ChatStreamEvent(
                sequence,
                "chat.started",
                {"conversation_id": conversation_id},
            )
            self._append_stream_event(actor=actor, prepared=prepared, event=event)
            yield event
        sequence += 1
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, Any] = {}
        provider = "unknown"
        model = "unknown"
        profile = "unknown"
        native = True
        provider_request_id: str | None = None
        finish_reason = "stop"
        try:
            async with self.stream_limit:
                async for delta in self.streaming_router.stream(model_request):
                    if await request.is_disconnected():
                        raise asyncio.CancelledError
                    provider = delta.provider
                    model = delta.model
                    profile = delta.profile
                    native = native and delta.native
                    provider_request_id = (
                        delta.provider_request_id or provider_request_id
                    )
                    if delta.text:
                        text_parts.append(delta.text)
                        if sequence > last_event_id:
                            event = ChatStreamEvent(
                                sequence,
                                "chat.delta",
                                {
                                    "text": delta.text,
                                    "provider": provider,
                                    "model": model,
                                    "native": delta.native,
                                },
                            )
                            self._append_stream_event(actor=actor, prepared=prepared, event=event)
                            yield event
                        sequence += 1
                    if delta.reasoning:
                        reasoning_parts.append(delta.reasoning)
                        if sequence > last_event_id:
                            event = ChatStreamEvent(
                                sequence,
                                "chat.reasoning.delta",
                                {
                                    "text": delta.reasoning,
                                    "native": delta.native,
                                },
                            )
                            self._append_stream_event(actor=actor, prepared=prepared, event=event)
                            yield event
                        sequence += 1
                    if delta.usage:
                        usage.update(delta.usage)
                    if delta.finish_reason:
                        finish_reason = delta.finish_reason
        except asyncio.CancelledError:
            event = ChatStreamEvent(
                sequence,
                "chat.failed",
                {"code": "stream_interrupted"},
            )
            self._append_stream_event(
                actor=actor,
                prepared=prepared,
                event=event,
                status="interrupted",
            )
            logger.info(
                "chat stream cancelled",
                extra={
                    "conversation_id": conversation_id,
                    "actor": actor,
                },
            )
            raise
        except Exception as exc:
            if self.metrics:
                self.metrics.inc("omnidesk_app_chat_model_errors_total")
            trace_id = self._trace_id(request)
            logger.exception(
                "model router streaming failed",
                extra={
                    "trace_id": trace_id,
                    "conversation_id": conversation_id,
                    "error_type": type(exc).__name__,
                },
            )
            if sequence > last_event_id:
                event = ChatStreamEvent(
                    sequence,
                    "chat.failed",
                    {
                        "code": "model_provider_unavailable",
                        "trace_id": trace_id,
                    },
                )
                self._append_stream_event(
                    actor=actor,
                    prepared=prepared,
                    event=event,
                    status="failed",
                )
                yield event
            return

        response = ModelResponse(
            text="".join(text_parts),
            provider=provider,
            model=model,
            profile=profile,
            usage=usage,
            raw={
                "provider_request_id": provider_request_id,
                "finish_reason": finish_reason,
                "native_stream": native,
                "reasoning": "".join(reasoning_parts),
            },
        )
        persisted = self._persist_result(
            actor=actor,
            conversation_id=conversation_id,
            user_message=user_message,
            response=response,
            idempotency_key=prepared.idempotency_key,
            idempotency_payload=idem_payload,
        )
        if sequence > last_event_id:
            event = ChatStreamEvent(
                sequence,
                "chat.usage",
                usage,
            )
            self._append_stream_event(actor=actor, prepared=prepared, event=event)
            yield event
        sequence += 1
        if sequence > last_event_id:
            event = ChatStreamEvent(
                sequence,
                "chat.completed",
                {
                    "conversation_id": conversation_id,
                    "audit_trace_id": persisted.get("audit_trace_id"),
                    "provider_request_id": provider_request_id,
                    "finish_reason": finish_reason,
                    "native": native,
                },
            )
            self._append_stream_event(
                actor=actor,
                prepared=prepared,
                event=event,
                status="completed",
            )
            yield event

    async def _replay_cached(
        self,
        cached: dict[str, Any],
        last_event_id: int,
    ) -> AsyncIterator[ChatStreamEvent]:
        durable_events = cached.get("stream_events")
        if isinstance(durable_events, list):
            for item in durable_events:
                if not isinstance(item, dict):
                    continue
                sequence = int(item.get("sequence") or 0)
                if sequence <= last_event_id:
                    continue
                data = item.get("data")
                yield ChatStreamEvent(
                    sequence,
                    str(item.get("event") or "message"),
                    data if isinstance(data, dict) else {},
                )
            return
        conversation_id = str(cached.get("conversation_id") or "")
        sequence = 1
        if sequence > last_event_id:
            yield ChatStreamEvent(
                sequence,
                "chat.started",
                {
                    "conversation_id": conversation_id,
                    "replay": True,
                },
            )
        sequence += 1
        message = (
            cached.get("assistant_message")
            if isinstance(cached.get("assistant_message"), dict)
            else {}
        )
        text = str(message.get("content") or "")
        for offset in range(0, len(text), STREAM_CHUNK_CHARACTERS):
            if sequence > last_event_id:
                yield ChatStreamEvent(
                    sequence,
                    "chat.delta",
                    {
                        "text": text[
                            offset : offset + STREAM_CHUNK_CHARACTERS
                        ],
                        "replay": True,
                    },
                )
            sequence += 1
        usage = (
            cached.get("usage")
            if isinstance(cached.get("usage"), dict)
            else {}
        )
        if sequence > last_event_id:
            yield ChatStreamEvent(
                sequence,
                "chat.usage",
                usage,
            )
        sequence += 1
        if sequence > last_event_id:
            yield ChatStreamEvent(
                sequence,
                "chat.completed",
                {
                    "conversation_id": conversation_id,
                    "audit_trace_id": cached.get("audit_trace_id"),
                    "replay": True,
                },
            )
