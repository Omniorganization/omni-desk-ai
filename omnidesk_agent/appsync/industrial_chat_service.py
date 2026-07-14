from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import HTTPException, Request

from omnidesk_agent.appsync.chat_repository import (
    ChatRequestInProgress,
    ChatReservation,
    ChatResumeStateMissing,
    PostgresChatRepository,
    canonical_chat_payload,
)
from omnidesk_agent.appsync.chat_service import ChatStreamEvent, ChatTurnService, PreparedChatTurn
from omnidesk_agent.appsync.conversation_context import ConversationContextBuilder
from omnidesk_agent.appsync.store import IdempotencyConflict
from omnidesk_agent.models.base import ModelRequest, ModelResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AtomicPreparedChatTurn(PreparedChatTurn):
    reservation: ChatReservation | None = None


class IndustrialChatTurnService(ChatTurnService):
    """Use atomic PostgreSQL chat requests while preserving local-dev behavior."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.atomic_repository = (
            PostgresChatRepository(
                self.store,
                lease_seconds=int(getattr(getattr(self.cfg, "app_sync", None), "chat_request_lease_seconds", 180)),
            )
            if getattr(self.store, "dsn", None)
            else None
        )
        self.context_builder = ConversationContextBuilder()
        self._active: ContextVar[ChatReservation | None] = ContextVar("omnidesk_chat_reservation", default=None)

    def _preflight(self, *, actor: str, role: str, payload: dict[str, Any], conversation_id: str | None) -> None:
        if self.streaming_router is None:
            raise HTTPException(503, "model router is not configured")
        repository = self.atomic_repository
        if repository is None:
            return
        organization_id = (
            str(repository.get_conversation(actor, conversation_id)["organization_id"])
            if conversation_id
            else repository.organization_for_actor(actor)
        )
        metadata: dict[str, Any] = {
            "actor": actor,
            "role": role,
            "organization_id": organization_id,
            "conversation_id": conversation_id,
            "source_device_id": payload.get("source_device_id"),
        }
        profile = str(payload.get("model_profile") or payload.get("profile") or "").strip()
        if profile:
            metadata["profile"] = profile
        route_plan = getattr(self.streaming_router.router, "route_plan", None)
        if callable(route_plan):
            try:
                route_plan("chat", metadata)
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc

    def _reserve(
        self,
        *,
        request: Request,
        payload: dict[str, Any],
        actor: str,
        role: str,
        conversation_id: str | None,
        default_title: str,
        last_event_id: int,
    ) -> tuple[ChatReservation, str]:
        repository = self.atomic_repository
        if repository is None:
            raise RuntimeError("atomic chat repository is unavailable")
        if not isinstance(payload, dict):
            raise HTTPException(422, "JSON object body is required")
        content = self.content(payload)
        key = self.require_idempotency(request, payload)
        if not key:
            raise HTTPException(428, "idempotency-key is required for this write operation")
        conversation_id = conversation_id or str(payload.get("conversation_id") or "").strip() or None
        self._preflight(actor=actor, role=role, payload=payload, conversation_id=conversation_id)
        try:
            reservation = repository.reserve(
                actor=actor,
                endpoint="conversations.ask",
                idempotency_key=key,
                payload=canonical_chat_payload(payload, conversation_id),
                conversation_id=conversation_id,
                title=str(payload.get("title") or default_title),
                source_device_id=payload.get("source_device_id"),
                content=content,
                last_event_id=last_event_id,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ChatRequestInProgress as exc:
            raise HTTPException(409, {"code": "stream_in_progress", "message": str(exc)}) from exc
        except ChatResumeStateMissing as exc:
            raise HTTPException(409, {"code": "stream_resume_state_missing", "message": str(exc)}) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return reservation, content

    def _model_request(self, reservation: ChatReservation, *, role: str, payload: dict[str, Any], content: str) -> ModelRequest:
        assert self.atomic_repository is not None
        history = self.atomic_repository.list_messages(reservation.actor, reservation.conversation_id)
        current_id = str(reservation.user_message.get("message_id") or "")
        context = self.context_builder.build(history, current_message_id=current_id)
        metadata: dict[str, Any] = {
            "actor": reservation.actor,
            "role": role,
            "organization_id": reservation.organization_id,
            "conversation_id": reservation.conversation_id,
            "source_device_id": payload.get("source_device_id"),
        }
        profile = str(payload.get("model_profile") or payload.get("profile") or "").strip()
        if profile:
            metadata["profile"] = profile
        return ModelRequest(
            system="You are OmniDesk AI inside the enterprise Gateway. Keep security and approval boundaries explicit.\n\n" + context,
            user=content,
            task="chat",
            task_id=f"chat-{reservation.conversation_id}-{current_id}",
            metadata=metadata,
        )

    def prepare_stream(self, *, request: Request, payload: dict[str, Any], actor: str, role: str, last_event_id: int = 0, default_title: str = "API streaming chat") -> PreparedChatTurn:
        if self.atomic_repository is None:
            return super().prepare_stream(request=request, payload=payload, actor=actor, role=role, last_event_id=last_event_id, default_title=default_title)
        reservation, content = self._reserve(
            request=request, payload=payload, actor=actor, role=role,
            conversation_id=None, default_title=default_title, last_event_id=last_event_id,
        )
        cached = None
        if reservation.terminal:
            cached = dict(reservation.response)
            cached["stream_status"] = reservation.status
            cached["stream_events"] = list(reservation.events)
        return AtomicPreparedChatTurn(
            conversation_id=reservation.conversation_id,
            idempotency_key=reservation.idempotency_key,
            cached=cached,
            user_message=None if cached else reservation.user_message,
            model_request=None if cached else self._model_request(reservation, role=role, payload=payload, content=content),
            idempotency_payload=canonical_chat_payload(payload, reservation.conversation_id),
            reservation=reservation,
        )

    async def complete(self, *, request: Request, payload: dict[str, Any], actor: str, role: str, conversation_id: str | None = None, default_title: str = "API chat") -> dict[str, Any]:
        if self.atomic_repository is None:
            return await super().complete(request=request, payload=payload, actor=actor, role=role, conversation_id=conversation_id, default_title=default_title)
        reservation, content = self._reserve(
            request=request, payload=payload, actor=actor, role=role,
            conversation_id=conversation_id, default_title=default_title, last_event_id=0,
        )
        if reservation.terminal:
            if reservation.status == "completed":
                return {"ok": True, **reservation.response}
            raise HTTPException(502, reservation.response.get("error") or {"code": "previous_chat_request_failed"})
        complete = getattr(getattr(self.runtime, "model_router", None), "complete", None)
        if not callable(complete):
            self.atomic_repository.fail(reservation, {"code": "model_router_not_configured"})
            raise HTTPException(503, "model router is not configured")
        try:
            response = await complete(self._model_request(reservation, role=role, payload=payload, content=content))
        except Exception as exc:
            trace_id = self._trace_id(request)
            error = {"code": "model_provider_unavailable", "trace_id": trace_id}
            self.atomic_repository.fail(reservation, error)
            logger.exception("model router failed", extra={"trace_id": trace_id, "conversation_id": reservation.conversation_id})
            raise HTTPException(502, error) from exc
        result = self.atomic_repository.complete(reservation, response)
        if self.metrics:
            self.metrics.inc("omnidesk_app_chat_ask_total")
        return {"ok": True, **result}

    def _append_stream_event(self, *, actor: str, prepared: PreparedChatTurn, event: ChatStreamEvent, status: str = "streaming") -> None:
        reservation = getattr(prepared, "reservation", None)
        if self.atomic_repository is None or not isinstance(reservation, ChatReservation):
            return super()._append_stream_event(actor=actor, prepared=prepared, event=event, status=status)
        mapped = {"streaming": "running", "completed": "completed", "failed": "failed", "interrupted": "interrupted"}.get(status, status)
        if event.event == "chat.usage" and mapped == "running":
            mapped = "finalizing"
        self.atomic_repository.append_event(reservation, sequence=event.sequence, event=event.event, data=event.data, status=mapped)

    def _persist_result(self, *, actor: str, conversation_id: str, user_message: dict[str, Any], response: ModelResponse, idempotency_key: str | None, idempotency_payload: dict[str, Any]) -> dict[str, Any]:
        if self.atomic_repository is None:
            return super()._persist_result(actor=actor, conversation_id=conversation_id, user_message=user_message, response=response, idempotency_key=idempotency_key, idempotency_payload=idempotency_payload)
        reservation = self._active.get()
        if reservation is None:
            raise RuntimeError("atomic stream reservation is missing")
        return {"ok": True, **self.atomic_repository.complete(reservation, response, status="finalizing")}

    async def stream_prepared(self, *, request: Request, prepared: PreparedChatTurn, actor: str, last_event_id: int = 0) -> AsyncIterator[ChatStreamEvent]:
        reservation = getattr(prepared, "reservation", None)
        token = self._active.set(reservation) if isinstance(reservation, ChatReservation) else None
        try:
            async for event in super().stream_prepared(request=request, prepared=prepared, actor=actor, last_event_id=last_event_id):
                yield event
        finally:
            if token is not None:
                self._active.reset(token)
