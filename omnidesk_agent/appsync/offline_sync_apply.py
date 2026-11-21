"""Applying uploaded offline operations, with conflict policies that are enforced.

A client uploads records it created while offline: conversations, messages and
tasks with client-chosen ids. Approvals and notifications are server-authored
and are never accepted from an upload (approval decisions need an owner and a
signed device, so uploading them would let an operator token forge approved
work), and desktop-runtime tasks must go through the approval flow.

An upload that names a record the server already has, with different content,
is a conflict. The caller's ``conflict_policy`` decides it:

``server-wins``
    The server's record stays; the conflict is recorded as resolved.
``client-wins``
    The uploaded value replaces the server's, where that is safe: conversation
    titles, and titles of tasks that are still queued without an approval.
    Messages are append-only history and approved or running tasks are never
    rewritten, so those conflicts stay open for review instead.
``merge``
    Needs the value the client last saw (``base_<field>``, for example
    ``base_title``). If only one side changed since then, that side wins;
    if both did, or no base was sent, the conflict stays open.
``manual-review``
    Nothing is applied; the conflict stays open until the actor resolves it
    with ``keep_server``, ``apply_client`` or ``merged``.

Re-uploading an operation the server already received is a duplicate and is not
applied again, so a retry can never turn into a conflict. The same rules run on
the JSON store and, as statements in the receiving transaction, on PostgreSQL.
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Optional, Protocol

from omnidesk_agent.appsync.store import (
    AppSyncStore,
    ConversationRecord,
    MessageRecord,
    SyncConflictRecord,
    TaskRecord,
    _fingerprint,
    _id,
    _now,
)

ConflictPolicy = Literal["server-wins", "client-wins", "manual-review", "merge"]
Resolution = Literal["keep_server", "apply_client", "merged"]
CONFLICT_POLICIES = frozenset({"server-wins", "client-wins", "manual-review", "merge"})
RESOLUTIONS = frozenset({"keep_server", "apply_client", "merged"})

_KEY_FIELDS = {"conversation": "conversation_id", "message": "message_id", "task": "task_id"}

# Outcomes that changed server state.
MUTATING_OUTCOMES = frozenset({"applied", "client_applied", "merged"})


class OfflineRecords(Protocol):
    """What the conflict rules need from a backend, for one actor."""

    actor: str
    # Errors that are not about the operation (a lock or statement timeout, a
    # deadlock, a lost connection). They abort the whole upload, so it is retried
    # or sent again; counted as a rejected operation, it would be lost.
    aborting_errors: tuple[type[BaseException], ...]

    def organization(self) -> str: ...

    def normalize_org(self, organization_id: Optional[str]) -> str: ...

    def get(self, kind: str, record_id: str, *, for_update: bool = False) -> Any: ...

    def insert(self, kind: str, record: Any) -> bool: ...

    def update(self, kind: str, record: Any) -> None: ...

    def event(self, event_type: str, payload: dict[str, Any]) -> None: ...

    def save_conflict(self, conflict: SyncConflictRecord, *, opened: bool) -> None: ...

    def guarded(self, action: Callable[[], str]) -> str: ...


class ApplyingAppSyncMixin:
    """JSON AppSync contract for applying uploaded offline operations."""

    def receive_outbox_operations(
        self,
        *,
        actor: str,
        operations: list[dict[str, Any]],
        remote: str = "default",
        device_id: Optional[str] = None,
        conflict_policy: ConflictPolicy = "manual-review",
    ) -> dict[str, Any]:
        store: AppSyncStore = self  # type: ignore[assignment]
        with store._lock:
            organization_id = store._organization_for_actor(actor)
            received_before = {
                str(operation.get("operation_id") or operation.get("id") or "")
                for operation in operations
                if isinstance(operation, dict)
                and AppSyncStore._inbox_key(organization_id, actor, remote_event_id(operation, remote)) in store.local_inbox
            }
            result = super().receive_outbox_operations(  # type: ignore[misc]
                actor=actor,
                operations=operations,
                remote=remote,
                device_id=device_id,
                conflict_policy=conflict_policy,
            )
            application = apply_uploaded_operations(
                store, actor=actor, operations=operations, remote=remote, device_id=device_id,
                conflict_policy=conflict_policy, duplicates=received_before, held=held_operations(result),
            )
            return merge_receive_results(result, application)


class ApplyingAppSyncStore(ApplyingAppSyncMixin, AppSyncStore):
    """JSON AppSync store that applies uploaded offline operations."""


def remote_event_id(event: dict[str, Any], remote: str) -> str:
    """The inbox id of an uploaded operation (``AppSyncStore.record_remote_events``)."""
    seq = int(event.get("remote_seq") or event.get("seq") or 0)
    event_type = event.get("operation_type") or event.get("event_type") or "operation.replay"
    return str(event.get("event_id") or event.get("operation_id") or event.get("id") or f"{remote}:{seq}:{event_type}")


def held_operations(recorded: dict[str, Any]) -> dict[str, str]:
    """Operation id -> conflict id for uploads that recording put on hold."""
    return {
        str(conflict.get("remote_event_id")): str(conflict.get("conflict_id"))
        for conflict in recorded.get("conflicts") or []
        if isinstance(conflict, dict) and conflict.get("remote_event_id")
    }


def merge_receive_results(recorded: dict[str, Any], application: dict[str, Any]) -> dict[str, Any]:
    """Inbox counts from recording the upload, plus what applying it did."""
    return {
        **recorded,
        **{key: value for key, value in application.items() if key != "conflicts"},
        "conflicts": list(recorded.get("conflicts") or []) + list(application.get("conflicts") or []),
    }


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


def apply_operations(
    records: OfflineRecords,
    *,
    operations: list[dict[str, Any]],
    remote: str,
    device_id: Optional[str],
    conflict_policy: str,
    duplicates: set[str] | frozenset[str] = frozenset(),
    held: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """``held`` maps operation ids that recording the upload already put on
    hold (an idempotency key reused with another payload) to their conflict."""
    policy = conflict_policy if conflict_policy in CONFLICT_POLICIES else "manual-review"
    held = held or {}
    results: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            results.append({"operation_id": None, "status": "rejected", "reason": "not_an_object"})
            continue
        operation_id = str(operation.get("operation_id") or operation.get("id") or "") or None
        event_type, payload = _operation_payload(operation)
        entry: dict[str, Any] = {"operation_id": operation_id, "event_type": event_type}
        if operation_id and operation_id in duplicates:
            results.append({**entry, "status": "duplicate"})
            continue
        if operation_id and operation_id in held:
            results.append({**entry, "status": "conflict", "conflict_id": held[operation_id]})
            continue
        handler = _HANDLERS.get(event_type)
        if handler is None:
            results.append({**entry, "status": "rejected", "reason": "not_uploadable"})
            continue
        opened: list[dict[str, Any]] = []
        try:
            status = records.guarded(lambda: handler(records, payload, device_id, policy, operation_id, opened))
        except records.aborting_errors:
            raise
        except Exception:
            results.append({**entry, "status": "rejected", "reason": "invalid_operation"})
            continue
        if status == "applied":
            event_payload = {**payload, "organization_id": records.organization()}
            event_payload.setdefault("remote", remote)
            records.event(event_type, event_payload)
        conflicts.extend(opened)
        results.append({**entry, "status": status, **({"conflict_id": opened[0]["conflict_id"]} if opened else {})})
    applied = sum(1 for item in results if item["status"] in MUTATING_OUTCOMES)
    return {
        "applied_state_mutations": applied,
        "skipped_state_mutations": len(results) - applied,
        "operations": results,
        "conflicts": conflicts,
    }


# --------------------------------------------------------------- handlers ---


def _conflict(
    records: OfflineRecords,
    *,
    kind: str,
    record_id: str,
    field: str,
    server_value: Any,
    client_value: Any,
    base_value: Any,
    policy: str,
    payload: dict[str, Any],
    operation_id: Optional[str],
    overwrite: Optional[Callable[[Any], None]],
    opened: list[dict[str, Any]],
) -> str:
    """Decide one diverged field by policy; ``overwrite`` is None where the server value must stay."""
    conflict = SyncConflictRecord(
        conflict_id=_id("conflict"),
        operation_id=operation_id or _id("op"),
        remote_event_id=operation_id,
        conflict_type=f"{kind}.{field}_diverged",
        strategy=policy,  # type: ignore[arg-type]
        actor=records.actor,
        organization_id=records.organization(),
        local_payload={"record": kind, "id": record_id, "field": field, "value": server_value},
        remote_payload=dict(payload),
        created_at=_now(),
    )

    def resolve(outcome: str) -> str:
        conflict.status = "resolved"
        conflict.resolved_at = _now()
        records.save_conflict(conflict, opened=False)
        return outcome

    if policy == "server-wins":
        return resolve("server_kept")
    if policy == "client-wins" and overwrite is not None:
        overwrite(client_value)
        return resolve("client_applied")
    if policy == "merge" and base_value is not None:
        if server_value == base_value and overwrite is not None:
            overwrite(client_value)
            return resolve("merged")
        if client_value == base_value:
            return resolve("server_kept")
    records.save_conflict(conflict, opened=True)
    opened.append(_record_dict(conflict))
    return "conflict"


def _record_dict(record: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(record)


def _conversation(records: OfflineRecords, payload: dict[str, Any], device_id: Optional[str], policy: str, operation_id: Optional[str], opened: list[dict[str, Any]]) -> str:
    conversation_id = str(payload.get("conversation_id") or payload.get("id") or "").strip() or _id("conv")
    title = str(payload.get("title") or "Offline conversation")
    created_at = float(payload.get("created_at") or _now())
    updated_at = float(payload.get("updated_at") or _now())
    existing = records.get("conversation", conversation_id, for_update=True)
    if existing is None:
        record = ConversationRecord(
            conversation_id=conversation_id, title=title, actor=records.actor, organization_id=records.organization(),
            source_device_id=str(payload.get("source_device_id") or device_id or "") or None,
            created_at=created_at, updated_at=updated_at,
        )
        return "applied" if records.insert("conversation", record) else "unchanged"
    if existing.actor != records.actor:
        return "rejected"
    if existing.title == title:
        return "unchanged"

    def overwrite(value: Any) -> None:
        existing.title = str(value)
        existing.updated_at = _now()
        records.update("conversation", existing)
        records.event("conversation.updated", {"conversation_id": conversation_id, "title": existing.title, "organization_id": existing.organization_id})

    return _conflict(
        records, kind="conversation", record_id=conversation_id, field="title", server_value=existing.title, client_value=title,
        base_value=payload.get("base_title"), policy=policy, payload=payload, operation_id=operation_id, overwrite=overwrite, opened=opened,
    )


def _message(records: OfflineRecords, payload: dict[str, Any], device_id: Optional[str], policy: str, operation_id: Optional[str], opened: list[dict[str, Any]]) -> str:
    message_id = str(payload.get("message_id") or payload.get("id") or "").strip() or _id("msg")
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id:
        return "rejected"
    role = str(payload.get("role") or "user")
    if role not in {"user", "assistant"}:
        # System prompts are server-authored; uploaded ones are treated as user text.
        role = "user"
    content = str(payload.get("content") or "")
    created_at = float(payload.get("created_at") or _now())
    existing = records.get("message", message_id, for_update=True)
    if existing is not None:
        if existing.actor != records.actor:
            return "rejected"
        if existing.content == content and existing.role == role:
            return "unchanged"
        # Messages are append-only history: no policy rewrites one silently.
        return _conflict(
            records, kind="message", record_id=message_id, field="content", server_value=existing.content, client_value=content,
            base_value=None, policy="server-wins" if policy == "server-wins" else "manual-review", payload=payload,
            operation_id=operation_id, overwrite=None, opened=opened,
        )
    conversation = records.get("conversation", conversation_id, for_update=True)
    if conversation is None:
        _conversation(records, {"conversation_id": conversation_id, "title": payload.get("conversation_title") or "Offline conversation", "source_device_id": payload.get("source_device_id") or device_id}, device_id, "server-wins", None, [])
        conversation = records.get("conversation", conversation_id, for_update=True)
    if conversation is None or conversation.actor != records.actor:
        # Never let one actor append to another actor's conversation (and LLM context).
        return "rejected"
    message = MessageRecord(
        message_id=message_id, conversation_id=conversation_id, role=role, content=content,  # type: ignore[arg-type]
        actor=records.actor, organization_id=records.normalize_org(conversation.organization_id),
        source_device_id=str(payload.get("source_device_id") or device_id or "") or None, task_id=payload.get("task_id"),
        model_provider=payload.get("model_provider"), model_name=payload.get("model_name"), model_profile=payload.get("model_profile"),
        usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else {}, trace_id=payload.get("trace_id"),
        created_at=created_at,
    )
    if not records.insert("message", message):
        return "unchanged"
    conversation.updated_at = _now()
    records.update("conversation", conversation)
    return "applied"


def _task_is_rewritable(task: TaskRecord) -> bool:
    """Only a queued task with no approval and no desktop work may change by upload."""
    return task.status == "queued" and not task.approval_id and not task.requires_desktop_runtime


def _task(records: OfflineRecords, payload: dict[str, Any], device_id: Optional[str], policy: str, operation_id: Optional[str], opened: list[dict[str, Any]]) -> str:
    task_id = str(payload.get("task_id") or payload.get("id") or "").strip() or _id("task")
    if payload.get("requires_desktop_runtime") or payload.get("approval_id") or payload.get("assigned_runtime_device_id"):
        # Desktop-runtime work must go through the approval flow, never offline upload.
        return "rejected"
    title = str(payload.get("title") or "Offline task")
    created_at = float(payload.get("created_at") or _now())
    updated_at = float(payload.get("updated_at") or _now())
    existing = records.get("task", task_id, for_update=True)
    if existing is not None:
        if existing.actor != records.actor:
            return "rejected"
        if existing.title == title:
            return "unchanged"

        def overwrite(value: Any) -> None:
            existing.title = str(value)
            existing.updated_at = _now()
            records.update("task", existing)
            records.event("task.updated", {"task_id": task_id, "organization_id": existing.organization_id})

        return _conflict(
            records, kind="task", record_id=task_id, field="title", server_value=existing.title, client_value=title,
            base_value=payload.get("base_title"), policy=policy, payload=payload, operation_id=operation_id,
            overwrite=overwrite if _task_is_rewritable(existing) else None, opened=opened,
        )
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id:
        conversation_id = _id("conv")
        _conversation(records, {"conversation_id": conversation_id, "title": payload.get("title") or "Offline task"}, device_id, "server-wins", None, [])
    else:
        conversation = records.get("conversation", conversation_id)
        if conversation is not None and conversation.actor != records.actor:
            return "rejected"
    task = TaskRecord(
        task_id=task_id, conversation_id=conversation_id, title=title, actor=records.actor, organization_id=records.organization(),
        status="queued", assigned_runtime_device_id=None, requires_desktop_runtime=False, approval_id=None,
        result_summary=payload.get("result_summary"), idempotency_key=payload.get("idempotency_key") or _fingerprint(payload),
        created_at=created_at, updated_at=updated_at,
    )
    return "applied" if records.insert("task", task) else "unchanged"


_HANDLERS: dict[str, Callable[[OfflineRecords, dict[str, Any], Optional[str], str, Optional[str], list[dict[str, Any]]], str]] = {
    "conversation.created": _conversation,
    "message.created": _message,
    "chat.message.created": _message,
    "task.created": _task,
}
UPLOADABLE_EVENT_TYPES = frozenset(_HANDLERS)


# ------------------------------------------------------------- resolution ---


def resolve_record_conflict(records: OfflineRecords, conflict: SyncConflictRecord, *, resolution: str, payload: Optional[dict[str, Any]]) -> None:
    """Apply an actor's decision on a diverged-record conflict (not yet saved as resolved)."""
    if resolution not in RESOLUTIONS:
        raise ValueError("resolution must be keep_server, apply_client or merged")
    if resolution == "keep_server":
        return
    target = conflict.local_payload if isinstance(conflict.local_payload, dict) else {}
    kind, record_id, field = str(target.get("record") or ""), str(target.get("id") or ""), str(target.get("field") or "")
    source = conflict.remote_payload if resolution == "apply_client" else (payload or {})
    if field not in source:
        raise ValueError(f"the resolution needs a value for {field!r}")
    value = str(source.get(field) or "")
    record = records.get(kind, record_id, for_update=True)
    if record is None or record.actor != records.actor:
        raise KeyError("conflicted record not found")
    if kind == "task" and not _task_is_rewritable(record):
        raise PermissionError("an approved, claimed or desktop task cannot be changed by a conflict resolution")
    if kind not in {"conversation", "message", "task"} or not hasattr(record, field):
        raise ValueError("this conflict cannot be resolved by changing the record")
    setattr(record, field, value)
    if hasattr(record, "updated_at"):
        record.updated_at = _now()
    records.update(kind, record)
    event_payload: dict[str, Any] = {_KEY_FIELDS[kind]: record_id, "organization_id": record.organization_id}
    if kind == "message":
        event_payload["conversation_id"] = record.conversation_id
    records.event(f"{kind}.updated", event_payload)


# ------------------------------------------------------------------- JSON ---


class _JsonRecords:
    aborting_errors: tuple[type[BaseException], ...] = ()

    def __init__(self, store: AppSyncStore, actor: str) -> None:
        self.store = store
        self.actor = actor or "unknown"
        self._tables: dict[str, dict[str, Any]] = {"conversation": store.conversations, "message": store.messages, "task": store.tasks}

    def organization(self) -> str:
        return self.store._normalize_org_id(self.store._organization_for_actor(self.actor))

    def normalize_org(self, organization_id: Optional[str]) -> str:
        return self.store._normalize_org_id(organization_id)

    def get(self, kind: str, record_id: str, *, for_update: bool = False) -> Any:
        return self._tables[kind].get(record_id)

    def insert(self, kind: str, record: Any) -> bool:
        key = str(getattr(record, _KEY_FIELDS[kind]))
        if key in self._tables[kind]:
            return False
        self._tables[kind][key] = record
        return True

    def update(self, kind: str, record: Any) -> None:
        """Records are mutated in place."""

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        # Not _event: an applied upload must not be queued back into this node's outbox.
        store = self.store
        store._seq += 1
        organization_id = store._normalize_org_id(payload.get("organization_id") or self.organization())
        store.events.append({"seq": store._seq, "ts": _now(), "event_type": event_type, "actor": self.actor, "organization_id": organization_id, "payload": {**payload, "organization_id": organization_id}})
        store.events = store.events[-1000:]

    def save_conflict(self, conflict: SyncConflictRecord, *, opened: bool) -> None:
        self.store.sync_conflicts[conflict.conflict_id] = conflict
        self.store._append_operation_log_locked(
            "sync.conflict.opened" if opened else "sync.conflict.resolved",
            {"conflict_id": conflict.conflict_id, "operation_id": conflict.operation_id, "strategy": conflict.strategy, "organization_id": conflict.organization_id},
        )

    def guarded(self, action: Callable[[], str]) -> str:
        return action()


def apply_uploaded_operations(
    store: AppSyncStore,
    *,
    actor: str,
    operations: list[dict[str, Any]],
    remote: str = "default",
    device_id: str | None = None,
    conflict_policy: str = "manual-review",
    duplicates: set[str] | frozenset[str] = frozenset(),
    held: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    with store._lock:
        result = apply_operations(
            _JsonRecords(store, actor), operations=operations, remote=remote, device_id=device_id,
            conflict_policy=conflict_policy, duplicates=duplicates, held=held,
        )
        if result["operations"]:
            store._persist()
    return result


# ------------------------------------------------------------- PostgreSQL ---


class _SqlRecords:
    """The same rules as statements in the receiving transaction."""

    def __init__(self, store: Any, tx: Any, actor: str) -> None:
        from psycopg import OperationalError  # type: ignore

        from omnidesk_agent.appsync.migrated_postgres_store import CONVERSATIONS, MESSAGES, SYNC_CONFLICTS, TASKS

        self.aborting_errors: tuple[type[BaseException], ...] = (OperationalError,)
        self.store, self.tx, self.actor = store, tx, actor or "unknown"
        self._tables = {"conversation": CONVERSATIONS, "message": MESSAGES, "task": TASKS}
        self._conflicts = SYNC_CONFLICTS

    def organization(self) -> str:
        return str(self.store._org_for_actor(self.tx, self.actor))

    def normalize_org(self, organization_id: Optional[str]) -> str:
        return str(self.store._normalize_org_id(organization_id))

    def get(self, kind: str, record_id: str, *, for_update: bool = False) -> Any:
        return self.store._get(self.tx, self._tables[kind], record_id, for_update=for_update)

    def insert(self, kind: str, record: Any) -> bool:
        return bool(self.store._insert(self.tx, self._tables[kind], record, if_absent=True))

    def update(self, kind: str, record: Any) -> None:
        self.store._update(self.tx, self._tables[kind], record)

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.store._event(self.tx, event_type, self.actor, payload, queue_locally=False)

    def save_conflict(self, conflict: SyncConflictRecord, *, opened: bool) -> None:
        self.store._upsert(self.tx, self._conflicts, conflict)
        self.store._operation_log(
            self.tx,
            "sync.conflict.opened" if opened else "sync.conflict.resolved",
            {"conflict_id": conflict.conflict_id, "operation_id": conflict.operation_id, "strategy": conflict.strategy, "organization_id": conflict.organization_id},
        )

    def guarded(self, action: Callable[[], str]) -> str:
        # One rejected operation must not abort the others in this transaction.
        self.tx.cur.execute("SAVEPOINT omnidesk_apply_operation")
        try:
            outcome = action()
        except Exception:
            self.tx.cur.execute("ROLLBACK TO SAVEPOINT omnidesk_apply_operation")
            raise
        self.tx.cur.execute("RELEASE SAVEPOINT omnidesk_apply_operation")
        return outcome


def apply_uploaded_operations_sql(
    store: Any,
    tx: Any,
    *,
    actor: str,
    operations: list[dict[str, Any]],
    remote: str = "default",
    device_id: str | None = None,
    conflict_policy: str = "manual-review",
    duplicates: set[str] | frozenset[str] = frozenset(),
    held: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    return apply_operations(
        _SqlRecords(store, tx, actor), operations=operations, remote=remote, device_id=device_id,
        conflict_policy=conflict_policy, duplicates=duplicates, held=held,
    )


def json_records(store: AppSyncStore, actor: str) -> OfflineRecords:
    return _JsonRecords(store, actor)


def sql_records(store: Any, tx: Any, actor: str) -> OfflineRecords:
    return _SqlRecords(store, tx, actor)
