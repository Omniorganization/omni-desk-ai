from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from omnidesk_agent.appsync.store import IdempotencyConflict

ACTIVE = {"reserved", "running", "finalizing"}
TERMINAL = {"completed", "failed", "interrupted"}


class ChatRequestInProgress(RuntimeError):
    """Raised when another worker owns an unexpired chat request lease."""


class ChatResumeStateMissing(RuntimeError):
    """Raised when Last-Event-ID references a request that was never reserved."""


class ChatLeaseLost(RuntimeError):
    """Raised when a stale worker attempts to mutate a request it no longer owns."""


@dataclass(frozen=True)
class ChatReservation:
    namespace: str
    organization_id: str
    actor: str
    endpoint: str
    idempotency_key: str
    payload_hash: str
    conversation_id: str
    user_message: dict[str, Any]
    status: str
    lease_owner: str | None
    response: dict[str, Any]
    events: tuple[dict[str, Any], ...]

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


def canonical_chat_payload(
    payload: dict[str, Any], conversation_id: str | None
) -> dict[str, Any]:
    result = {key: value for key, value in payload.items() if key != "stream"}
    if conversation_id:
        result["conversation_id"] = conversation_id
    return result


def _hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _lock_key(scope: str) -> int:
    return int.from_bytes(
        hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True
    )


class PostgresChatRepository:
    """Direct transactional repository for the high-volume chat write path."""

    def __init__(self, store: Any, *, lease_seconds: int = 180) -> None:
        if not getattr(store, "dsn", None):
            raise TypeError(
                "PostgresChatRepository requires a PostgreSQL AppSync store"
            )
        self.store = store
        self.namespace = str(getattr(store, "namespace", "production"))
        self.lease_seconds = max(30, min(int(lease_seconds), 1800))

    def _connect(self) -> Any:
        return self.store._connect()

    def organization_for_actor(self, actor: str) -> str:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT organization_id FROM omnidesk_appsync_users "
                "WHERE namespace=%s AND user_id=%s",
                (self.namespace, actor),
            )
            row = cur.fetchone()
            return str(row[0] if row else "org_default")

    def get_conversation(
        self, actor: str, conversation_id: str
    ) -> dict[str, Any]:
        organization_id = self.organization_for_actor(actor)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT conversation_id,title,actor,organization_id,"
                "source_device_id,created_at,updated_at "
                "FROM omnidesk_appsync_conversations "
                "WHERE namespace=%s AND organization_id=%s "
                "AND conversation_id=%s",
                (self.namespace, organization_id, conversation_id),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError("conversation not found")
            return dict(
                zip(
                    (
                        "conversation_id",
                        "title",
                        "actor",
                        "organization_id",
                        "source_device_id",
                        "created_at",
                        "updated_at",
                    ),
                    row,
                )
            )

    def list_messages(
        self, actor: str, conversation_id: str
    ) -> list[dict[str, Any]]:
        organization_id = self.organization_for_actor(actor)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT message_id,conversation_id,role,content,actor,"
                "organization_id,source_device_id,task_id,model_provider,"
                "model_name,model_profile,usage,trace_id,created_at "
                "FROM omnidesk_appsync_messages "
                "WHERE namespace=%s AND organization_id=%s "
                "AND conversation_id=%s ORDER BY created_at,message_id",
                (self.namespace, organization_id, conversation_id),
            )
            keys = (
                "message_id",
                "conversation_id",
                "role",
                "content",
                "actor",
                "organization_id",
                "source_device_id",
                "task_id",
                "model_provider",
                "model_name",
                "model_profile",
                "usage",
                "trace_id",
                "created_at",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def reserve(
        self,
        *,
        actor: str,
        endpoint: str,
        idempotency_key: str,
        payload: dict[str, Any],
        conversation_id: str | None,
        title: str,
        source_device_id: str | None,
        content: str,
        last_event_id: int,
    ) -> ChatReservation:
        if not idempotency_key:
            raise ValueError("idempotency key is required")
        now = time.time()
        owner = _id("lease")
        with self._connect() as conn, conn.cursor() as cur:
            scope = f"{self.namespace}:{actor}:{endpoint}:{idempotency_key}"
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(scope),))
            cur.execute(
                "SELECT organization_id FROM omnidesk_appsync_users "
                "WHERE namespace=%s AND user_id=%s",
                (self.namespace, actor),
            )
            row = cur.fetchone()
            organization_id = str(row[0] if row else "org_default")
            if not row:
                cur.execute(
                    "INSERT INTO omnidesk_appsync_organizations"
                    "(organization_id,namespace,name,created_at) "
                    "VALUES(%s,%s,%s,%s) "
                    "ON CONFLICT(organization_id) DO NOTHING",
                    (
                        organization_id,
                        self.namespace,
                        "Default Organization",
                        now,
                    ),
                )
                cur.execute(
                    "INSERT INTO omnidesk_appsync_users"
                    "(namespace,organization_id,user_id,display_name,role,"
                    "created_at,updated_at) "
                    "VALUES(%s,%s,%s,%s,'operator',%s,%s) "
                    "ON CONFLICT(namespace,user_id) DO NOTHING",
                    (
                        self.namespace,
                        organization_id,
                        actor,
                        actor,
                        now,
                        now,
                    ),
                )
            digest = _hash(canonical_chat_payload(payload, conversation_id))
            cur.execute(
                "SELECT payload_hash,conversation_id,user_message_id,status,"
                "lease_owner,lease_expires_at,response,error "
                "FROM omnidesk_appsync_chat_requests "
                "WHERE namespace=%s AND organization_id=%s AND actor=%s "
                "AND endpoint=%s AND idempotency_key=%s FOR UPDATE",
                (
                    self.namespace,
                    organization_id,
                    actor,
                    endpoint,
                    idempotency_key,
                ),
            )
            existing = cur.fetchone()
            if existing:
                if str(existing[0]) != digest:
                    raise IdempotencyConflict(
                        "idempotency key was reused with a different payload"
                    )
                status = str(existing[3])
                if status in ACTIVE and float(existing[5] or 0) > now:
                    raise ChatRequestInProgress(
                        "The original chat request is still in progress"
                    )
                if status in ACTIVE:
                    self._interrupt_locked(
                        cur,
                        organization_id,
                        actor,
                        endpoint,
                        idempotency_key,
                        "stale_request_lease",
                    )
                conn.commit()
                return self._load(
                    organization_id, actor, endpoint, idempotency_key
                )
            if last_event_id > 0:
                raise ChatResumeStateMissing(
                    "No durable stream state exists for Last-Event-ID"
                )
            if conversation_id:
                cur.execute(
                    "SELECT organization_id "
                    "FROM omnidesk_appsync_conversations "
                    "WHERE namespace=%s AND conversation_id=%s FOR SHARE",
                    (self.namespace, conversation_id),
                )
                conversation = cur.fetchone()
                if not conversation:
                    raise KeyError("conversation not found")
                if str(conversation[0]) != organization_id:
                    raise PermissionError("organization scope mismatch")
            else:
                conversation_id = _id("conv")
                cur.execute(
                    "INSERT INTO omnidesk_appsync_conversations"
                    "(namespace,organization_id,conversation_id,title,actor,"
                    "source_device_id,created_at,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        self.namespace,
                        organization_id,
                        conversation_id,
                        title,
                        actor,
                        source_device_id,
                        now,
                        now,
                    ),
                )
            message_id = _id("msg")
            cur.execute(
                "INSERT INTO omnidesk_appsync_messages"
                "(namespace,organization_id,message_id,conversation_id,role,"
                "content,actor,source_device_id,created_at) "
                "VALUES(%s,%s,%s,%s,'user',%s,%s,%s,%s)",
                (
                    self.namespace,
                    organization_id,
                    message_id,
                    conversation_id,
                    content,
                    actor,
                    source_device_id,
                    now,
                ),
            )
            cur.execute(
                "INSERT INTO omnidesk_appsync_chat_requests"
                "(namespace,organization_id,actor,endpoint,idempotency_key,"
                "payload_hash,conversation_id,user_message_id,status,"
                "lease_owner,lease_expires_at,created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'reserved',%s,%s,%s,%s)",
                (
                    self.namespace,
                    organization_id,
                    actor,
                    endpoint,
                    idempotency_key,
                    digest,
                    conversation_id,
                    message_id,
                    owner,
                    now + self.lease_seconds,
                    now,
                    now,
                ),
            )
            conn.commit()
            return self._load(
                organization_id, actor, endpoint, idempotency_key
            )

    def _interrupt_locked(
        self,
        cur: Any,
        org: str,
        actor: str,
        endpoint: str,
        key: str,
        reason: str,
    ) -> None:
        from psycopg.types.json import Jsonb  # type: ignore

        cur.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 "
            "FROM omnidesk_appsync_chat_stream_events "
            "WHERE namespace=%s AND organization_id=%s AND actor=%s "
            "AND endpoint=%s AND idempotency_key=%s",
            (self.namespace, org, actor, endpoint, key),
        )
        sequence = int(cur.fetchone()[0])
        error = {"code": "stream_interrupted", "reason": reason}
        cur.execute(
            "INSERT INTO omnidesk_appsync_chat_stream_events"
            "(namespace,organization_id,actor,endpoint,idempotency_key,"
            "sequence,event_type,payload,created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,'chat.failed',%s,%s) "
            "ON CONFLICT DO NOTHING",
            (
                self.namespace,
                org,
                actor,
                endpoint,
                key,
                sequence,
                Jsonb(error),
                time.time(),
            ),
        )
        cur.execute(
            "UPDATE omnidesk_appsync_chat_requests "
            "SET status='interrupted',lease_owner=NULL,lease_expires_at=NULL,"
            "last_sequence=%s,error=%s,updated_at=%s "
            "WHERE namespace=%s AND organization_id=%s AND actor=%s "
            "AND endpoint=%s AND idempotency_key=%s",
            (
                sequence,
                Jsonb(error),
                time.time(),
                self.namespace,
                org,
                actor,
                endpoint,
                key,
            ),
        )

    def _load(
        self, org: str, actor: str, endpoint: str, key: str
    ) -> ChatReservation:
        with self._connect() as conn, conn.cursor() as cur:
            return self._load_locked(cur, org, actor, endpoint, key)

    def _load_locked(
        self, cur: Any, org: str, actor: str, endpoint: str, key: str
    ) -> ChatReservation:
        cur.execute(
            "SELECT payload_hash,conversation_id,user_message_id,status,"
            "lease_owner,response,error "
            "FROM omnidesk_appsync_chat_requests "
            "WHERE namespace=%s AND organization_id=%s AND actor=%s "
            "AND endpoint=%s AND idempotency_key=%s",
            (self.namespace, org, actor, endpoint, key),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("chat request reservation is missing")
        cur.execute(
            "SELECT message_id,conversation_id,role,content,actor,"
            "organization_id,source_device_id,created_at "
            "FROM omnidesk_appsync_messages "
            "WHERE namespace=%s AND message_id=%s",
            (self.namespace, row[2]),
        )
        message = cur.fetchone()
        if not message:
            raise RuntimeError("reserved user message is missing")
        user = dict(
            zip(
                (
                    "message_id",
                    "conversation_id",
                    "role",
                    "content",
                    "actor",
                    "organization_id",
                    "source_device_id",
                    "created_at",
                ),
                message,
            )
        )
        cur.execute(
            "SELECT sequence,event_type,payload "
            "FROM omnidesk_appsync_chat_stream_events "
            "WHERE namespace=%s AND organization_id=%s AND actor=%s "
            "AND endpoint=%s AND idempotency_key=%s ORDER BY sequence",
            (self.namespace, org, actor, endpoint, key),
        )
        events = tuple(
            {
                "sequence": int(item[0]),
                "event": item[1],
                "data": item[2] if isinstance(item[2], dict) else {},
            }
            for item in cur.fetchall()
        )
        response = row[5] if isinstance(row[5], dict) else {}
        if not response and isinstance(row[6], dict) and row[6]:
            response = {"error": row[6]}
        return ChatReservation(
            self.namespace,
            org,
            actor,
            endpoint,
            key,
            str(row[0]),
            str(row[1]),
            user,
            str(row[3]),
            row[4],
            response,
            events,
        )
