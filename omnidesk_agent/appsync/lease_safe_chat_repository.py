from __future__ import annotations

import time
import uuid
from typing import Any

from omnidesk_agent.appsync.chat_repository import (
    ChatRequestInProgress,
    ChatReservation,
    ChatResumeStateMissing,
    PostgresChatRepository as BasePostgresChatRepository,
    canonical_chat_payload,
)
from omnidesk_agent.models.base import ModelResponse

ACTIVE = {"reserved", "running", "finalizing"}
TERMINAL = {"completed", "failed", "interrupted"}


class ChatLeaseLost(RuntimeError):
    """A stale worker attempted to mutate a request it no longer owns."""


class PostgresChatRepository(BasePostgresChatRepository):
    """Lease-fenced extension of the direct transactional chat repository."""

    def _lock_owned_request(self, cur: Any, reservation: ChatReservation) -> str:
        cur.execute(
            "SELECT status,lease_owner FROM omnidesk_appsync_chat_requests "
            "WHERE namespace=%s AND organization_id=%s AND actor=%s "
            "AND endpoint=%s AND idempotency_key=%s FOR UPDATE",
            (
                reservation.namespace,
                reservation.organization_id,
                reservation.actor,
                reservation.endpoint,
                reservation.idempotency_key,
            ),
        )
        row = cur.fetchone()
        if not row:
            raise ChatLeaseLost("chat request no longer exists")
        if str(row[0]) not in ACTIVE or row[1] != reservation.lease_owner:
            raise ChatLeaseLost("chat request lease is no longer owned")
        return str(row[0])

    def append_event(
        self,
        reservation: ChatReservation,
        *,
        sequence: int,
        event: str,
        data: dict[str, Any],
        status: str,
    ) -> None:
        from psycopg.types.json import Jsonb  # type: ignore

        if status not in ACTIVE | TERMINAL:
            raise ValueError(f"invalid chat request status: {status}")
        now = time.time()
        with self._connect() as conn, conn.cursor() as cur:
            self._lock_owned_request(cur, reservation)
            cur.execute(
                "INSERT INTO omnidesk_appsync_chat_stream_events"
                "(namespace,organization_id,actor,endpoint,idempotency_key,"
                "sequence,event_type,payload,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (
                    reservation.namespace,
                    reservation.organization_id,
                    reservation.actor,
                    reservation.endpoint,
                    reservation.idempotency_key,
                    sequence,
                    event,
                    Jsonb(data),
                    now,
                ),
            )
            terminal = status in TERMINAL
            cur.execute(
                "UPDATE omnidesk_appsync_chat_requests "
                "SET status=%s,last_sequence=GREATEST(last_sequence,%s),"
                "lease_owner=%s,lease_expires_at=%s,updated_at=%s "
                "WHERE namespace=%s AND organization_id=%s AND actor=%s "
                "AND endpoint=%s AND idempotency_key=%s",
                (
                    status,
                    sequence,
                    None if terminal else reservation.lease_owner,
                    None if terminal else now + self.lease_seconds,
                    now,
                    reservation.namespace,
                    reservation.organization_id,
                    reservation.actor,
                    reservation.endpoint,
                    reservation.idempotency_key,
                ),
            )
            conn.commit()

    def complete(
        self,
        reservation: ChatReservation,
        response: ModelResponse,
        *,
        status: str = "completed",
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb  # type: ignore

        if status not in {"finalizing", "completed"}:
            raise ValueError("complete status must be finalizing or completed")
        now = time.time()
        message_id = f"msg_{uuid.uuid4().hex[:16]}"
        trace_id = f"trace_{uuid.uuid4().hex[:16]}"
        assistant = {
            "message_id": message_id,
            "conversation_id": reservation.conversation_id,
            "role": "assistant",
            "content": response.text,
            "actor": reservation.actor,
            "organization_id": reservation.organization_id,
            "model_provider": response.provider,
            "model_name": response.model,
            "model_profile": response.profile,
            "usage": response.usage or {},
            "trace_id": trace_id,
            "created_at": now,
        }
        result = {
            "conversation_id": reservation.conversation_id,
            "user_message": reservation.user_message,
            "assistant_message": assistant,
            "usage": response.usage or {},
            "audit_trace_id": trace_id,
        }
        with self._connect() as conn, conn.cursor() as cur:
            self._lock_owned_request(cur, reservation)
            cur.execute(
                "INSERT INTO omnidesk_appsync_messages"
                "(namespace,organization_id,message_id,conversation_id,role,"
                "content,actor,model_provider,model_name,model_profile,usage,"
                "trace_id,created_at) "
                "VALUES(%s,%s,%s,%s,'assistant',%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    reservation.namespace,
                    reservation.organization_id,
                    message_id,
                    reservation.conversation_id,
                    response.text,
                    reservation.actor,
                    response.provider,
                    response.model,
                    response.profile,
                    Jsonb(response.usage or {}),
                    trace_id,
                    now,
                ),
            )
            terminal = status == "completed"
            cur.execute(
                "UPDATE omnidesk_appsync_chat_requests "
                "SET status=%s,response=%s,lease_owner=%s,"
                "lease_expires_at=%s,updated_at=%s "
                "WHERE namespace=%s AND organization_id=%s AND actor=%s "
                "AND endpoint=%s AND idempotency_key=%s",
                (
                    status,
                    Jsonb(result),
                    None if terminal else reservation.lease_owner,
                    None if terminal else now + self.lease_seconds,
                    now,
                    reservation.namespace,
                    reservation.organization_id,
                    reservation.actor,
                    reservation.endpoint,
                    reservation.idempotency_key,
                ),
            )
            conn.commit()
        return result

    def fail(
        self,
        reservation: ChatReservation,
        error: dict[str, Any],
        *,
        status: str = "failed",
    ) -> None:
        from psycopg.types.json import Jsonb  # type: ignore

        if status not in {"failed", "interrupted"}:
            raise ValueError("failure status must be failed or interrupted")
        with self._connect() as conn, conn.cursor() as cur:
            self._lock_owned_request(cur, reservation)
            cur.execute(
                "UPDATE omnidesk_appsync_chat_requests "
                "SET status=%s,error=%s,lease_owner=NULL,"
                "lease_expires_at=NULL,updated_at=%s "
                "WHERE namespace=%s AND organization_id=%s AND actor=%s "
                "AND endpoint=%s AND idempotency_key=%s",
                (
                    status,
                    Jsonb(error),
                    time.time(),
                    reservation.namespace,
                    reservation.organization_id,
                    reservation.actor,
                    reservation.endpoint,
                    reservation.idempotency_key,
                ),
            )
            conn.commit()


__all__ = [
    "ChatLeaseLost",
    "ChatRequestInProgress",
    "ChatReservation",
    "ChatResumeStateMissing",
    "PostgresChatRepository",
    "canonical_chat_payload",
]
