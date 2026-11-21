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
- notifications about these records or about approvals they requested; pushes
  to their devices or about those records; their devices' runtime status;
- their offline-sync records (outbox, inbox, cursors, conflicts, and the
  operation-log entries of their operations), and anyone's offline-sync record
  that names one of those records by id, so no change to them leaves for the
  remote gateway; their idempotency replays.

It keeps these rows, without the person (for approvals this is security's one
rule for every store, SECURITY.md in #270):
- Approvals they requested, or that gate their tasks: the requester becomes
  ``[erased]`` and content is withheld. An agent step's proposal keeps only its
  structural fields and ``*_sha256``/``*_length`` arguments, as the ledger's
  does (any free text, such as a planner's reason, is dropped and named in
  ``fields_withheld``); a desktop task's action keeps only its capability. A
  pending one is withdrawn (rejected, "requester erased").
- Devices stay revoked, so an erased device can never verify, register or get
  a push again. Name and push token are removed; the actor becomes
  ``[erased]``.
- Device enrollments they started: requested_by becomes ``[erased]``. One not
  completed is revoked, so its pairing code can no longer enrol a device.
- Notifications they caused on someone else's records: the actor becomes
  ``[erased]``.
- Projects they own stay with their organization; ``owner_actor`` becomes
  ``[erased]``, in the project store's local backups too.
- Approvals they decided for someone else: decided_by and approved_by become
  ``[erased]``, and their reason is removed. On an approval still pending,
  their partial approval is withdrawn instead: an approver nobody can identify
  must not count toward the two that a critical action needs.
- Sync events keep their numbers, so readers' cursors stay valid. The actor
  becomes ``[erased]``. Events by the person or about an erased record keep
  only identifiers and states.

Not covered here: security's audit logs, kept by design (see
``privacy.data_subject``); the approval ledger, which applies the same approval
rule to its own rows (#270).
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


_REQUEST_KEYS = ("created_by", "proposer", "actor", "requested_by")
# What an erased subject's request keeps: structure, never free text. A
# planner's "reason" or "summary" can describe the person's task. Copied from
# security.dual_approval.STRUCTURAL_REQUEST_KEYS (#270) until it is on main.
STRUCTURAL_REQUEST_KEYS = frozenset({
    *_REQUEST_KEYS,
    "tool", "action", "risk", "source", "channel", "run_id", "plan_id", "step_id",
    "approval_id", "scope_hash", "requires_dual_approval", "provenance", "args",
})


def anonymized_proposal(proposal: dict[str, Any], actor: str) -> dict[str, Any]:
    """The ledger's rule for a request by an erased subject (security.dual_approval.anonymized_request,
    #270): requester fields become [erased]; only structure stays.

    Argument values are withheld (their digests and lengths stay), and every
    field outside STRUCTURAL_REQUEST_KEYS is dropped; ``fields_withheld`` names them.
    """
    out = {key: value for key, value in proposal.items() if key in STRUCTURAL_REQUEST_KEYS}
    dropped = sorted(key for key in proposal if key not in STRUCTURAL_REQUEST_KEYS and key not in {"args_withheld", "fields_withheld"})
    for key in _REQUEST_KEYS:
        if out.get(key) == actor:
            out[key] = ERASED_ACTOR
    args = out.get("args")
    if isinstance(args, dict):
        kept = {k: v for k, v in cast(dict[str, Any], args).items() if str(k).endswith(("_sha256", "_length"))}
        out["args"] = kept
        out["args_withheld"] = len(kept) != len(cast(dict[str, Any], args))
    if dropped:
        out["fields_withheld"] = dropped
    return out


def erase_request(approval: ApprovalRecord, *, actor: str, now: float) -> None:
    """An approval the erased person requested stays as the record of a requested action, without them."""
    approval.requested_by = ERASED_ACTOR
    detail = dict(approval.action_detail or {})
    if detail.get("kind") == "agent":
        detail["proposal"] = anonymized_proposal(cast(dict[str, Any], detail.get("proposal") or {}), actor)
        approval.reason = "An agent step waits for approval."  # the proposal's reason was about their task
    else:
        # A desktop task's action, and the text describing it, name what it touched.
        kept = {key: value for key, value in detail.items() if key == "capability" or str(key).endswith(("_sha256", "_length"))}
        if kept != detail:
            kept["args_withheld"] = True
        detail = kept
        approval.action = f"{kept.get('capability') or 'desktop task'} (details withheld)"
    approval.action_detail = detail
    if approval.status == "pending":
        approval.status = "rejected"
        approval.decided_by = ERASED_ACTOR
        approval.decision_reason = "requester erased"
        approval.decided_at = now


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
