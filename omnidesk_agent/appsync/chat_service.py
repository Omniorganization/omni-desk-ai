from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from fastapi import HTTPException, Request

from omnidesk_agent.appsync.conversation_context import ConversationContextBuilder
from omnidesk_agent.appsync.store import AppSyncStore, IdempotencyConflict
from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.router_streaming import GovernedStreamingRouter

logger = logging.getLogger(__name__)
STREAM_CHUNK_CHARACTERS = 256
INCOMPLETE_STREAM_STATES = {"in_progress", "interrupted", "failed"}


@dataclass(frozen=True)
class ChatStreamEvent:
    sequence: int
    event: str
    data: dict[str, Any]


@dataclass
class PreparedChatTurn:
    conversation_id: str
    idempotency_key: str | None
    cached: dict[str, Any] | None
    user_message: dict[str, Any] | None = None
    model_request: ModelRequest | None = None
    idempotency_payload: dict[str, Any] | None = None
    stream_events: list[dict[str, Any]] = field(default_factory=list)


class ChatTurnService:
    """Own conversation creation, policy, model execution and durable stream state."""

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
        header = request.headers.get("idempotency-key") or request.headers.get(
            "x-idempotency-key"
        )
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
        value = str(payload.get("content") or payload.get("message") or "").strip()
        if not value:
            raise HTTPException(422, "content is required")
        return value

    @staticmethod
    def _idempotency_payload(
        payload: dict[str, Any],
        conversation_id: str,
    ) -> dict[str, Any]:
        # Streaming is a transport preference and must not alter the logical write
        # fingerprint used by pre-native-streaming releases or fallback clients.
        canonical = {key: value for key, value in payload.items() if key != "stream"}
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
                title=str(idem_payload["title"]),
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

    def _cached_response(
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
        if not isinstance(cached, dict):
            raise HTTPException(409, {"code": "invalid_cached_chat_response"})
        return dict(cached)

    def _model_metadata(
        self,
        *,
        actor: str,
        role: str,
        conversation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        conversation = next(
            (
                item
                for item in self.store.list_conversations(actor=actor)
                if str(item.get("conversation_id") or "") == conversation_id
            ),
            None,
        )
        if conversation is None:
            # Unauthorized conversations are intentionally indistinguishable from
            # missing conversations at this boundary.
            raise HTTPException(404, "conversation not found")
        metadata: dict[str, Any] = {
            "actor": actor,
            "role": role,
            "organization_id": conversation.get("organization_id"),
            "conversation_id": conversation_id,
            "source_device_id": payload.get("source_device_id"),
        }
        model_profile = str(
            payload.get("model_profile") or payload.get("profile") or ""
        ).strip()
        if model_profile:
            metadata["profile"] = model_profile

        router = getattr(self.runtime, "model_router", None)
        route_plan = getattr(router, "route_plan", None)
        if callable(route_plan):
            try:
                route_plan("chat", metadata)
            except PermissionError as exc:
                raise HTTPException(
                    403,
                    {"code": "model_profile_denied"},
                ) from exc
        return metadata

    def _prepare_turn(
        self,
        *,
        actor: str,
        conversation_id: str,
        payload: dict[str, Any],
        metadata: dict[str, Any],
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
            task_id=f"chat-{conversation_id}-{user_message['message_id']}",
            metadata=metadata,
        )
        return user_message, model_request, idem_payload

    def _stream_state_response(
        self,
        prepared: PreparedChatTurn,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "conversation_id": prepared.conversation_id,
            "user_message": prepared.user_message,
            "assistant_message": None,
            "usage": {},
            "audit_trace_id": None,
            "stream_status": status,
            "stream_events": list(prepared.stream_events),
        }
        if result:
            response.update(
                {
                    key: value
                    for key, value in result.items()
                    if key != "ok"
                }
            )
            response["stream_status"] = status
            response["stream_events"] = list(prepared.stream_events)
        return response

    def _persist_stream_state(
        self,
        *,
        actor: str,
        prepared: PreparedChatTurn,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        if not prepared.idempotency_key or prepared.idempotency_payload is None:
            return
        self.store.put_idempotency_response(
            actor=actor,
            endpoint="conversations.ask",
            key=prepared.idempotency_key,
            payload=prepared.idempotency_payload,
            response=self._stream_state_response(
                prepared,
                status=status,
                result=result,
            ),
        )

    def _append_stream_event(
        self,
        *,
        actor: str,
        prepared: PreparedChatTurn,
        event: ChatStreamEvent,
        status: str = "in_progress",
        result: dict[str, Any] | None = None,
    ) -> None:
        prepared.stream_events.append(
            {
                "sequence": event.sequence,
                "event": event.event,
                "data": dict(event.data),
            }
        )
        self._persist_stream_state(
            actor=actor,
            prepared=prepared,
            status=status,
            result=result,
        )

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
        """Validate policy and reserve resumable state before HTTP 200 starts."""
        if not isinstance(payload, dict):
            raise HTTPException(422, "JSON object body is required")
        self.content(payload)
        idempotency_key = self.require_idempotency(request, payload)
        supplied_conversation_id = str(payload.get("conversation_id") or "").strip()
        if last_event_id > 0 and not supplied_conversation_id:
            raise HTTPException(
                409,
                {"code": "stream_resume_requires_conversation_id"},
            )
        conversation_id = self.ensure_conversation(
            actor=actor,
            payload=payload,
            idempotency_key=idempotency_key,
            default_title=default_title,
        )
        cached = self._cached_response(
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
                idempotency_payload=self._idempotency_payload(
                    payload,
                    conversation_id,
                ),
                stream_events=list(cached.get("stream_events") or []),
            )
        if last_event_id > 0:
            raise HTTPException(
                409,
                {"code": "stream_resume_state_missing"},
            )
        if self.streaming_router is None:
            raise HTTPException(503, "model router is not configured")

        metadata = self._model_metadata(
            actor=actor,
            role=role,
            conversation_id=conversation_id,
            payload=payload,
        )
        user_message, model_request, idem_payload = self._prepare_turn(
            actor=actor,
            conversation_id=conversation_id,
            payload=payload,
            metadata=metadata,
        )
        prepared = PreparedChatTurn(
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            cached=None,
            user_message=user_message,
            model_request=model_request,
            idempotency_payload=idem_payload,
        )
        # Reserve the logical write before the model call. A retry with the same
        # key can now replay state instead of creating another message/model call.
        self._persist_stream_state(
            actor=actor,
            prepared=prepared,
            status="in_progress",
        )
        return prepared

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
        persist_idempotency: bool = True,
        stream_events: list[dict[str, Any]] | None = None,
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
        if persist_idempotency:
            stored_result = dict(result)
            if stream_events is not None:
                stored_result["stream_status"] = "completed"
                stored_result["stream_events"] = list(stream_events)
            self.store.put_idempotency_response(
                actor=actor,
                endpoint="conversations.ask",
                key=idempotency_key,
                payload=idempotency_payload,
                response=stored_result,
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
        cached = self._cached_response(
            actor=actor,
            conversation_id=conversation_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        if cached is not None:
            if str(cached.get("stream_status") or "") in INCOMPLETE_STREAM_STATES:
                raise HTTPException(
                    409,
                    {
                        "code": "chat_stream_not_completed",
                        "stream_status": cached.get("stream_status"),
                    },
                )
            return {"ok": True, **cached}

        metadata = self._model_metadata(
            actor=actor,
            role=role,
            conversation_id=conversation_id,
            payload=payload,
        )
        user_message, model_request, idem_payload = self._prepare_turn(
            actor=actor,
            conversation_id=conversation_id,
            payload=payload,
            metadata=metadata,
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
        started = ChatStreamEvent(
            sequence,
            "chat.started",
            {"conversation_id": prepared.conversation_id},
        )
        self._append_stream_event(
            actor=actor,
            prepared=prepared,
            event=started,
        )
        if sequence > last_event_id:
            yield started
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
                        self._append_stream_event(
                            actor=actor,
                            prepared=prepared,
                            event=event,
                        )
                        if sequence > last_event_id:
                            yield event
                        sequence += 1
                    if delta.reasoning:
                        reasoning_parts.append(delta.reasoning)
                        event = ChatStreamEvent(
                            sequence,
                            "chat.reasoning.delta",
                            {
                                "text": delta.reasoning,
                                "native": delta.native,
                            },
                        )
                        self._append_stream_event(
                            actor=actor,
                            prepared=prepared,
                            event=event,
                        )
                        if sequence > last_event_id:
                            yield event
                        sequence += 1
                    if delta.usage:
                        usage.update(delta.usage)
                    if delta.finish_reason:
                        finish_reason = delta.finish_reason
        except asyncio.CancelledError:
            trace_id = self._trace_id(request)
            interrupted = ChatStreamEvent(
                sequence,
                "chat.failed",
                {
                    "code": "stream_interrupted",
                    "trace_id": trace_id,
                },
            )
            self._append_stream_event(
                actor=actor,
                prepared=prepared,
                event=interrupted,
                status="interrupted",
            )
            logger.info(
                "chat stream interrupted",
                extra={
                    "conversation_id": prepared.conversation_id,
                    "actor": actor,
                    "trace_id": trace_id,
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
                    "conversation_id": prepared.conversation_id,
                    "error_type": type(exc).__name__,
                },
            )
            failed = ChatStreamEvent(
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
                event=failed,
                status="failed",
            )
            if sequence > last_event_id:
                yield failed
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
            conversation_id=prepared.conversation_id,
            user_message=user_message,
            response=response,
            idempotency_key=prepared.idempotency_key,
            idempotency_payload=idem_payload,
            persist_idempotency=False,
        )

        usage_event = ChatStreamEvent(sequence, "chat.usage", usage)
        self._append_stream_event(
            actor=actor,
            prepared=prepared,
            event=usage_event,
        )
        if sequence > last_event_id:
            yield usage_event
        sequence += 1

        completed = ChatStreamEvent(
            sequence,
            "chat.completed",
            {
                "conversation_id": prepared.conversation_id,
                "audit_trace_id": persisted.get("audit_trace_id"),
                "provider_request_id": provider_request_id,
                "finish_reason": finish_reason,
                "native": native,
            },
        )
        self._append_stream_event(
            actor=actor,
            prepared=prepared,
            event=completed,
            status="completed",
            result=persisted,
        )
        if sequence > last_event_id:
            yield completed

    async def _replay_cached(
        self,
        cached: dict[str, Any],
        last_event_id: int,
    ) -> AsyncIterator[ChatStreamEvent]:
        raw_events = cached.get("stream_events")
        if isinstance(raw_events, list):
            highest_sequence = 0
            has_failure = False
            for raw in raw_events:
                if not isinstance(raw, dict):
                    continue
                try:
                    sequence = int(raw.get("sequence") or 0)
                except (TypeError, ValueError):
                    continue
                event_name = str(raw.get("event") or "")
                data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
                if sequence <= 0 or not event_name:
                    continue
                highest_sequence = max(highest_sequence, sequence)
                has_failure = has_failure or event_name == "chat.failed"
                if sequence > last_event_id:
                    yield ChatStreamEvent(sequence, event_name, dict(data))
            status = str(cached.get("stream_status") or "")
            if status in INCOMPLETE_STREAM_STATES and not has_failure:
                sequence = highest_sequence + 1
                if sequence > last_event_id:
                    yield ChatStreamEvent(
                        sequence,
                        "chat.failed",
                        {"code": f"stream_{status}"},
                    )
            return

        # Backward-compatible replay for responses produced before exact stream
        # event boundaries were persisted.
        conversation_id = str(cached.get("conversation_id") or "")
        sequence = 1
        if sequence > last_event_id:
            yield ChatStreamEvent(
                sequence,
                "chat.started",
                {"conversation_id": conversation_id, "replay": True},
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
                        "text": text[offset : offset + STREAM_CHUNK_CHARACTERS],
                        "replay": True,
                    },
                )
            sequence += 1
        usage = cached.get("usage") if isinstance(cached.get("usage"), dict) else {}
        if sequence > last_event_id:
            yield ChatStreamEvent(sequence, "chat.usage", usage)
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
