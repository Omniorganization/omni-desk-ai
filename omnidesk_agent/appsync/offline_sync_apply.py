from __future__ import annotations

from typing import Any, Callable, Literal, Optional

from omnidesk_agent.appsync.store import (
    AppSyncStore,
    ApprovalRecord,
    ConversationRecord,
    MessageRecord,
    NotificationRecord,
    TaskRecord,
    _fingerprint,
    _id,
    _now,
)

ConflictPolicy = Literal["server-wins", "client-wins", "manual-review", "merge"]


class ApplyingAppSyncMixin:
    """Formal AppSync contract for applying uploaded offline operations.

    The previous implementation used a global monkey patch to mutate
    ``AppSyncStore.receive_outbox_operations``. This mixin keeps the behavior
    explicit on the store instance returned by the factory, which makes backend
    selection, test coverage, and multi-instance reasoning deterministic.
    """

    def receive_outbox_operations(
        self,
        *,
        actor: str,
        operations: list[dict[str, Any]],
        remote: str = "default",
        device_id: Optional[str] = None,
        conflict_policy: ConflictPolicy = "manual-review",
    ) -> dict[str, Any]:
        result = super().receive_outbox_operations(  # type: ignore[misc]
            actor=actor,
            operations=operations,
            remote=remote,
            device_id=device_id,
            conflict_policy=conflict_policy,
        )
        application = apply_uploaded_operations(self, actor=actor, operations=operations, remote=remote, device_id=device_id)  # type: ignore[arg-type]
        return {**result, **application}


class ApplyingAppSyncStore(ApplyingAppSyncMixin, AppSyncStore):
    """JSON AppSync store that applies uploaded offline operations."""



def apply_uploaded_operations(
    store: AppSyncStore,
    *,
    actor: str,
    operations: list[dict[str, Any]],
    remote: str = "default",
    device_id: str | None = None,
) -> dict[str, Any]:
    applied = 0
    skipped = 0
    with store._lock:
        for operation in operations:
            if not isinstance(operation, dict):
                skipped += 1
                continue
            event_type, payload = _operation_payload(operation)
            if not event_type:
                skipped += 1
                continue
            try:
                changed = _apply_one(store, actor=actor, event_type=event_type, payload=payload, device_id=device_id)
            except Exception:
                skipped += 1
                continue
            if changed:
                applied += 1
                _append_sync_event(store, actor=actor, event_type=event_type, payload=payload, remote=remote)
            else:
                skipped += 1
        if applied:
            store._persist()
    return {"applied_state_mutations": applied, "skipped_state_mutations": skipped}


def _operation_payload(operation: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    raw_payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    nested_payload = raw_payload.get("payload") if isinstance(raw_payload.get("payload"), dict) else raw_payload
    event_type = str(
        operation.get("operation_type")
        or operation.get("event_type")
        or raw_payload.get("event_type")
        or nested_payload.get("event_type")
        or ""
    ).strip()
    return event_type, dict(nested_payload or {})


def _apply_one(store: AppSyncStore, *, actor: str, event_type: str, payload: dict[str, Any], device_id: str | None = None) -> bool:
    handlers: dict[str, Callable[[AppSyncStore, str, dict[str, Any], str | None], bool]] = {
        "conversation.created": _apply_conversation_created,
        "message.created": _apply_message_created,
        "chat.message.created": _apply_message_created,
        "task.created": _apply_task_created,
        "approval.created": _apply_approval_created,
        "notification.created": _apply_notification_created,
    }
    handler = handlers.get(event_type)
    return False if handler is None else handler(store, actor, payload, device_id)


def _append_sync_event(store: AppSyncStore, *, actor: str, event_type: str, payload: dict[str, Any], remote: str) -> None:
    store._seq += 1
    organization_id = store._normalize_org_id(payload.get("organization_id") or store._organization_for_actor(actor))
    payload = dict(payload or {})
    payload.setdefault("organization_id", organization_id)
    payload.setdefault("remote", remote)
    store.events.append(
        {
            "seq": store._seq,
            "ts": _now(),
            "event_type": event_type,
            "actor": actor or payload.get("actor") or "unknown",
            "organization_id": organization_id,
            "payload": payload,
        }
    )
    store.events = store.events[-1000:]


def _apply_conversation_created(store: AppSyncStore, actor: str, payload: dict[str, Any], device_id: str | None) -> bool:
    conversation_id = str(payload.get("conversation_id") or payload.get("id") or "").strip() or _id("conv")
    if conversation_id in store.conversations:
        return False
    organization_id = store._normalize_org_id(payload.get("organization_id") or store._organization_for_actor(actor))
    store.conversations[conversation_id] = ConversationRecord(
        conversation_id=conversation_id,
        title=str(payload.get("title") or "Offline conversation"),
        actor=str(payload.get("actor") or actor or "unknown"),
        organization_id=organization_id,
        source_device_id=str(payload.get("source_device_id") or device_id or "") or None,
        created_at=float(payload.get("created_at") or _now()),
        updated_at=float(payload.get("updated_at") or _now()),
    )
    return True


def _apply_message_created(store: AppSyncStore, actor: str, payload: dict[str, Any], device_id: str | None) -> bool:
    message_id = str(payload.get("message_id") or payload.get("id") or "").strip() or _id("msg")
    if message_id in store.messages:
        return False
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id:
        return False
    if conversation_id not in store.conversations:
        _apply_conversation_created(
            store,
            actor,
            {
                "conversation_id": conversation_id,
                "title": payload.get("conversation_title") or "Offline conversation",
                "organization_id": payload.get("organization_id"),
                "source_device_id": payload.get("source_device_id") or device_id,
            },
            device_id,
        )
    organization_id = store._normalize_org_id(payload.get("organization_id") or store.conversations[conversation_id].organization_id)
    role = str(payload.get("role") or "user")
    if role not in {"user", "assistant", "system"}:
        role = "user"
    store.messages[message_id] = MessageRecord(
        message_id=message_id,
        conversation_id=conversation_id,
        role=role,  # type: ignore[arg-type]
        content=str(payload.get("content") or ""),
        actor=str(payload.get("actor") or actor or "unknown"),
        organization_id=organization_id,
        source_device_id=str(payload.get("source_device_id") or device_id or "") or None,
        task_id=payload.get("task_id"),
        model_provider=payload.get("model_provider"),
        model_name=payload.get("model_name"),
        model_profile=payload.get("model_profile"),
        usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
        trace_id=payload.get("trace_id"),
        created_at=float(payload.get("created_at") or _now()),
    )
    if conversation_id in store.conversations:
        store.conversations[conversation_id].updated_at = _now()
    return True


def _apply_task_created(store: AppSyncStore, actor: str, payload: dict[str, Any], device_id: str | None) -> bool:
    task_id = str(payload.get("task_id") or payload.get("id") or "").strip() or _id("task")
    if task_id in store.tasks:
        return False
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id:
        conversation_id = _id("conv")
        _apply_conversation_created(store, actor, {"conversation_id": conversation_id, "title": payload.get("title") or "Offline task"}, device_id)
    organization_id = store._normalize_org_id(payload.get("organization_id") or store._organization_for_actor(actor))
    status = str(payload.get("status") or "queued")
    if status not in {"queued", "running", "blocked", "completed", "failed", "cancelled"}:
        status = "queued"
    store.tasks[task_id] = TaskRecord(
        task_id=task_id,
        conversation_id=conversation_id,
        title=str(payload.get("title") or "Offline task"),
        actor=str(payload.get("actor") or actor or "unknown"),
        organization_id=organization_id,
        status=status,  # type: ignore[arg-type]
        assigned_runtime_device_id=payload.get("assigned_runtime_device_id"),
        requires_desktop_runtime=bool(payload.get("requires_desktop_runtime", False)),
        approval_id=payload.get("approval_id"),
        result_summary=payload.get("result_summary"),
        idempotency_key=payload.get("idempotency_key") or _fingerprint(payload),
        created_at=float(payload.get("created_at") or _now()),
        updated_at=float(payload.get("updated_at") or _now()),
    )
    return True


def _apply_approval_created(store: AppSyncStore, actor: str, payload: dict[str, Any], device_id: str | None) -> bool:
    approval_id = str(payload.get("approval_id") or payload.get("id") or "").strip() or _id("appr")
    if approval_id in store.approvals:
        return False
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        task_id = _id("task")
        _apply_task_created(store, actor, {"task_id": task_id, "title": payload.get("action") or "Offline approval"}, device_id)
    risk = str(payload.get("risk") or "medium")
    if risk not in {"low", "medium", "high", "critical"}:
        risk = "medium"
    status = str(payload.get("status") or "pending")
    if status not in {"pending", "approved", "rejected", "expired"}:
        status = "pending"
    organization_id = store._normalize_org_id(payload.get("organization_id") or store._organization_for_actor(actor))
    store.approvals[approval_id] = ApprovalRecord(
        approval_id=approval_id,
        task_id=task_id,
        risk=risk,  # type: ignore[arg-type]
        action=str(payload.get("action") or "offline_operation"),
        reason=str(payload.get("reason") or "Uploaded offline approval operation"),
        requested_by=str(payload.get("requested_by") or actor or "unknown"),
        organization_id=organization_id,
        status=status,  # type: ignore[arg-type]
        decided_by=payload.get("decided_by"),
        decision_reason=payload.get("decision_reason"),
        decision_idempotency_key=payload.get("decision_idempotency_key"),
        created_at=float(payload.get("created_at") or _now()),
        decided_at=payload.get("decided_at"),
        expires_at=payload.get("expires_at"),
    )
    return True


def _apply_notification_created(store: AppSyncStore, actor: str, payload: dict[str, Any], device_id: str | None) -> bool:
    notification_id = str(payload.get("notification_id") or payload.get("id") or "").strip() or _id("notif")
    if notification_id in store.notifications:
        return False
    audience = str(payload.get("audience") or "all")
    if audience not in {"desktop", "mobile", "web_admin", "all"}:
        audience = "all"
    organization_id = store._normalize_org_id(payload.get("organization_id") or store._organization_for_actor(actor))
    store.notifications[notification_id] = NotificationRecord(
        notification_id=notification_id,
        audience=audience,  # type: ignore[arg-type]
        title=str(payload.get("title") or "Offline sync"),
        body=str(payload.get("body") or ""),
        event_type=str(payload.get("event_type") or "offline.sync"),
        actor=str(payload.get("actor") or actor or "unknown"),
        organization_id=organization_id,
        related_id=payload.get("related_id"),
        read=bool(payload.get("read", False)),
        created_at=float(payload.get("created_at") or _now()),
    )
    return True
