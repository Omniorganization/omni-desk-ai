"""Data-subject export and erasure in the AppSync stores.

``privacy.data_subject.DataSubjectService`` calls ``export_subject(actor)`` and
``erase_subject(actor)`` on ``rt.app_sync``. Both AppSync stores (JSON and
PostgreSQL) implement them with the rules in this module, so they export and
erase the same records. Matching is by actor id. An export lists what an
erasure deletes or anonymises.

Erasure deletes:
- the person's user profile, conversations, messages and chat requests;
- their tasks. A desktop that still holds one gets "task not found" when it
  reports back, so nothing is written back;
- approvals they requested, or that gate their tasks;
- notifications about any of these records; pushes to their devices or about
  these records; their devices' runtime status;
- their offline-sync records (outbox, inbox, cursors, conflicts, and the
  operation-log entries of their operations), and anyone's offline-sync record
  that names one of the erased records by id, so no change to them leaves for
  the remote gateway; their idempotency replays.

It keeps these rows, without the person:
- Devices stay revoked, so an erased device can never verify, register or get
  a push again. Name and push token are removed; the actor becomes
  ``[erased]``.
- Device enrollments they started: requested_by becomes ``[erased]``. One not
  completed is revoked, so its pairing code can no longer enrol a device.
- Notifications they caused on someone else's records: the actor becomes
  ``[erased]``.
- Approvals they decided for someone else: decided_by and approved_by become
  ``[erased]``, and their reason is removed. On an approval still pending,
  their partial approval is withdrawn instead: an approver nobody can identify
  must not count toward the two that a critical action needs.
- Sync events keep their numbers, so readers' cursors stay valid. The actor
  becomes ``[erased]``. Events by the person or about an erased record keep
  only identifiers and states.

Not covered here: security's audit logs (kept by design, see
``privacy.data_subject``); the approval ledger, which erases its own rows;
projects (``GatewayProjectStore``), whose ``owner_actor`` is not erased.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional, cast

from omnidesk_agent.appsync.store import (
    ApprovalRecord,
    DeviceEnrollmentRecord,
    DeviceRecord,
    LocalInboxRecord,
    SyncConflictRecord,
    public_device_view,
)

# Written where a row must stay but must no longer name the person.
ERASED_ACTOR = "[erased]"

# What an event by the erased person, or about an erased record, keeps besides
# its identifiers (keys ending in _id or _ids).
_EVENT_STATE_KEYS = frozenset({
    "status", "risk", "decision", "organization_id", "operation_type", "credential_status", "trust_level",
    "attempt_count", "required_approvals", "remote_seq", "capability", "audience", "redacted",
})


def exported(kind: str, record: Any) -> dict[str, Any]:
    """One record of an export: its fields, tagged with its kind, never a credential."""
    data: dict[str, Any] = dict(cast(dict[str, Any], record)) if isinstance(record, dict) else asdict(record)
    if kind == "device":
        data = public_device_view(data)
    elif kind == "device_enrollment":
        data.pop("pairing_code_hash", None)
    return {"record": kind, **data}


def exported_decision(approval: ApprovalRecord, actor: str) -> dict[str, Any]:
    """The actor's decision on someone else's approval; the request itself is theirs."""
    return {
        "record": "approval_decision",
        "approval_id": approval.approval_id,
        "risk": approval.risk,
        "status": approval.status,
        "decided": approval.decided_by == actor,
        "approved": actor in (approval.approved_by or []),
        "decided_at": approval.decided_at,
    }


def exported_idempotency(row: dict[str, Any]) -> dict[str, Any]:
    """A stored replay: which endpoint and when. The response repeats records exported in full."""
    return {"record": "idempotency", "endpoint": row.get("endpoint"), "created_at": row.get("created_at")}


def decided_by(approval: ApprovalRecord, actor: str) -> bool:
    return approval.decided_by == actor or actor in (approval.approved_by or [])


def erase_device(device: DeviceRecord, *, now: float) -> None:
    """Revoke the device for good and remove what identified its owner."""
    device.credential_status = "revoked"
    device.trust_level = "revoked"
    device.revoked_at = device.revoked_at or now
    device.online = False
    device.push_token = None
    device.name = ERASED_ACTOR
    device.actor = ERASED_ACTOR
    device.updated_at = now


def erase_enrollment(enrollment: DeviceEnrollmentRecord) -> None:
    """The enrollment stays without its requester; an unfinished one is revoked."""
    enrollment.requested_by = ERASED_ACTOR
    if enrollment.status in ("pending", "paired"):
        enrollment.status = "revoked"


def erase_decision(approval: ApprovalRecord, *, actor: str) -> bool:
    """Remove ``actor`` from an approval someone else requested; True when it changed."""
    approvers = list(approval.approved_by or [])
    if approval.status == "pending":
        if actor not in approvers:
            return False
        # Withdrawn, not anonymised: the four-eyes check could no longer tell
        # this approver from a later one.
        approval.approved_by = [name for name in approvers if name != actor]
        return True
    if not decided_by(approval, actor):
        return False
    approval.approved_by = [ERASED_ACTOR if name == actor else name for name in approvers]
    if approval.decided_by == actor:
        approval.decided_by = ERASED_ACTOR
        approval.decision_reason = None
    return True


def references(payload: Any, ids: set[str]) -> bool:
    """Whether a stored payload, or the payload nested in it, has an *_id value among ``ids``."""
    if not isinstance(payload, dict) or not ids:
        return False
    data = cast(dict[str, Any], payload)
    if any(key.endswith("_id") and isinstance(value, str) and value in ids for key, value in data.items()):
        return True
    nested = data.get("payload")
    return isinstance(nested, dict) and any(
        key.endswith("_id") and isinstance(value, str) and value in ids for key, value in cast(dict[str, Any], nested).items()
    )


def inbox_about(item: LocalInboxRecord, actor: str, ids: set[str]) -> bool:
    return item.actor == actor or item.payload.get("actor") == actor or references(item.payload, ids)


def conflict_about(item: SyncConflictRecord, actor: str, ids: set[str]) -> bool:
    return item.actor == actor or references(item.local_payload, ids) or references(item.remote_payload, ids)


def _without_actor(value: Any, actor: str) -> Any:
    if isinstance(value, str):
        return ERASED_ACTOR if value == actor else value
    if isinstance(value, list):
        return [_without_actor(item, actor) for item in value]
    if isinstance(value, dict):
        return {key: _without_actor(item, actor) for key, item in value.items()}
    return value


def scrubbed_event(event_actor: str, payload: dict[str, Any], *, actor: str, erased_ids: set[str]) -> Optional[tuple[str, dict[str, Any]]]:
    """The (actor, payload) a sync event keeps once ``actor`` is erased; None when unchanged."""
    about_erased = event_actor == actor or references(payload, erased_ids)
    kept = payload
    if about_erased:
        kept = {key: value for key, value in payload.items() if key.endswith(("_id", "_ids")) or key in _EVENT_STATE_KEYS}
        kept["redacted"] = True
    kept = _without_actor(kept, actor)
    new_actor = ERASED_ACTOR if event_actor == actor else event_actor
    if new_actor == event_actor and kept == payload:
        return None
    return new_actor, kept
