from __future__ import annotations

import hashlib
import hmac
import json
import base64
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Literal, Optional, cast

DeviceType = Literal["desktop", "mobile", "web_admin"]
# "consumed": the approval authorized a run that a runtime device has claimed.
ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "consumed"]
TaskStatus = Literal["queued", "running", "blocked", "completed", "failed", "cancelled"]
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _fingerprint(payload: dict[str, Any] | None) -> str:
    canonical = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _secret_pepper() -> str:
    return (
        os.getenv("OMNIDESK_APPSYNC_SECRET_PEPPER")
        or os.getenv("OMNIDESK_GATEWAY_SECRET")
        or "omnidesk-dev-only-appsync-pepper"
    )


def _hash_secret(value: str, *, purpose: str = "appsync") -> str:
    message = f"{purpose}:{str(value or '')}".encode("utf-8")
    digest = hmac.new(_secret_pepper().encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{purpose}:{digest}"


def _secret_matches(stored: str, value: str, *, purpose: str = "appsync") -> bool:
    current = _hash_secret(value, purpose=purpose)
    if hmac.compare_digest(stored or "", current):
        return True
    legacy = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
    return bool(stored) and hmac.compare_digest(stored, legacy)


def _device_signing_message(*, challenge_id: str, nonce_hash: str) -> bytes:
    return f"omnidesk-device-challenge:v2:{challenge_id}:{nonce_hash}".encode("utf-8")


def _approval_decision_message(*, approval_id: str, decision: str, timestamp: str, nonce: str) -> bytes:
    return f"omnidesk-approval-decision:v1:{approval_id}:{decision}:{timestamp}:{nonce}".encode("utf-8")


LEGACY_HMAC_KEY_PREFIX = "legacy-hmac:"
LEGACY_HMAC_KEY_REFUSED = "legacy-hmac device keys are not accepted in production; enroll the device with an Ed25519 or P-256 public key"


def is_legacy_hmac_device_key(public_key: Optional[str]) -> bool:
    """A ``legacy-hmac:`` device "public key" is the device's HMAC secret, stored as is."""
    return str(public_key or "").strip().startswith(LEGACY_HMAC_KEY_PREFIX)


def _is_ed25519_public_key(value: str) -> bool:
    if not value.startswith("base64:"):
        return False
    try:
        return len(base64.b64decode(value.split(":", 1)[1], validate=True)) == 32
    except Exception:
        return False


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload or b"").hexdigest()


def _device_request_message(*, method: str, path: str, body_sha256: str, timestamp: str, nonce: str) -> bytes:
    return f"omnidesk-device-request:v1:{method.upper()}:{path}:{body_sha256}:{timestamp}:{nonce}".encode("utf-8")


# Events of these types carry a conversation's chat itself.
_CHAT_EVENT_PREFIXES = ("conversation.", "message.", "chat.")


def scope_sync_event_to_actor(
    event: dict[str, Any],
    *,
    actor: str,
    conversation_owner: Callable[[str], Optional[str]],
) -> Optional[dict[str, Any]]:
    """Return ``event`` as ``actor`` may see it in a sync feed, or ``None``.

    Conversations are private to the actor that created them; there is no
    membership model. Chat events of another actor's conversation are hidden, and
    any other event that references one (a task created from it) loses the
    reference. ``conversation_owner`` returns ``None`` for an unknown id, so an
    unresolvable ``conversation_id`` is treated as another actor's.
    """
    event_type = str(event.get("event_type") or "")
    chat_event = event_type.startswith(_CHAT_EVENT_PREFIXES)
    if chat_event and str(event.get("actor") or "") != actor:
        # Offline uploads may name the conversation under another key, or not at all.
        return None
    if event_type == "conversation.deleted":
        # Only the owner deletes, and the conversation no longer resolves: the
        # owner's devices still need the event to drop their copy.
        return event
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    hidden: set[str] = set()
    conversation_id = str(payload.get("conversation_id") or "")
    if conversation_id and conversation_owner(conversation_id) != actor:
        hidden.add("conversation_id")
    related_id = str(payload.get("related_id") or "")
    related_owner = conversation_owner(related_id) if related_id else None
    if related_owner is not None and related_owner != actor:
        hidden.add("related_id")
    if not hidden:
        return event
    if chat_event or event_type == "notification.created":
        return None
    return {**event, "payload": {key: value for key, value in payload.items() if key not in hidden}}


def _verify_device_signature(public_key: str, message: bytes, signature: str) -> bool:
    """Verify device challenge signatures.

    Production GA path: Ed25519 or P-256 public key in PEM/base64 plus base64/hex signature.
    Legacy HMAC is accepted only for keys prefixed with ``legacy-hmac:`` so old test
    fixtures cannot accidentally masquerade as production device credentials; callers
    refuse those keys in production first (``AppSyncStore._legacy_hmac_refused``).
    """
    public_key = (public_key or "").strip()
    signature = (signature or "").strip()
    if not public_key or not signature:
        return False
    if public_key.startswith(LEGACY_HMAC_KEY_PREFIX):
        secret = public_key.split(":", 1)[1]
        expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    except Exception:
        return False
    try:
        if signature.startswith("base64:"):
            sig = base64.b64decode(signature.split(":", 1)[1])
        else:
            try:
                sig = bytes.fromhex(signature)
            except ValueError:
                sig = base64.b64decode(signature)
        key_bytes = public_key.encode("utf-8")
        if public_key.startswith("base64:"):
            key_bytes = base64.b64decode(public_key.split(":", 1)[1])
            pub = ed25519.Ed25519PublicKey.from_public_bytes(key_bytes)
        else:
            pub = serialization.load_pem_public_key(key_bytes)
        if isinstance(pub, ed25519.Ed25519PublicKey):
            pub.verify(sig, message)
            return True
        if isinstance(pub, ec.EllipticCurvePublicKey):
            try:
                pub.verify(sig, message, ec.ECDSA(hashes.SHA256()))
                return True
            except InvalidSignature:
                # WebCrypto returns raw IEEE P1363 r||s ECDSA signatures; Python
                # cryptography verifies DER. Accept raw P-256 only after conversion.
                if len(sig) != 64:
                    raise
                der_sig = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
                pub.verify(der_sig, message, ec.ECDSA(hashes.SHA256()))
                return True
        return False
    except InvalidSignature:
        return False
    except Exception:
        return False


@dataclass
class Organization:
    organization_id: str = "org_default"
    name: str = "Default Organization"
    created_at: float = field(default_factory=_now)


@dataclass
class UserProfile:
    user_id: str
    display_name: str
    role: str = "operator"
    organization_id: str = "org_default"
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)


@dataclass
class DeviceRecord:
    device_id: str
    device_type: DeviceType
    name: str
    platform: str
    actor: str
    organization_id: str = "org_default"
    push_token: Optional[str] = None
    public_key: Optional[str] = None
    token_hash: Optional[str] = None
    credential_status: str = "pending"
    trust_level: str = "unverified"
    revoked_at: Optional[float] = None
    last_challenge_nonce_hash: Optional[str] = None
    last_challenge_expires_at: Optional[float] = None
    capabilities: list[str] = field(default_factory=list)
    online: bool = True
    last_seen_at: float = field(default_factory=_now)
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    # Ed25519 key held in biometric/passcode-gated storage on the device; once
    # set, approval decisions from this device must carry its signature.
    approval_public_key: Optional[str] = None
    approval_key_registered_at: Optional[float] = None


# Values that verify a device credential or address the device's push target.
_DEVICE_CREDENTIAL_FIELDS = frozenset({"push_token", "token_hash", "last_challenge_nonce_hash", "last_challenge_expires_at"})


def _push_target_hint(target: Optional[str]) -> Optional[str]:
    """Masked push target: its kind, plus the last 4 characters of a long value."""
    if not target:
        return None
    kind = "fid" if target.startswith("fid:") else "fcm_token"
    return f"{kind}:***{target[-4:]}" if len(target) >= 16 else f"{kind}:***"


def public_device_view(device: dict[str, Any]) -> dict[str, Any]:
    """A device record as API responses return it: no credential fields, and the
    push target only as a masked hint."""
    view = {key: value for key, value in device.items() if key not in _DEVICE_CREDENTIAL_FIELDS}
    view["push_target_hint"] = _push_target_hint(device.get("push_token"))
    if is_legacy_hmac_device_key(view.get("public_key")):
        # A legacy "public key" is the device's HMAC secret.
        view["public_key"] = f"{LEGACY_HMAC_KEY_PREFIX}***"
    return view


@dataclass
class ConversationRecord:
    conversation_id: str
    title: str
    actor: str
    organization_id: str = "org_default"
    source_device_id: Optional[str] = None
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)


@dataclass
class MessageRecord:
    message_id: str
    conversation_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    actor: str
    organization_id: str = "org_default"
    source_device_id: Optional[str] = None
    task_id: Optional[str] = None
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    model_profile: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None
    created_at: float = field(default_factory=_now)


@dataclass
class TaskRecord:
    task_id: str
    conversation_id: str
    title: str
    actor: str
    organization_id: str = "org_default"
    status: TaskStatus = "queued"
    assigned_runtime_device_id: Optional[str] = None
    requires_desktop_runtime: bool = False
    approval_id: Optional[str] = None
    result_summary: Optional[str] = None
    idempotency_key: Optional[str] = None
    claimed_by_device_id: Optional[str] = None
    lease_expires_at: Optional[float] = None
    attempt_count: int = 0
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    # An executable task carries one scoped action (see normalize_task_action);
    # its approval binds action_hash, and the desktop refuses a mismatch.
    capability: Optional[str] = None
    action: dict[str, Any] = field(default_factory=dict)
    action_hash: Optional[str] = None
    # What the runtime reported back, bounded; only the task's creator sees it.
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalRecord:
    approval_id: str
    task_id: str
    risk: Literal["low", "medium", "high", "critical"]
    action: str
    reason: str
    requested_by: str
    organization_id: str = "org_default"
    status: ApprovalStatus = "pending"
    decided_by: Optional[str] = None
    decision_reason: Optional[str] = None
    decision_idempotency_key: Optional[str] = None
    created_at: float = field(default_factory=_now)
    decided_at: Optional[float] = None
    expires_at: Optional[float] = None
    # The exact action an approval authorizes, for executable tasks.
    action_detail: dict[str, Any] = field(default_factory=dict)
    action_hash: Optional[str] = None
    # Four-eyes control: who has approved so far, and how many distinct
    # approvers (never the requester) the action needs.
    required_approvals: int = 1
    approved_by: list[str] = field(default_factory=list)


CRITICAL_APPROVERS = 2

# An agent step that waits for approval in the orchestrator's ApprovalStore is
# shown to the clients as an AppSync approval with this id prefix; the ledger
# (security's ApprovalStore) stays the authority, AppSync mirrors its state.
AGENT_APPROVAL_PREFIX = "agt_"
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_STEP_RE = re.compile(r"[^A-Za-z0-9_.-]")
_RISKS = ("low", "medium", "high", "critical")

# Pushes travel through third parties (FCM, APNs), so they carry no content:
# a fixed text per event and, for an agent step, only its tool.action and risk.
# Devices fetch the details from the authenticated API.
_PUSH_TEXT = {
    "approval.requested": ("Omni: approval required", "Open Omni to review the request."),
    "approval.decided": ("Omni: approval decided", "Open Omni for the details."),
    "task.status": ("Omni: task updated", "Open Omni for the details."),
    "conversation.ask.completed": ("Omni: new reply", "Open Omni to read it."),
}


def push_text(event_type: str, *, step: Optional[str] = None) -> tuple[str, str]:
    title, body = _PUSH_TEXT.get(event_type, ("Omni", "Open Omni for the details."))
    return title, (f"{step} needs approval." if step else body)


def agent_step_label(proposal: dict[str, Any]) -> str:
    """``tool.action`` of a proposal, reduced to identifier characters (never its args)."""
    tool = _STEP_RE.sub("", str(proposal.get("tool") or proposal.get("capability") or "agent"))[:64] or "agent"
    action = _STEP_RE.sub("", str(proposal.get("action") or ""))[:64]
    return f"{tool}.{action}" if action else tool


def agent_approval_record(*, source_approval_id: str, run_id: str, organization_id: str, requester: str, risk: str, proposal: dict[str, Any], requires_dual_approval: bool, expires_at: Optional[float], channel: str, now: float) -> ApprovalRecord:
    if not _SOURCE_ID_RE.fullmatch(source_approval_id):
        raise ValueError("invalid agent approval id")
    if not requester.strip():
        raise ValueError("an agent approval needs its requester")
    level = risk if risk in _RISKS else "high"
    step = agent_step_label(proposal)
    dual = bool(requires_dual_approval) or level == "critical"
    return ApprovalRecord(
        approval_id=AGENT_APPROVAL_PREFIX + source_approval_id, task_id="", risk=level,  # type: ignore[arg-type]
        action=f"Agent step {step}", reason=str(proposal.get("reason") or "An agent step waits for approval.")[:500],
        requested_by=requester.strip(), organization_id=organization_id, created_at=now, expires_at=expires_at,
        # ledger_dual: the ledger opened a dual approval (its proposal flag). A
        # critical step needs two approvers here even when it did not.
        action_detail={"kind": "agent", "source_approval_id": source_approval_id, "run_id": run_id, "channel": channel, "step": step, "ledger_dual": bool(requires_dual_approval), "proposal": proposal},
        required_approvals=CRITICAL_APPROVERS if dual else 1,
    )


def approvals_required(risk: str) -> int:
    """A critical action needs two distinct approvers; anything else one."""
    return CRITICAL_APPROVERS if risk == "critical" else 1


def apply_approval_decision(approval: ApprovalRecord, *, actor: str, decision: str, reason: Optional[str], idempotency_key: Optional[str], now: float, break_glass: bool = False) -> bool:
    """Records ``actor``'s decision on a pending approval; True once it is final.

    The requester may withdraw (reject) their own request but never approve it.
    A critical approval stays pending until two distinct approvers other than
    the requester have approved; a rejection by any of them is final. Approvals
    created before this rule still need what their risk requires now. A
    break-glass elevation may decide a single-approver action but is never one
    of the two approvers of a critical one.
    """
    required = max(int(approval.required_approvals or 1), approvals_required(approval.risk))
    if decision == "approved" and break_glass and required > 1:
        raise PermissionError("a break-glass elevation cannot approve a critical action")
    if decision == "approved" and actor == approval.requested_by:
        raise PermissionError("the requester cannot approve their own request")
    if decision == "approved" and actor in approval.approved_by:
        raise PermissionError("this approver has already approved; a different approver must decide")
    if decision == "approved":
        approval.approved_by = [*approval.approved_by, actor]
        approval.required_approvals = required
        if len(approval.approved_by) < approval.required_approvals:
            return False
    approval.status = "approved" if decision == "approved" else "rejected"
    approval.decided_by = actor
    approval.decision_reason = reason
    approval.decision_idempotency_key = idempotency_key
    approval.decided_at = now
    return True


def mirror_ledger_outcome(approval: ApprovalRecord, *, outcome: str, deciders: list[str], reason: Optional[str], now: float) -> None:
    """Copy onto an agent approval the outcome its ledger already holds, naming the ledger's deciders.

    The plane's checks in apply_approval_decision gate decisions made through it;
    this decision was made in the ledger (security's ApprovalStore), which stays
    the authority, so it is recorded as the ledger states it.
    """
    if outcome not in ("approved", "rejected") or not deciders:
        raise ValueError("a ledger outcome needs approved or rejected and at least one decider")
    if outcome == "approved":
        approval.approved_by = list(dict.fromkeys(deciders))
    approval.status = "approved" if outcome == "approved" else "rejected"
    approval.decided_by = deciders[-1]
    approval.decision_reason = reason
    approval.decision_idempotency_key = None
    approval.decided_at = now


@dataclass
class NotificationRecord:
    notification_id: str
    audience: Literal["desktop", "mobile", "web_admin", "all"]
    title: str
    body: str
    event_type: str
    actor: str
    organization_id: str = "org_default"
    related_id: Optional[str] = None
    read: bool = False
    created_at: float = field(default_factory=_now)


@dataclass
class RuntimeStatusRecord:
    device_id: str
    status: Literal["online", "offline", "degraded"]
    organization_id: str = "org_default"
    version: Optional[str] = None
    hostname: Optional[str] = None
    active_task_id: Optional[str] = None
    capabilities: list[str] = field(default_factory=list)
    last_heartbeat_at: float = field(default_factory=_now)


@dataclass
class DeviceEnrollmentRecord:
    enrollment_id: str
    device_type: DeviceType
    requested_by: str
    pairing_code_hash: str
    organization_id: str = "org_default"
    # pending -> paired (pairing code proven, device and key bound) -> completed
    # (challenge verified). A completed enrollment cannot verify another device.
    status: Literal["pending", "paired", "completed", "expired", "revoked"] = "pending"
    device_id: Optional[str] = None
    public_key: Optional[str] = None
    created_at: float = field(default_factory=_now)
    expires_at: float = field(default_factory=lambda: _now() + 600)
    completed_at: Optional[float] = None


@dataclass
class PushOutboxRecord:
    push_id: str
    device_id: str
    platform: str
    audience: str
    title: str
    body: str
    organization_id: str = "org_default"
    related_id: Optional[str] = None
    # "sending" while one dispatcher holds the lease; "dry_run" when no real
    # provider was configured, so nothing reached the device.
    status: Literal["pending", "sending", "sent", "dry_run", "failed"] = "pending"
    attempt_count: int = 0
    last_error: Optional[str] = None
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    event_type: Optional[str] = None
    next_attempt_at: float = 0.0
    lease_expires_at: Optional[float] = None
    claimed_by: Optional[str] = None
    provider: Optional[str] = None
    provider_message_id: Optional[str] = None


# What a dispatcher reports for a leased push: delivered, recorded as a dry run,
# to be retried at ``retry_at``, or given up.
PushOutcome = Literal["sent", "dry_run", "retry", "failed"]

@dataclass
class DeviceChallengeRecord:
    challenge_id: str
    enrollment_id: str
    device_id: str
    nonce_hash: str
    organization_id: str = "org_default"
    status: Literal["pending", "verified", "expired"] = "pending"
    created_at: float = field(default_factory=_now)
    expires_at: float = field(default_factory=lambda: _now() + 300)
    verified_at: Optional[float] = None


@dataclass
class LocalOutboxRecord:
    operation_id: str
    operation_type: str
    payload: dict[str, Any]
    actor: str
    organization_id: str = "org_default"
    device_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    payload_hash: str = ""
    conflict_policy: Literal["server-wins", "client-wins", "manual-review", "merge"] = "manual-review"
    # rejected: the remote gateway will never accept it (terminal, not retried).
    status: Literal["pending", "syncing", "synced", "failed", "conflict", "rejected"] = "pending"
    retry_count: int = 0
    next_retry_at: Optional[float] = None
    remote_seq: Optional[int] = None
    last_error: Optional[str] = None
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)


@dataclass
class LocalInboxRecord:
    remote_event_id: str
    event_type: str
    payload: dict[str, Any]
    actor: str
    organization_id: str = "org_default"
    source_device_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    payload_hash: str = ""
    status: Literal["received", "applied", "duplicate", "conflict"] = "received"
    received_at: float = field(default_factory=_now)


@dataclass
class SyncCursorRecord:
    cursor_id: str
    remote: str
    actor: str
    organization_id: str = "org_default"
    device_id: Optional[str] = None
    since_seq: int = 0
    updated_at: float = field(default_factory=_now)


@dataclass
class SyncConflictRecord:
    conflict_id: str
    operation_id: str
    remote_event_id: Optional[str]
    conflict_type: str
    strategy: Literal["server-wins", "client-wins", "manual-review", "merge"] = "manual-review"
    status: Literal["open", "resolved"] = "open"
    actor: str = "system"
    organization_id: str = "org_default"
    local_payload: dict[str, Any] = field(default_factory=dict)
    remote_payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=_now)
    resolved_at: Optional[float] = None


# Actions a desktop runtime may execute for an approved task. Each reads inside
# the desktop's approved workspace (~/OmniDesktopWorkspace) and nothing else;
# no capability writes, runs commands or reaches the network.
EXECUTABLE_CAPABILITIES = frozenset({"workspace.read_file", "workspace.list_directory"})
# Largest result a runtime may report for one task (JSON-encoded).
TASK_RESULT_MAX_BYTES = 64 * 1024


def _scoped_relative_path(value: Any, *, field_name: str, allow_root: bool) -> str:
    text = str(value if value is not None else "").strip()
    if allow_root and text in {"", "."}:
        return "."
    if not text or len(text) > 1024 or any(ord(ch) < 32 for ch in text) or "\\" in text:
        raise ValueError(f"{field_name} must be 1-1024 printable characters without backslashes")
    parts = text.split("/")
    if text.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} must be a relative path inside the workspace, without '.', '..' or empty parts")
    return text


def normalize_task_action(action: Any) -> dict[str, Any]:
    """Validate an executable action and return its canonical form.

    ``{"capability": "workspace.read_file" | "workspace.list_directory",
    "scope": {"workspace": "<dir under the desktop workspace, default '.'>",
    "relative_path": "<path inside it>"}}``. Unknown keys are rejected so an
    approval can never authorize more than what it displays.
    """
    if not isinstance(action, dict):
        raise ValueError("action must be an object")
    unknown = set(action) - {"capability", "scope"}
    if unknown:
        raise ValueError(f"unknown action fields: {sorted(unknown)}")
    capability = str(action.get("capability") or "")
    if capability not in EXECUTABLE_CAPABILITIES:
        raise ValueError(f"capability must be one of {sorted(EXECUTABLE_CAPABILITIES)}")
    scope = action.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("action scope must be an object")
    unknown = set(scope) - {"workspace", "relative_path"}
    if unknown:
        raise ValueError(f"unknown scope fields: {sorted(unknown)}")
    return {
        "capability": capability,
        "scope": {
            "workspace": _scoped_relative_path(scope.get("workspace"), field_name="scope.workspace", allow_root=True),
            "relative_path": _scoped_relative_path(scope.get("relative_path"), field_name="scope.relative_path", allow_root=capability == "workspace.list_directory"),
        },
    }


def task_action_hash(action: dict[str, Any]) -> str:
    """SHA-256 of the canonical action (sorted keys, compact): what an approval binds."""
    canonical = json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def describe_task_action(action: dict[str, Any]) -> str:
    """The sentence an approver reads: exactly what will run, and where."""
    scope = action.get("scope") or {}
    workspace = str(scope.get("workspace") or ".")
    where = str(scope.get("relative_path")) if workspace == "." else f"{workspace}/{scope.get('relative_path')}"
    verb = "Read the file" if action.get("capability") == "workspace.read_file" else "List the directory"
    return f"{verb} {where} in ~/OmniDesktopWorkspace on the desktop that claims it"[:300]


def normalize_task_result(result: Any) -> dict[str, Any]:
    if result in (None, {}):
        return {}
    if not isinstance(result, dict):
        raise ValueError("result must be an object")
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > TASK_RESULT_MAX_BYTES:
        raise ValueError(f"result must be at most {TASK_RESULT_MAX_BYTES} bytes as JSON")
    return json.loads(encoded)


def task_view_for_actor(task: dict[str, Any], *, actor: Optional[str], conversation_owner: Optional[str]) -> dict[str, Any]:
    """Tasks are organization-wide work items; their conversation and their result are not."""
    hidden: set[str] = set()
    if actor and conversation_owner != actor:
        hidden.add("conversation_id")
    if actor and task.get("actor") != actor:
        hidden.add("result")
    return {key: value for key, value in task.items() if key not in hidden} if hidden else task


def authorize_task_status_change(
    task: TaskRecord,
    *,
    actor: str,
    actor_organization_id: str,
    status: str,
    device_id: Optional[str],
    assigned_runtime_device_id: Optional[str],
    reporting_device: Optional[DeviceRecord],
    normalize_org: Callable[[Optional[str]], str],
    now: float,
) -> None:
    """Only the device holding a task's lease reports on it, and finished tasks stay finished.

    Shared by every AppSync backend so the rules cannot drift between them.
    ``reporting_device`` is the record of ``device_id or assigned_runtime_device_id``.
    """
    if actor_organization_id != normalize_org(task.organization_id):
        raise PermissionError("organization scope mismatch")
    if task.status in TERMINAL_TASK_STATUSES:
        raise ValueError(f"task is already {task.status}")
    if device_id and assigned_runtime_device_id and device_id != assigned_runtime_device_id:
        raise PermissionError("assigned_runtime_device_id must match the reporting device")
    reporting_device_id = device_id or assigned_runtime_device_id
    if task.status != "running":
        # Nothing holds an unclaimed task; its creator may still withdraw it.
        if status != "cancelled" or task.actor != actor:
            raise PermissionError("an unclaimed task can only be cancelled by its creator")
        return
    if not reporting_device_id or reporting_device_id != task.claimed_by_device_id:
        lease_expired = not task.lease_expires_at or task.lease_expires_at <= now
        if status == "cancelled" and task.actor == actor and lease_expired:
            # A run abandoned by its device can be withdrawn by the task's creator.
            return
        raise PermissionError("only the device holding the task lease can update it")
    device = reporting_device
    if device and (device.actor != actor or normalize_org(device.organization_id) != normalize_org(task.organization_id)):
        raise PermissionError("the lease-holding device belongs to a different actor or organization")
    if status == "blocked":
        raise ValueError("tasks are blocked only by the approval flow")
    if status == "queued" and task.approval_id:
        raise PermissionError("an approved task cannot be re-queued; request a new approval")


# Largest sync page; a reader with more to catch up gets has_more and pages on.
SYNC_PAGE_LIMIT = 1000


def sync_reset(*, head: int, pruned_through: int) -> dict[str, Any]:
    """The reader's cursor is older than retained history (or ahead of it, after a
    restore): it must re-bootstrap, then read on from ``sync_seq``."""
    return {"sync_seq": head, "head_seq": head, "events": [], "has_more": False, "reset_required": True, "pruned_through": pruned_through}


def sync_page(
    candidates: list[dict[str, Any]],
    *,
    head: int,
    pruned_through: int,
    limit: int,
    scope: Optional[Callable[[dict[str, Any]], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Build a sync response from the events after the cursor, oldest first.

    ``sync_seq`` is where the reader resumes: the last event of a full page
    (``has_more`` is then true), else the head. Sequence order is commit order
    on every backend, so resuming there never skips an event. ``scope`` hides
    events the reader may not see after paging, so hidden events still advance
    the cursor.
    """
    bounded = max(1, min(int(limit or SYNC_PAGE_LIMIT), SYNC_PAGE_LIMIT))
    page = candidates[:bounded]
    has_more = len(candidates) > bounded
    cursor = int(page[-1]["seq"]) if has_more else head
    events = page if scope is None else [event for event in (scope(item) for item in page) if event is not None]
    return {"sync_seq": cursor, "head_seq": head, "events": events, "has_more": has_more, "reset_required": False, "pruned_through": pruned_through}


def mentions_conversation(payload: Any, conversation_id: str) -> bool:
    """Whether a stored payload (or an outbox event's nested one) is about the conversation."""
    if not isinstance(payload, dict):
        return False
    if conversation_id in (payload.get("conversation_id"), payload.get("related_id"), payload.get("id") if payload.get("record") == "conversation" else None):
        return True
    nested = payload.get("payload")
    return isinstance(nested, dict) and conversation_id in (nested.get("conversation_id"), nested.get("related_id"))


def runtime_status_changed(previous: Optional[RuntimeStatusRecord], current: RuntimeStatusRecord) -> bool:
    """Whether a heartbeat changed what the runtime reports, not just its liveness."""
    if previous is None:
        return True
    return (
        previous.status != current.status
        or previous.active_task_id != current.active_task_id
        or previous.version != current.version
        or previous.hostname != current.hostname
        or sorted(previous.capabilities or []) != sorted(current.capabilities or [])
        or previous.organization_id != current.organization_id
    )


def check_enrollment_device(
    enrollment: DeviceEnrollmentRecord,
    device: DeviceRecord,
    *,
    actor: str,
    normalize_org: Callable[[Optional[str]], str],
) -> None:
    """A device may complete an enrollment only for its own actor, organization and type."""
    if normalize_org(device.organization_id) != normalize_org(enrollment.organization_id):
        raise PermissionError("device belongs to a different organization")
    if device.actor != actor:
        raise PermissionError("device belongs to a different actor")
    if device.revoked_at or device.credential_status == "revoked":
        raise PermissionError("device is revoked; enroll a new device")
    if device.device_type != enrollment.device_type:
        raise PermissionError("device type does not match the enrollment")


class IdempotencyConflict(ValueError):
    """Raised when the same idempotency key is replayed with different input."""


class AppSyncStore:
    """Small durable app-state store for the tri-app foundation.

    This store intentionally keeps the V1 product surface narrow: it gives the
    Desktop App, Mobile App, and Web Admin a single task/approval/notification
    timeline without weakening the existing runtime permission gates. Enterprise
    deployments can replace it with the existing PostgreSQL repository layer in
    a later migration while preserving the API contract.
    """

    def __init__(self, path: Path, *, local_outbox_enabled: bool = False):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.local_outbox_enabled = bool(local_outbox_enabled)
        self._lock = threading.RLock()
        self.organization = Organization()
        self.users: dict[str, UserProfile] = {}
        self.devices: dict[str, DeviceRecord] = {}
        self.conversations: dict[str, ConversationRecord] = {}
        self.messages: dict[str, MessageRecord] = {}
        self.tasks: dict[str, TaskRecord] = {}
        self.approvals: dict[str, ApprovalRecord] = {}
        self.notifications: dict[str, NotificationRecord] = {}
        self.runtimes: dict[str, RuntimeStatusRecord] = {}
        self.device_enrollments: dict[str, DeviceEnrollmentRecord] = {}
        self.push_outbox: dict[str, PushOutboxRecord] = {}
        self.device_challenges: dict[str, DeviceChallengeRecord] = {}
        self.events: list[dict[str, Any]] = []
        self.idempotency: dict[str, dict[str, Any]] = {}
        self.device_request_nonces: dict[str, float] = {}
        self.local_outbox: dict[str, LocalOutboxRecord] = {}
        self.local_inbox: dict[str, LocalInboxRecord] = {}
        self.operation_log: list[dict[str, Any]] = []
        self.sync_cursors: dict[str, SyncCursorRecord] = {}
        self.sync_conflicts: dict[str, SyncConflictRecord] = {}
        self.network_state: dict[str, Any] = {"state": "local_only", "updated_at": _now()}
        # Server-side organization of each actor on first use (configuration, never a request).
        self.actor_organizations: dict[str, str] = {}
        # Turned off in production; see configure_device_key_policy.
        self.allow_legacy_hmac_device_keys = True
        self._seq = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        with self._lock:
            self._seq = int(raw.get("seq", 0))
            org = raw.get("organization") or {}
            if org:
                self.organization = Organization(**org)
            self.users = {k: UserProfile(**v) for k, v in (raw.get("users") or {}).items()}
            self.devices = {k: DeviceRecord(**v) for k, v in (raw.get("devices") or {}).items()}
            self.conversations = {k: ConversationRecord(**v) for k, v in (raw.get("conversations") or {}).items()}
            self.messages = {k: MessageRecord(**v) for k, v in (raw.get("messages") or {}).items()}
            self.tasks = {k: TaskRecord(**v) for k, v in (raw.get("tasks") or {}).items()}
            self.approvals = {k: ApprovalRecord(**v) for k, v in (raw.get("approvals") or {}).items()}
            self.notifications = {k: NotificationRecord(**v) for k, v in (raw.get("notifications") or {}).items()}
            self.runtimes = {k: RuntimeStatusRecord(**v) for k, v in (raw.get("runtimes") or {}).items()}
            self.device_enrollments = {k: DeviceEnrollmentRecord(**v) for k, v in (raw.get("device_enrollments") or {}).items()}
            self.push_outbox = {k: PushOutboxRecord(**v) for k, v in (raw.get("push_outbox") or {}).items()}
            self.device_challenges = {k: DeviceChallengeRecord(**v) for k, v in (raw.get("device_challenges") or {}).items()}
            self.events = list(raw.get("events") or [])[-1000:]
            self.idempotency = dict(raw.get("idempotency") or {})
            self.local_outbox = {k: LocalOutboxRecord(**v) for k, v in (raw.get("local_outbox") or {}).items()}
            self.local_inbox = {k: LocalInboxRecord(**v) for k, v in (raw.get("local_inbox") or {}).items()}
            self.operation_log = list(raw.get("operation_log") or [])[-2000:]
            self.sync_cursors = {k: SyncCursorRecord(**v) for k, v in (raw.get("sync_cursors") or {}).items()}
            self.sync_conflicts = {k: SyncConflictRecord(**v) for k, v in (raw.get("sync_conflicts") or {}).items()}
            self.network_state = dict(raw.get("network_state") or {"state": "local_only", "updated_at": _now()})

    def _persist(self) -> None:
        payload = {
            "seq": self._seq,
            "organization": asdict(self.organization),
            "users": {k: asdict(v) for k, v in self.users.items()},
            "devices": {k: asdict(v) for k, v in self.devices.items()},
            "conversations": {k: asdict(v) for k, v in self.conversations.items()},
            "messages": {k: asdict(v) for k, v in self.messages.items()},
            "tasks": {k: asdict(v) for k, v in self.tasks.items()},
            "approvals": {k: asdict(v) for k, v in self.approvals.items()},
            "notifications": {k: asdict(v) for k, v in self.notifications.items()},
            "runtimes": {k: asdict(v) for k, v in self.runtimes.items()},
            "device_enrollments": {k: asdict(v) for k, v in self.device_enrollments.items()},
            "push_outbox": {k: asdict(v) for k, v in self.push_outbox.items()},
            "device_challenges": {k: asdict(v) for k, v in self.device_challenges.items()},
            "events": self.events[-1000:],
            "idempotency": self.idempotency,
            "local_outbox": {k: asdict(v) for k, v in self.local_outbox.items()},
            "local_inbox": {k: asdict(v) for k, v in self.local_inbox.items()},
            "operation_log": self.operation_log[-2000:],
            "sync_cursors": {k: asdict(v) for k, v in self.sync_cursors.items()},
            "sync_conflicts": {k: asdict(v) for k, v in self.sync_conflicts.items()},
            "network_state": self.network_state,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _default_org_id(self) -> str:
        return self.organization.organization_id or "org_default"

    def _normalize_org_id(self, organization_id: Optional[str]) -> str:
        return str(organization_id or self._default_org_id() or "org_default").strip() or "org_default"

    def configure_device_key_policy(self, *, allow_legacy_hmac: bool) -> None:
        """Whether ``legacy-hmac:`` device keys may be registered, paired or verified.

        The factory turns them off for production stores. Routes also pass their
        per-request production check to the verify methods; either one refuses.
        """
        with self._lock:
            self.allow_legacy_hmac_device_keys = bool(allow_legacy_hmac)

    def _legacy_hmac_refused(self, public_key: Optional[str], *, allow_legacy_hmac: bool = True) -> bool:
        allowed = self.allow_legacy_hmac_device_keys and allow_legacy_hmac
        return not allowed and is_legacy_hmac_device_key(public_key)

    def configure_actor_organizations(self, assignments: Optional[dict[str, str]]) -> None:
        """Set the server-side organization that each listed actor joins on first use."""
        with self._lock:
            self.actor_organizations = {
                str(actor).strip(): self._normalize_org_id(organization_id)
                for actor, organization_id in (assignments or {}).items()
                if str(actor).strip()
            }

    def organization_for_new_actor(self, actor: Optional[str]) -> str:
        return self._normalize_org_id(self.actor_organizations.get(actor or ""))

    def organization_for_actor(self, actor: Optional[str]) -> str:
        """The organization an actor acts in (the orchestrator's organization resolver)."""
        with self._lock:
            return self._organization_for_actor(actor)

    def _organization_for_actor(self, actor: Optional[str]) -> str:
        user = self.users.get(actor or "unknown")
        return self._normalize_org_id(user.organization_id) if user else self.organization_for_new_actor(actor)

    def _organization_for_device(self, device_id: Optional[str]) -> str:
        device = self.devices.get(device_id or "")
        return self._normalize_org_id(device.organization_id if device else None)

    def _organization_for_related(self, related_id: Optional[str], actor: Optional[str]) -> str:
        related_id = related_id or ""
        if related_id in self.tasks:
            return self._normalize_org_id(self.tasks[related_id].organization_id)
        if related_id in self.approvals:
            return self._normalize_org_id(self.approvals[related_id].organization_id)
        if related_id in self.conversations:
            return self._normalize_org_id(self.conversations[related_id].organization_id)
        if related_id in self.devices:
            return self._normalize_org_id(self.devices[related_id].organization_id)
        return self._organization_for_actor(actor)

    def _organization_for_event(self, event: dict[str, Any]) -> str:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        return self._normalize_org_id(
            event.get("organization_id")
            or payload.get("organization_id")
            or self._organization_for_related(
                str(payload.get("task_id") or payload.get("approval_id") or payload.get("conversation_id") or payload.get("device_id") or ""),
                str(event.get("actor") or ""),
            )
        )

    def _actor_can_access_org(self, actor: Optional[str], organization_id: Optional[str]) -> bool:
        return self._organization_for_actor(actor) == self._normalize_org_id(organization_id)

    def _require_actor_org(self, actor: Optional[str], organization_id: Optional[str]) -> None:
        if not self._actor_can_access_org(actor, organization_id):
            raise PermissionError("organization scope mismatch")

    def _event(self, event_type: str, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        payload = dict(payload or {})
        organization_id = self._normalize_org_id(payload.get("organization_id") or self._organization_for_related(
            str(payload.get("task_id") or payload.get("approval_id") or payload.get("conversation_id") or payload.get("device_id") or ""),
            actor,
        ))
        payload.setdefault("organization_id", organization_id)
        event = {"seq": self._seq, "ts": _now(), "event_type": event_type, "actor": actor, "organization_id": organization_id, "payload": payload}
        self.events.append(event)
        self.events = self.events[-1000:]
        if self.local_outbox_enabled and not event_type.startswith(("operation.", "sync.", "network.")):
            self._auto_queue_event_locked(event)
        return event

    def _append_operation_log_locked(self, event: str, payload: dict[str, Any]) -> None:
        self.operation_log.append({"seq": self._seq, "ts": _now(), "event": event, "payload": dict(payload or {})})
        self.operation_log = self.operation_log[-2000:]

    def _auto_queue_event_locked(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        operation_id = _id("op")
        organization_id = self._normalize_org_id(event.get("organization_id") or payload.get("organization_id"))
        record = LocalOutboxRecord(
            operation_id=operation_id,
            operation_type=str(event.get("event_type") or "event"),
            payload={
                "event_seq": int(event.get("seq", 0)),
                "event_type": event.get("event_type"),
                "payload": payload,
            },
            actor=str(event.get("actor") or "system"),
            organization_id=organization_id,
            device_id=payload.get("device_id") or payload.get("source_device_id"),
            idempotency_key=f"event:{event.get('seq')}:{event.get('event_type')}",
            payload_hash=_fingerprint(payload),
        )
        self.local_outbox[operation_id] = record
        self._append_operation_log_locked("operation.enqueued", {"operation_id": operation_id, "operation_type": record.operation_type, "organization_id": organization_id})

    @staticmethod
    def _record(obj: Any) -> dict[str, Any]:
        return asdict(obj)

    def _idempotency_scope(self, *, actor: str, endpoint: str, key: str, organization_id: Optional[str] = None) -> str:
        return f"{self._normalize_org_id(organization_id or self._organization_for_actor(actor))}:{actor or 'unknown'}:{endpoint}:{key}"

    def _idempotency_get(self, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None) -> Optional[Any]:
        if not key:
            return None
        scoped = self._idempotency_scope(actor=actor, endpoint=endpoint, key=key)
        item = self.idempotency.get(scoped)
        if not item:
            return None
        incoming_hash = _fingerprint(payload)
        if item.get("payload_hash") != incoming_hash:
            raise IdempotencyConflict("idempotency key was reused with a different payload")
        return item.get("response")

    def _idempotency_put(self, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None, response: Any) -> None:
        if not key:
            return
        organization_id = self._organization_for_actor(actor)
        scoped = self._idempotency_scope(actor=actor, endpoint=endpoint, key=key, organization_id=organization_id)
        self.idempotency[scoped] = {
            "organization_id": organization_id,
            "actor": actor or "unknown",
            "endpoint": endpoint,
            "key": key,
            "payload_hash": _fingerprint(payload),
            "response": response,
            "created_at": _now(),
        }

    def set_network_state(self, state: str, *, reason: Optional[str] = None) -> dict[str, Any]:
        allowed = {"offline", "local_only", "online_detected", "reconnecting", "syncing", "update_checking", "healthy", "failed", "rollback"}
        if state not in allowed:
            raise ValueError("invalid network state")
        with self._lock:
            changed = self.network_state.get("state") != state or self.network_state.get("reason") != reason
            self.network_state = {"state": state, "reason": reason, "updated_at": _now()}
            if changed:
                # The reconnect loop reports its state every minute; only changes are events.
                self._event("network.state", "system", self.network_state)
            self._persist()
            return dict(self.network_state)

    def enqueue_local_operation(
        self,
        *,
        actor: str,
        operation_type: str,
        payload: dict[str, Any],
        device_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        conflict_policy: Literal["server-wins", "client-wins", "manual-review", "merge"] = "manual-review",
    ) -> dict[str, Any]:
        with self._lock:
            organization_id = self._organization_for_actor(actor)
            payload = dict(payload or {})
            operation_id = _id("op")
            key = idempotency_key or payload.get("idempotency_key") or operation_id
            record = LocalOutboxRecord(
                operation_id=operation_id,
                operation_type=operation_type,
                payload=payload,
                actor=actor or "unknown",
                organization_id=organization_id,
                device_id=device_id or payload.get("device_id") or payload.get("source_device_id"),
                idempotency_key=str(key),
                payload_hash=_fingerprint(payload),
                conflict_policy=conflict_policy,
            )
            self.local_outbox[operation_id] = record
            self._append_operation_log_locked("operation.enqueued", {"operation_id": operation_id, "operation_type": operation_type, "organization_id": organization_id})
            self._persist()
            return self._record(record)

    def pending_local_outbox(self, *, actor: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            values = [item for item in self.local_outbox.values() if item.status in {"pending", "failed"} and (item.next_retry_at is None or item.next_retry_at <= _now())]
            if actor:
                organization_id = self._organization_for_actor(actor)
                values = [item for item in values if self._normalize_org_id(item.organization_id) == organization_id]
            values = sorted(values, key=lambda item: item.created_at)[: max(1, min(int(limit or 100), 500))]
            return [self._record(item) for item in values]

    def mark_local_operation_synced(self, operation_id: str, *, remote_seq: Optional[int] = None) -> dict[str, Any]:
        with self._lock:
            item = self.local_outbox.get(operation_id)
            if not item:
                raise KeyError("local outbox operation not found")
            item.status = "synced"
            item.remote_seq = remote_seq
            item.updated_at = _now()
            self._append_operation_log_locked("operation.synced", {"operation_id": operation_id, "remote_seq": remote_seq, "organization_id": item.organization_id})
            self._persist()
            return self._record(item)

    def mark_local_operation_failed(self, operation_id: str, *, error: str, retry_delay_seconds: int = 60) -> dict[str, Any]:
        with self._lock:
            item = self.local_outbox.get(operation_id)
            if not item:
                raise KeyError("local outbox operation not found")
            item.status = "failed"
            item.retry_count += 1
            item.last_error = str(error)[:1000]
            item.next_retry_at = _now() + max(1, int(retry_delay_seconds or 60))
            item.updated_at = _now()
            self._append_operation_log_locked("operation.failed", {"operation_id": operation_id, "error": item.last_error, "retry_count": item.retry_count, "organization_id": item.organization_id})
            self._persist()
            return self._record(item)

    def mark_local_operation_rejected(self, operation_id: str, *, reason: str) -> dict[str, Any]:
        """The remote gateway refused the operation; it is not sent again."""
        with self._lock:
            item = self.local_outbox.get(operation_id)
            if not item:
                raise KeyError("local outbox operation not found")
            item.status = "rejected"
            item.last_error = str(reason)[:1000]
            item.next_retry_at = None
            item.updated_at = _now()
            self._append_operation_log_locked("operation.rejected", {"operation_id": operation_id, "reason": item.last_error, "organization_id": item.organization_id})
            self._persist()
            return self._record(item)

    def mark_local_operation_conflict(self, operation_id: str, *, detail: str) -> dict[str, Any]:
        """The remote gateway received the operation and holds it as a conflict for review."""
        with self._lock:
            item = self.local_outbox.get(operation_id)
            if not item:
                raise KeyError("local outbox operation not found")
            item.status = "conflict"
            item.last_error = str(detail)[:1000]
            item.updated_at = _now()
            self._append_operation_log_locked("operation.conflict", {"operation_id": operation_id, "detail": item.last_error, "organization_id": item.organization_id})
            self._persist()
            return self._record(item)

    def record_sync_cursor(self, *, actor: str, remote: str, since_seq: int, device_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            organization_id = self._organization_for_actor(actor)
            cursor_id = f"{organization_id}:{actor or 'unknown'}:{remote or 'default'}:{device_id or ''}"
            record = SyncCursorRecord(cursor_id=cursor_id, remote=remote or "default", actor=actor or "unknown", organization_id=organization_id, device_id=device_id, since_seq=max(0, int(since_seq or 0)))
            self.sync_cursors[cursor_id] = record
            self._append_operation_log_locked("sync.cursor.updated", {"cursor_id": cursor_id, "since_seq": record.since_seq, "organization_id": organization_id})
            self._persist()
            return self._record(record)

    def record_remote_events(
        self,
        *,
        actor: str,
        events: list[dict[str, Any]],
        remote: str = "default",
        device_id: Optional[str] = None,
        conflict_policy: Literal["server-wins", "client-wins", "manual-review", "merge"] = "manual-review",
    ) -> dict[str, Any]:
        with self._lock:
            organization_id = self._organization_for_actor(actor)
            applied = 0
            duplicates = 0
            conflicts: list[dict[str, Any]] = []
            max_seq = 0
            for event in events:
                remote_seq = int(event.get("seq", 0) or 0)
                max_seq = max(max_seq, remote_seq)
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                remote_event_id = str(event.get("event_id") or event.get("operation_id") or f"{remote}:{remote_seq}:{event.get('event_type', 'event')}")
                # Event ids and idempotency keys are client-chosen (or predictable),
                # so dedupe and outbox matching only ever see the caller's own records.
                inbox_key = self._inbox_key(organization_id, actor, remote_event_id)
                if inbox_key in self.local_inbox:
                    duplicates += 1
                    self.local_inbox[inbox_key].status = "duplicate"
                    continue
                key = str(event.get("idempotency_key") or payload.get("idempotency_key") or "")
                payload_hash = _fingerprint(payload)
                matched = [
                    item for item in self.local_outbox.values()
                    if key and item.idempotency_key == key and item.status in {"pending", "failed", "syncing"}
                    and item.actor == actor and self._normalize_org_id(item.organization_id) == organization_id
                ]
                if matched and any(item.payload_hash != payload_hash for item in matched) and conflict_policy in {"server-wins", "client-wins"}:
                    # server-wins: the remote version supersedes the queued local one.
                    # client-wins: the local operation stays queued and is sent again.
                    closed = SyncConflictRecord(
                        conflict_id=_id("conflict"), operation_id=matched[0].operation_id, remote_event_id=remote_event_id,
                        conflict_type="idempotency_payload_mismatch", strategy=conflict_policy, status="resolved",
                        actor=actor or "unknown", organization_id=organization_id, local_payload=dict(matched[0].payload or {}),
                        remote_payload=dict(payload or {}), resolved_at=_now(),
                    )
                    self.sync_conflicts[closed.conflict_id] = closed
                    self._append_operation_log_locked("sync.conflict.resolved", {"conflict_id": closed.conflict_id, "operation_id": closed.operation_id, "strategy": conflict_policy, "organization_id": organization_id})
                    if conflict_policy == "server-wins":
                        self.mark_local_operation_synced(matched[0].operation_id, remote_seq=remote_seq)
                    self.local_inbox[inbox_key] = LocalInboxRecord(remote_event_id=inbox_key, event_type=str(event.get("event_type") or "event"), payload=payload, actor=actor, organization_id=organization_id, source_device_id=device_id, idempotency_key=key or None, payload_hash=payload_hash, status="applied")
                    applied += 1
                    continue
                if matched and any(item.payload_hash != payload_hash for item in matched):
                    conflict = self._record_sync_conflict_locked(
                        operation_id=matched[0].operation_id,
                        remote_event_id=remote_event_id,
                        actor=actor,
                        organization_id=organization_id,
                        strategy=conflict_policy,
                        local_payload=matched[0].payload,
                        remote_payload=payload,
                        conflict_type="idempotency_payload_mismatch",
                    )
                    conflicts.append(conflict)
                    self.local_inbox[inbox_key] = LocalInboxRecord(remote_event_id=inbox_key, event_type=str(event.get("event_type") or "event"), payload=payload, actor=actor, organization_id=organization_id, source_device_id=device_id, idempotency_key=key or None, payload_hash=payload_hash, status="conflict")
                    continue
                inbox = LocalInboxRecord(remote_event_id=inbox_key, event_type=str(event.get("event_type") or "event"), payload=payload, actor=actor, organization_id=organization_id, source_device_id=device_id, idempotency_key=key or None, payload_hash=payload_hash, status="applied")
                self.local_inbox[inbox_key] = inbox
                if matched:
                    self.mark_local_operation_synced(matched[0].operation_id, remote_seq=remote_seq)
                applied += 1
            if max_seq:
                self.record_sync_cursor(actor=actor, remote=remote, since_seq=max_seq, device_id=device_id)
            self._append_operation_log_locked("sync.remote_events.recorded", {"applied": applied, "duplicates": duplicates, "conflicts": len(conflicts), "organization_id": organization_id})
            self._persist()
            return {"applied": applied, "duplicates": duplicates, "conflicts": conflicts, "cursor": max_seq}

    def receive_outbox_operations(
        self,
        *,
        actor: str,
        operations: list[dict[str, Any]],
        remote: str = "default",
        device_id: Optional[str] = None,
        conflict_policy: Literal["server-wins", "client-wins", "manual-review", "merge"] = "manual-review",
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        for operation in operations:
            payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
            events.append({
                "seq": int(operation.get("remote_seq") or operation.get("seq") or 0),
                "operation_id": operation.get("operation_id") or operation.get("id"),
                "event_type": operation.get("operation_type") or operation.get("event_type") or "operation.replay",
                "actor": operation.get("actor") or actor,
                "idempotency_key": operation.get("idempotency_key"),
                "payload": payload,
            })
        return self.record_remote_events(actor=actor, events=events, remote=remote, device_id=device_id, conflict_policy=conflict_policy)

    @staticmethod
    def _inbox_key(organization_id: str, actor: str, remote_event_id: str) -> str:
        # Also the stored remote_event_id: the Postgres inbox is keyed on it alone.
        return json.dumps([organization_id, actor, remote_event_id], ensure_ascii=False, separators=(",", ":"))

    def _record_sync_conflict_locked(
        self,
        *,
        operation_id: str,
        remote_event_id: Optional[str],
        actor: str,
        organization_id: str,
        strategy: Literal["server-wins", "client-wins", "manual-review", "merge"],
        local_payload: dict[str, Any],
        remote_payload: dict[str, Any],
        conflict_type: str,
    ) -> dict[str, Any]:
        conflict_id = _id("conflict")
        conflict = SyncConflictRecord(
            conflict_id=conflict_id,
            operation_id=operation_id,
            remote_event_id=remote_event_id,
            conflict_type=conflict_type,
            strategy=strategy,
            actor=actor or "unknown",
            organization_id=organization_id,
            local_payload=dict(local_payload or {}),
            remote_payload=dict(remote_payload or {}),
        )
        self.sync_conflicts[conflict_id] = conflict
        if operation_id in self.local_outbox:
            self.local_outbox[operation_id].status = "conflict"
            self.local_outbox[operation_id].updated_at = _now()
        self._append_operation_log_locked("sync.conflict.opened", {"conflict_id": conflict_id, "operation_id": operation_id, "strategy": strategy, "organization_id": organization_id})
        return self._record(conflict)

    def list_sync_conflicts(self, *, actor: str, status: Optional[str] = "open", limit: int = 200) -> list[dict[str, Any]]:
        """The actor's own sync conflicts, newest first."""
        with self._lock:
            organization_id = self._organization_for_actor(actor)
            values = [
                item for item in self.sync_conflicts.values()
                if item.actor == actor and self._normalize_org_id(item.organization_id) == organization_id and (not status or item.status == status)
            ]
            ordered = sorted(values, key=lambda item: item.created_at, reverse=True)
            return [self._record(item) for item in ordered[: max(1, min(int(limit or 200), 1000))]]

    def resolve_sync_conflict(self, *, actor: str, conflict_id: str, resolution: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Settle one of the actor's open conflicts: keep_server, apply_client or merged."""
        from omnidesk_agent.appsync.offline_sync_apply import RESOLUTIONS, json_records, resolve_record_conflict

        if resolution not in RESOLUTIONS:
            raise ValueError("resolution must be keep_server, apply_client or merged")
        with self._lock:
            conflict = self.sync_conflicts.get(conflict_id)
            if conflict is None or conflict.actor != actor or self._normalize_org_id(conflict.organization_id) != self._organization_for_actor(actor):
                raise KeyError("conflict not found")
            if conflict.status != "open":
                raise ValueError("conflict already resolved")
            if conflict.conflict_type == "idempotency_payload_mismatch":
                operation = self.local_outbox.get(conflict.operation_id)
                if operation is not None:
                    if resolution == "keep_server":
                        operation.status = "synced"
                    else:
                        if resolution == "merged":
                            if not isinstance(payload, dict):
                                raise ValueError("a merged resolution needs the merged payload")
                            operation.payload = dict(payload)
                            operation.payload_hash = _fingerprint(operation.payload)
                        operation.status = "pending"
                        operation.next_retry_at = None
                    operation.updated_at = _now()
            else:
                resolve_record_conflict(json_records(self, actor), conflict, resolution=resolution, payload=payload)
            conflict.status = "resolved"
            conflict.resolved_at = _now()
            conflict.strategy = cast(Any, {"keep_server": "server-wins", "apply_client": "client-wins", "merged": "merge"}[resolution])
            self._append_operation_log_locked("sync.conflict.resolved", {"conflict_id": conflict_id, "operation_id": conflict.operation_id, "strategy": conflict.strategy, "organization_id": conflict.organization_id})
            self._persist()
            return self._record(conflict)

    def replay_operation_log(self, *, actor: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.operation_log)
            if actor:
                organization_id = self._organization_for_actor(actor)
                values = [item for item in values if self._normalize_org_id((item.get("payload") or {}).get("organization_id")) == organization_id]
            return values[-max(1, min(int(limit or 200), 2000)):]

    def sync_state(self, *, actor: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            outbox = list(self.local_outbox.values())
            inbox = list(self.local_inbox.values())
            conflicts = list(self.sync_conflicts.values())
            if actor:
                organization_id = self._organization_for_actor(actor)
                outbox = [item for item in outbox if self._normalize_org_id(item.organization_id) == organization_id]
                inbox = [item for item in inbox if self._normalize_org_id(item.organization_id) == organization_id]
                conflicts = [item for item in conflicts if self._normalize_org_id(item.organization_id) == organization_id]
            return {
                "network_state": dict(self.network_state),
                "outbox": {
                    "pending": sum(1 for item in outbox if item.status in {"pending", "failed"}),
                    "synced": sum(1 for item in outbox if item.status == "synced"),
                    "conflict": sum(1 for item in outbox if item.status == "conflict"),
                    "rejected": sum(1 for item in outbox if item.status == "rejected"),
                },
                "inbox": {"received": len(inbox)},
                "conflicts": {"open": sum(1 for item in conflicts if item.status == "open")},
                "cursors": [self._record(cursor) for cursor in self.sync_cursors.values() if not actor or self._normalize_org_id(cursor.organization_id) == self._organization_for_actor(actor)],
            }

    def ensure_user(self, actor: str, role: str = "operator", display_name: Optional[str] = None, organization_id: Optional[str] = None) -> dict[str, Any]:
        """Return the actor's profile, creating it on first use.

        ``organization_id`` comes from a server-side caller, never a request. A new
        actor joins it, else its configured organization, else the default one; an
        existing actor never changes organization.
        """
        actor = actor or "unknown"
        requested = self._normalize_org_id(organization_id) if organization_id is not None else None
        with self._lock:
            assigned = self.actor_organizations.get(actor)
            if requested is not None and assigned is not None and requested != assigned:
                raise PermissionError("organization does not match the server-side assignment for this actor")
            if actor not in self.users:
                new_organization_id = requested or self.organization_for_new_actor(actor)
                self.users[actor] = UserProfile(user_id=actor, display_name=display_name or actor, role=role, organization_id=new_organization_id)
                self._event("user.created", actor, {"user_id": actor, "role": role, "organization_id": new_organization_id})
                self._persist()
            elif requested is not None and self._normalize_org_id(self.users[actor].organization_id) != requested:
                raise PermissionError("actor is already bound to a different organization")
            return self._record(self.users[actor])

    def register_device(self, *, actor: str, device_id: Optional[str], device_type: DeviceType, name: str, platform: str, push_token: Optional[str] = None, capabilities: Optional[list[str]] = None, public_key: Optional[str] = None, token_hash: Optional[str] = None, organization_id: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None, device_signature_verified: bool = False) -> dict[str, Any]:
        """Register a device or refresh an existing one owned by the same actor.

        The device joins its actor's organization; ``organization_id`` is only a
        server-side assignment for a new actor (see ``ensure_user``).
        ``device_signature_verified`` must only be set when the request was signed
        by this device's own verified key; it allows a verified device to update
        its push target and capabilities.
        """
        actor = actor or "unknown"
        if self._legacy_hmac_refused(public_key):
            raise PermissionError(LEGACY_HMAC_KEY_REFUSED)
        with self._lock:
            self.ensure_user(actor, organization_id=organization_id)
            organization_id = self._organization_for_actor(actor)
            cached = self._idempotency_get(actor=actor, endpoint="devices.register", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached
            device_id = device_id or _id("dev")
            now = _now()
            existing = self.devices.get(device_id)
            if existing:
                if self._normalize_org_id(existing.organization_id) != organization_id:
                    raise PermissionError("device is already bound to a different organization")
                if existing.actor != actor:
                    raise PermissionError("device is already bound to a different actor")
                if existing.credential_status == "revoked" or existing.revoked_at:
                    raise PermissionError("device is revoked; enroll a new device")
                key_changed = bool(public_key) and public_key != existing.public_key
                if existing.credential_status == "verified":
                    self._check_verified_device_update(
                        existing,
                        device_type=device_type,
                        key_changed=key_changed,
                        push_token=push_token,
                        capabilities=capabilities,
                        token_hash=token_hash,
                        device_signature_verified=device_signature_verified,
                    )
                existing.device_type = device_type
                existing.name = name or existing.name
                existing.platform = platform or existing.platform
                existing.organization_id = organization_id
                existing.push_token = push_token or existing.push_token
                existing.public_key = public_key or existing.public_key
                existing.token_hash = token_hash or existing.token_hash
                if key_changed:
                    existing.credential_status = "enrolled"
                    existing.trust_level = "public_key_registered"
                existing.capabilities = list(capabilities or existing.capabilities)
                existing.online = True
                existing.last_seen_at = now
                existing.updated_at = now
                device = existing
            else:
                device = DeviceRecord(device_id=device_id, device_type=device_type, name=name, platform=platform, actor=actor, organization_id=organization_id, push_token=push_token, public_key=public_key, token_hash=token_hash, credential_status="enrolled" if public_key else "pending", trust_level="public_key_registered" if public_key else "unverified", capabilities=list(capabilities or []))
                self.devices[device_id] = device
            self._event("device.registered", actor, {"device_id": device_id, "device_type": device_type, "organization_id": organization_id})
            result = self._record(device)
            self._idempotency_put(actor=actor, endpoint="devices.register", key=idempotency_key, payload=idempotency_payload, response=result)
            self._persist()
            return result

    @staticmethod
    def _check_verified_device_update(
        existing: DeviceRecord,
        *,
        device_type: str,
        key_changed: bool,
        push_token: Optional[str],
        capabilities: Optional[list[str]],
        token_hash: Optional[str],
        device_signature_verified: bool,
    ) -> None:
        # Type and key are bound by the enrollment; the token is minted by challenge
        # verification or rotation. Push target and capabilities need the device's
        # own signature so an actor token alone cannot redirect pushes or relabel it.
        if key_changed:
            raise PermissionError("verified device keys can only be replaced through re-enrollment")
        if device_type != existing.device_type:
            raise PermissionError("verified device type can only change through re-enrollment")
        if token_hash and token_hash != existing.token_hash:
            raise PermissionError("verified device tokens can only change through challenge verification or rotation")
        if device_signature_verified:
            return
        if push_token and push_token != existing.push_token:
            raise PermissionError("verified device push targets can only change through a device-signed request")
        if capabilities and list(capabilities) != list(existing.capabilities):
            raise PermissionError("verified device capabilities can only change through a device-signed request")

    def create_conversation(self, *, actor: str, title: str, source_device_id: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            cached = self._idempotency_get(actor=actor, endpoint="conversations.create", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached
            user = self.ensure_user(actor)
            source_org = self._normalize_org_id(user["organization_id"])
            if source_device_id and source_device_id in self.devices:
                device_org = self._organization_for_device(source_device_id)
                if device_org != source_org:
                    raise PermissionError("source device belongs to a different organization")
            cid = _id("conv")
            convo = ConversationRecord(conversation_id=cid, title=title or "New conversation", actor=actor, organization_id=source_org, source_device_id=source_device_id)
            self.conversations[cid] = convo
            self._event("conversation.created", actor, {"conversation_id": cid, "title": convo.title, "organization_id": source_org})
            result = self._record(convo)
            self._idempotency_put(actor=actor, endpoint="conversations.create", key=idempotency_key, payload=idempotency_payload, response=result)
            self._persist()
            return result

    def record_event(self, event_type: str, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append and persist an event raised by a collaborator such as the project store."""
        with self._lock:
            event = self._event(event_type, actor, payload)
            self._persist()
            return event

    def get_idempotency_response(self, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None = None) -> Optional[Any]:
        with self._lock:
            return self._idempotency_get(actor=actor, endpoint=endpoint, key=key, payload=payload)

    def put_idempotency_response(self, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None, response: Any) -> None:
        with self._lock:
            self._idempotency_put(actor=actor, endpoint=endpoint, key=key, payload=payload, response=response)
            self._persist()

    def _owned_conversation_locked(self, conversation_id: str, actor: str) -> ConversationRecord:
        """Conversations are private to the actor that created them."""
        conversation = self.conversations.get(conversation_id)
        if not conversation:
            raise KeyError("conversation not found")
        self._require_actor_org(actor, conversation.organization_id)
        if conversation.actor != actor:
            # The same answer as for a missing conversation.
            raise KeyError("conversation not found")
        return conversation

    def delete_conversation(self, *, actor: str, conversation_id: str) -> dict[str, Any]:
        """Erase one of the actor's conversations and the data derived from it.

        Messages, chat replays, notifications and pushes about it, idempotency
        responses and offline-sync payloads that carry it are deleted; sync
        events keep their numbers (readers' cursors stay valid) but lose the
        title. Tasks and approvals created from it stay: they are the
        organization's record of requested and approved actions.
        """
        with self._lock:
            conversation = self._owned_conversation_locked(conversation_id, actor)
            organization_id = self._normalize_org_id(conversation.organization_id)

            def drop(table: dict[str, Any], keep: Callable[[Any], bool]) -> int:
                doomed = [key for key, value in table.items() if not keep(value)]
                for key in doomed:
                    del table[key]
                return len(doomed)

            deleted = {
                "messages": drop(self.messages, lambda item: item.conversation_id != conversation_id),
                "notifications": drop(self.notifications, lambda item: item.related_id != conversation_id),
                "push_outbox": drop(self.push_outbox, lambda item: item.related_id != conversation_id),
                "idempotency_keys": drop(self.idempotency, lambda item: item.get("actor") != actor or conversation_id not in json.dumps(item.get("response"), default=str)),
                "local_inbox": drop(self.local_inbox, lambda item: item.actor != actor or not mentions_conversation(item.payload, conversation_id)),
                "local_outbox": drop(self.local_outbox, lambda item: item.actor != actor or not mentions_conversation(item.payload, conversation_id)),
                "sync_conflicts": drop(self.sync_conflicts, lambda item: item.actor != actor or not (mentions_conversation(item.local_payload, conversation_id) or mentions_conversation(item.remote_payload, conversation_id))),
            }
            redacted = 0
            for event in self.events:
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else None
                if payload is not None and mentions_conversation(payload, conversation_id):
                    payload.pop("title", None)
                    payload["redacted"] = True
                    redacted += 1
            deleted["sync_events_redacted"] = redacted
            del self.conversations[conversation_id]
            deleted["conversations"] = 1
            self._event("conversation.deleted", actor, {"conversation_id": conversation_id, "organization_id": organization_id})
            self._persist()
            return {"conversation_id": conversation_id, "deleted": deleted}

    def _conversation_owner(self, conversation_id: str) -> Optional[str]:
        conversation = self.conversations.get(conversation_id)
        return conversation.actor if conversation else None

    def _task_for_actor(self, task: dict[str, Any], actor: Optional[str]) -> dict[str, Any]:
        """Tasks are organization-wide work items; the conversation they came from is not."""
        return task_view_for_actor(task, actor=actor, conversation_owner=self._conversation_owner(str(task.get("conversation_id") or "")))

    def add_chat_user_message(self, *, actor: str, conversation_id: str, content: str, source_device_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            conversation = self._owned_conversation_locked(conversation_id, actor)
            now = _now()
            mid = _id("msg")
            message = MessageRecord(message_id=mid, conversation_id=conversation_id, role="user", content=content, actor=actor, organization_id=conversation.organization_id, source_device_id=source_device_id)
            self.messages[mid] = message
            conversation.updated_at = now
            self._event("conversation.ask.requested", actor, {"message_id": mid, "conversation_id": conversation_id, "organization_id": conversation.organization_id})
            self._persist()
            return self._record(message)

    def add_assistant_message(self, *, actor: str, conversation_id: str, content: str, provider: str, model: str, profile: str, usage: Optional[dict[str, Any]] = None, trace_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            conversation = self._owned_conversation_locked(conversation_id, actor)
            now = _now()
            mid = _id("msg")
            trace_id = trace_id or _id("trace")
            message = MessageRecord(
                message_id=mid,
                conversation_id=conversation_id,
                role="assistant",
                content=content,
                actor=actor,
                organization_id=conversation.organization_id,
                model_provider=provider,
                model_name=model,
                model_profile=profile,
                usage=dict(usage or {}),
                trace_id=trace_id,
            )
            self.messages[mid] = message
            conversation.updated_at = now
            self._event("conversation.ask.completed", actor, {"message_id": mid, "conversation_id": conversation_id, "provider": provider, "model": model, "profile": profile, "trace_id": trace_id, "organization_id": conversation.organization_id})
            self._notify_locked(audience="all", title="Omni assistant replied", body=content[:180], event_type="conversation.ask.completed", actor=actor, related_id=conversation_id)
            self._persist()
            return self._record(message)

    def add_message_and_task(self, *, actor: str, conversation_id: str, content: str, source_device_id: Optional[str] = None, requires_desktop_runtime: bool = False, risk: str = "medium", idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None, action: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """``action`` makes the task executable by a desktop runtime: it is
        validated, needs an approval, and that approval binds its hash."""
        executable = normalize_task_action(action) if action is not None else None
        if executable is not None:
            requires_desktop_runtime = True
        with self._lock:
            cached = self._idempotency_get(actor=actor, endpoint="messages.create", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached
            conversation = self._owned_conversation_locked(conversation_id, actor)
            mid = _id("msg")
            task_id = _id("task")
            message = MessageRecord(message_id=mid, conversation_id=conversation_id, role="user", content=content, actor=actor, organization_id=conversation.organization_id, source_device_id=source_device_id, task_id=task_id)
            task = TaskRecord(
                task_id=task_id,
                conversation_id=conversation_id,
                title=(content or "Task")[:120],
                actor=actor,
                organization_id=conversation.organization_id,
                status="queued",
                requires_desktop_runtime=bool(requires_desktop_runtime),
                idempotency_key=idempotency_key,
                capability=executable["capability"] if executable else None,
                action=executable or {},
                action_hash=task_action_hash(executable) if executable else None,
            )
            self.messages[mid] = message
            self.tasks[task_id] = task
            self._event("message.created", actor, {"message_id": mid, "conversation_id": conversation_id, "task_id": task_id, "organization_id": conversation.organization_id})
            self._event("task.created", actor, {"task_id": task_id, "conversation_id": conversation_id, "requires_desktop_runtime": requires_desktop_runtime, "organization_id": conversation.organization_id})
            if risk in {"high", "critical"} or requires_desktop_runtime:
                approval = self.create_approval_locked(task_id=task_id, actor=actor, risk="high" if risk not in {"critical"} else "critical", action=describe_task_action(executable) if executable else task.title, reason="Desktop/runtime action requires cross-device approval", action_detail=executable, action_hash=task.action_hash)
                task.status = "blocked"
                task.approval_id = approval["approval_id"]
            result = {"message": self._record(message), "task": self._record(task), "approval": self._record(self.approvals[task.approval_id]) if task.approval_id else None}
            self._idempotency_put(actor=actor, endpoint="messages.create", key=idempotency_key, payload=idempotency_payload, response=result)
            self._persist()
            return result

    def create_approval_locked(self, *, task_id: str, actor: str, risk: Literal["low", "medium", "high", "critical"], action: str, reason: str, action_detail: Optional[dict[str, Any]] = None, action_hash: Optional[str] = None) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        organization_id = self._normalize_org_id(task.organization_id if task else self._organization_for_actor(actor))
        approval_id = _id("appr")
        approval = ApprovalRecord(approval_id=approval_id, task_id=task_id, risk=risk, action=action, reason=reason, requested_by=actor, organization_id=organization_id, expires_at=_now() + 900, action_detail=dict(action_detail or {}), action_hash=action_hash, required_approvals=approvals_required(risk))
        self.approvals[approval_id] = approval
        self._event("approval.requested", actor, {"approval_id": approval_id, "task_id": task_id, "risk": risk, "organization_id": organization_id})
        self._notify_locked(audience="mobile", title="Omni approval required", body=action[:180], event_type="approval.requested", actor=actor, related_id=approval_id)
        self._notify_locked(audience="web_admin", title="High-risk action pending", body=reason, event_type="approval.requested", actor=actor, related_id=approval_id)
        return self._record(approval)

    def get_approval(self, approval_id: str, *, actor: str) -> dict[str, Any]:
        with self._lock:
            approval = self.approvals.get(approval_id)
            if not approval:
                raise KeyError("approval not found")
            self._require_actor_org(actor, approval.organization_id)
            return self._record(approval)

    def record_agent_approval(self, *, source_approval_id: str, run_id: str, organization_id: str, requester: str, risk: str, proposal: dict[str, Any], requires_dual_approval: bool, expires_at: Optional[float], channel: str = "") -> dict[str, Any]:
        """Shows an agent step waiting in the ApprovalStore to the clients (idempotent)."""
        with self._lock:
            record = agent_approval_record(source_approval_id=source_approval_id, run_id=run_id, organization_id=self._normalize_org_id(organization_id), requester=requester, risk=risk, proposal=proposal, requires_dual_approval=requires_dual_approval, expires_at=expires_at, channel=channel, now=_now())
            existing = self.approvals.get(record.approval_id)
            if existing:
                return self._record(existing)
            self.approvals[record.approval_id] = record
            self._event("approval.requested", record.requested_by, {"approval_id": record.approval_id, "kind": "agent", "run_id": run_id, "risk": record.risk, "organization_id": record.organization_id})
            label = f"Agent step {record.action_detail['step']} ({record.risk})"
            for audience in ("mobile", "web_admin"):
                self._notify_locked(audience=audience, title="Agent step needs approval", body=record.action, event_type="approval.requested", actor=record.requested_by, related_id=record.approval_id, push_step=label)
            self._persist()
            return self._record(record)

    def decide_approval(self, *, approval_id: str, actor: str, decision: Literal["approved", "rejected"], reason: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None, break_glass: bool = False) -> dict[str, Any]:
        with self._lock:
            cached = self._idempotency_get(actor=actor, endpoint="approvals.decide", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached["approval"] if isinstance(cached, dict) and "approval" in cached else cached
            approval = self.approvals.get(approval_id)
            if not approval:
                raise KeyError("approval not found")
            self._require_actor_org(actor, approval.organization_id)
            if approval.status != "pending":
                raise ValueError("approval already decided")
            if approval.expires_at and approval.expires_at < _now():
                approval.status = "expired"
                self._event("approval.expired", actor, {"approval_id": approval_id, "task_id": approval.task_id, "organization_id": approval.organization_id})
                self._persist()
                raise ValueError("approval expired")
            if not apply_approval_decision(approval, actor=actor, decision=decision, reason=reason, idempotency_key=idempotency_key, now=_now(), break_glass=break_glass):
                # One of two approvals of a critical action: ask for the second.
                self._event("approval.partially_approved", actor, {"approval_id": approval_id, "task_id": approval.task_id, "approvals": len(approval.approved_by), "required": approval.required_approvals, "organization_id": approval.organization_id})
                self._notify_locked(audience="mobile", title="Second approval required", body=approval.action[:180], event_type="approval.requested", actor=actor, related_id=approval.approval_id)
                result = self._record(approval)
                self._idempotency_put(actor=actor, endpoint="approvals.decide", key=idempotency_key, payload=idempotency_payload, response={"approval": result})
                self._persist()
                return result
            task = self.tasks.get(approval.task_id)
            # Only a task still waiting on this approval moves; a decision must not
            # revive a task that was cancelled meanwhile.
            if task and task.status == "blocked":
                task.status = "queued" if decision == "approved" else "cancelled"
                task.updated_at = _now()
            self._event("approval.decided", actor, {"approval_id": approval_id, "decision": decision, "task_id": approval.task_id, "organization_id": approval.organization_id})
            if approval.task_id:
                self._notify_locked(audience="desktop", title=f"Approval {decision}", body=approval.action[:180], event_type="approval.decided", actor=actor, related_id=approval.task_id)
            result = self._record(approval)
            self._idempotency_put(actor=actor, endpoint="approvals.decide", key=idempotency_key, payload=idempotency_payload, response={"approval": result})
            self._persist()
            return result

    def mirror_agent_decision(self, *, approval_id: str, outcome: Literal["approved", "rejected"], deciders: list[str], reason: Optional[str] = None) -> dict[str, Any]:
        """Record on an agent approval the outcome its ledger already holds (see mirror_ledger_outcome).

        For a ledger decided outside this plane (``/approvals/*``, the CLI), or by
        an attempt whose mirror was lost. A decided approval is returned as it is.
        """
        if not approval_id.startswith(AGENT_APPROVAL_PREFIX):
            raise ValueError("only an agent approval mirrors its ledger")
        with self._lock:
            approval = self.approvals.get(approval_id)
            if not approval:
                raise KeyError("approval not found")
            if approval.status == "pending":
                mirror_ledger_outcome(approval, outcome=outcome, deciders=deciders, reason=reason, now=_now())
                self._event("approval.decided", str(approval.decided_by), {"approval_id": approval_id, "decision": outcome, "task_id": approval.task_id, "organization_id": approval.organization_id})
                self._persist()
            return self._record(approval)

    def heartbeat_runtime(self, *, actor: str, device_id: str, status: Literal["online", "offline", "degraded"], version: Optional[str] = None, hostname: Optional[str] = None, active_task_id: Optional[str] = None, capabilities: Optional[list[str]] = None) -> dict[str, Any]:
        with self._lock:
            organization_id = self._organization_for_device(device_id) if device_id in self.devices else self._organization_for_actor(actor)
            if active_task_id and active_task_id in self.tasks and self._normalize_org_id(self.tasks[active_task_id].organization_id) != organization_id:
                raise PermissionError("runtime cannot attach to a task from another organization")
            record = RuntimeStatusRecord(device_id=device_id, status=status, organization_id=organization_id, version=version, hostname=hostname, active_task_id=active_task_id, capabilities=list(capabilities or []))
            previous = self.runtimes.get(device_id)
            self.runtimes[device_id] = record
            if device_id in self.devices:
                self.devices[device_id].online = status == "online"
                self.devices[device_id].last_seen_at = record.last_heartbeat_at
                self.devices[device_id].updated_at = record.last_heartbeat_at
            if runtime_status_changed(previous, record):
                # Liveness alone is not a sync event: a desktop beats every few
                # seconds, and the feed must not grow with uptime.
                self._event("runtime.status_changed", actor, {"device_id": device_id, "status": status, "active_task_id": active_task_id, "organization_id": organization_id})
            self._persist()
            return self._record(record)


    def claim_next_task(self, *, actor: str, device_id: str, lease_seconds: int = 60, capabilities: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
        """Atomically claim the next queued desktop-runtime task for a device.

        The lease lets multiple desktop runtimes poll safely: a second runtime
        cannot take the same task until the lease expires or the first runtime
        finishes/fails it.
        """
        now = _now()
        lease_seconds = max(15, min(int(lease_seconds or 60), 600))
        with self._lock:
            self.ensure_user(actor)
            organization_id = self._organization_for_device(device_id) if device_id in self.devices else self._organization_for_actor(actor)
            offered = set(capabilities or [])
            for task in sorted(self.tasks.values(), key=lambda item: item.created_at):
                if self._normalize_org_id(task.organization_id) != organization_id:
                    continue
                if not task.requires_desktop_runtime:
                    continue
                if task.capability and task.capability not in offered:
                    # Handed only to a runtime that says it can execute it.
                    continue
                if task.status not in {"queued", "running"}:
                    continue
                if task.status == "running" and task.lease_expires_at and task.lease_expires_at > now:
                    continue
                approval = self.approvals.get(task.approval_id) if task.approval_id else None
                if task.approval_id and not self._approval_allows_claim(task, approval):
                    continue
                task.status = "running"
                task.assigned_runtime_device_id = device_id
                task.claimed_by_device_id = device_id
                task.lease_expires_at = now + lease_seconds
                task.attempt_count += 1
                task.updated_at = now
                self._event("task.claimed", actor, {"task_id": task.task_id, "device_id": device_id, "lease_expires_at": task.lease_expires_at, "capabilities": list(capabilities or []), "organization_id": organization_id})
                if approval is not None:
                    self._consume_approval_locked(approval, actor=actor, device_id=device_id)
                self._persist()
                return self._task_for_actor(self._record(task), actor)
            return None

    @staticmethod
    def _approval_allows_claim(task: TaskRecord, approval: Optional[ApprovalRecord]) -> bool:
        """An approval authorizes exactly one claim of its task.

        The claim consumes it. A run whose lease expired is not handed to another
        device under the same approval: that would execute an approved action twice.
        The holder can still report, and the creator can cancel it.
        """
        if approval is None or approval.task_id != task.task_id:
            return False
        return task.status == "queued" and approval.status == "approved"

    def _consume_approval_locked(self, approval: ApprovalRecord, *, actor: str, device_id: str) -> None:
        if approval.status == "consumed":
            return
        approval.status = "consumed"
        self._event("approval.consumed", actor, {"approval_id": approval.approval_id, "task_id": approval.task_id, "device_id": device_id, "organization_id": approval.organization_id})

    def _authorize_task_status_change_locked(self, task: TaskRecord, *, actor: str, status: str, device_id: Optional[str], assigned_runtime_device_id: Optional[str]) -> None:
        """Only the device holding a task's lease reports on it, and finished tasks stay finished."""
        reporting_device_id = device_id or assigned_runtime_device_id
        authorize_task_status_change(
            task,
            actor=actor,
            actor_organization_id=self._organization_for_actor(actor),
            status=status,
            device_id=device_id,
            assigned_runtime_device_id=assigned_runtime_device_id,
            reporting_device=self.devices.get(reporting_device_id) if reporting_device_id else None,
            normalize_org=self._normalize_org_id,
            now=_now(),
        )

    @staticmethod
    def _apply_task_status(task: TaskRecord, *, status: str, result_summary: Optional[str], now: float) -> None:
        task.status = cast(TaskStatus, status)
        task.result_summary = result_summary or task.result_summary
        task.updated_at = now
        if status == "queued":
            # The lease holder released the task; the next claim starts a new run.
            task.claimed_by_device_id = None
            task.assigned_runtime_device_id = None
            task.lease_expires_at = None
        elif status in TERMINAL_TASK_STATUSES:
            task.lease_expires_at = None

    def update_task_status(self, *, task_id: str, actor: str, status: TaskStatus, result_summary: Optional[str] = None, assigned_runtime_device_id: Optional[str] = None, device_id: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None, result: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Apply a status report; ``device_id`` is the device that authenticated the request."""
        reported = normalize_task_result(result)
        if reported and status not in {"completed", "failed"}:
            raise ValueError("a result is reported only with completed or failed")
        with self._lock:
            cached = self._idempotency_get(actor=actor, endpoint="tasks.status", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError("task not found")
            self._authorize_task_status_change_locked(task, actor=actor, status=status, device_id=device_id, assigned_runtime_device_id=assigned_runtime_device_id)
            self._apply_task_status(task, status=status, result_summary=result_summary, now=_now())
            if reported:
                task.result = reported
            self._event("task.status", actor, {"task_id": task_id, "status": status, "organization_id": task.organization_id})
            if status in TERMINAL_TASK_STATUSES:
                self._notify_locked(audience="all", title=f"Task {status}", body=task.title[:180], event_type="task.status", actor=actor, related_id=task_id)
            result = self._task_for_actor(self._record(task), actor)
            self._idempotency_put(actor=actor, endpoint="tasks.status", key=idempotency_key, payload=idempotency_payload, response=result)
            self._persist()
            return result

    def _notify_locked(self, *, audience: Literal["desktop", "mobile", "web_admin", "all"], title: str, body: str, event_type: str, actor: str, related_id: Optional[str], push_step: Optional[str] = None) -> dict[str, Any]:
        nid = _id("notif")
        organization_id = self._organization_for_related(related_id, actor)
        notification = NotificationRecord(notification_id=nid, audience=audience, title=title, body=body, event_type=event_type, actor=actor, organization_id=organization_id, related_id=related_id)
        self.notifications[nid] = notification
        # A chat reply notification goes to its conversation's actor alone.
        recipient = self._conversation_owner(related_id or "")
        for device in self.devices.values():
            if self._normalize_org_id(device.organization_id) != organization_id:
                continue
            if not self._device_receives_push(device):
                continue
            if audience not in {"all", device.device_type}:
                continue
            if recipient is not None and device.actor != recipient:
                continue
            push_id = _id("push")
            push_title, push_body = push_text(event_type, step=push_step)
            self.push_outbox[push_id] = PushOutboxRecord(push_id=push_id, device_id=device.device_id, platform=device.platform, audience=audience, title=push_title, body=push_body, organization_id=organization_id, related_id=related_id, event_type=event_type)
        self._event("notification.created", actor, {"notification_id": nid, "audience": audience, "event_type": event_type, "related_id": related_id, "organization_id": organization_id})
        return self._record(notification)

    @staticmethod
    def _device_receives_push(device: DeviceRecord) -> bool:
        """Pushes carry task and approval text: only challenge-verified, unrevoked devices get them."""
        return (
            bool(device.push_token)
            and not device.revoked_at
            and device.credential_status == "verified"
            and device.trust_level == "challenge_verified"
        )

    def _notification_visible(self, notification: NotificationRecord, actor: str) -> bool:
        owner = self._conversation_owner(notification.related_id or "")
        return owner is None or owner == actor

    def register_push_token(self, *, actor: str, device_id: str, push_token: str, platform: str = "unknown") -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(device_id)
            if not device:
                raise KeyError("device not found")
            self._require_actor_org(actor, device.organization_id)
            if device.actor != actor:
                raise PermissionError("device belongs to a different actor")
            if device.revoked_at or device.credential_status == "revoked":
                raise PermissionError("device is revoked; enroll a new device")
            device.push_token = push_token
            device.platform = platform or device.platform
            device.last_seen_at = _now()
            device.updated_at = device.last_seen_at
            self._event("device.push_token_registered", actor, {"device_id": device_id, "platform": platform, "organization_id": device.organization_id})
            self._persist()
            return self._record(device)

    def start_device_enrollment(self, *, actor: str, device_type: DeviceType, pairing_code: str) -> dict[str, Any]:
        with self._lock:
            if device_type not in {"desktop", "mobile", "web_admin"}:
                raise ValueError("invalid device_type")
            user = self.ensure_user(actor)
            organization_id = self._normalize_org_id(user["organization_id"])
            enrollment_id = _id("enroll")
            record = DeviceEnrollmentRecord(
                enrollment_id=enrollment_id,
                device_type=device_type,
                requested_by=actor or "unknown",
                pairing_code_hash=_hash_secret(pairing_code, purpose="pairing_code"),
                organization_id=organization_id,
            )
            self.device_enrollments[enrollment_id] = record
            self._event("device.enrollment_started", actor, {"enrollment_id": enrollment_id, "device_type": device_type, "organization_id": organization_id})
            self._persist()
            result = self._record(record)
            result.pop("pairing_code_hash", None)
            return result

    def complete_device_enrollment(self, *, actor: str, enrollment_id: str, pairing_code: str, device_id: str, public_key: str) -> dict[str, Any]:
        with self._lock:
            record = self.device_enrollments.get(enrollment_id)
            if not record:
                raise KeyError("enrollment not found")
            self._require_actor_org(actor, record.organization_id)
            if record.status != "pending":
                raise ValueError("enrollment is not pending")
            if record.expires_at < _now():
                record.status = "expired"
                self._persist()
                raise ValueError("enrollment expired")
            if not _secret_matches(record.pairing_code_hash, pairing_code, purpose="pairing_code"):
                raise ValueError("pairing code mismatch")
            device_id = (device_id or "").strip()
            public_key = (public_key or "").strip()
            if not device_id or not public_key:
                raise ValueError("device_id and public_key are required")
            if self._legacy_hmac_refused(public_key):
                raise ValueError(LEGACY_HMAC_KEY_REFUSED)
            device = self.devices.get(device_id)
            if device:
                self._check_enrollment_device(record, device, actor=actor)
            # The pairing code binds this enrollment to one device and key; the
            # challenge must later be signed by exactly that key.
            record.status = "paired"
            record.device_id = device_id
            record.public_key = public_key
            self._event("device.enrollment_completed", actor, {"enrollment_id": enrollment_id, "device_id": device_id, "organization_id": record.organization_id})
            self._persist()
            result = self._record(record)
            result.pop("pairing_code_hash", None)
            return result

    def _check_enrollment_device(self, enrollment: DeviceEnrollmentRecord, device: DeviceRecord, *, actor: str) -> None:
        check_enrollment_device(enrollment, device, actor=actor, normalize_org=self._normalize_org_id)

    def _require_paired_enrollment_locked(self, enrollment: DeviceEnrollmentRecord, *, device_id: str) -> None:
        if enrollment.status == "paired" and enrollment.expires_at < _now():
            enrollment.status = "expired"
            self._persist()
            raise ValueError("enrollment expired")
        if enrollment.status != "paired":
            raise ValueError("enrollment is not awaiting device verification")
        if not device_id or device_id != enrollment.device_id:
            raise PermissionError("device does not match the enrollment")

    def issue_device_challenge(self, *, actor: str, enrollment_id: str, device_id: str) -> dict[str, Any]:
        with self._lock:
            record = self.device_enrollments.get(enrollment_id)
            if not record:
                raise KeyError("enrollment not found")
            self._require_actor_org(actor, record.organization_id)
            self._require_paired_enrollment_locked(record, device_id=device_id)
            device = self.devices.get(device_id)
            if not device:
                raise KeyError("device not found")
            self._check_enrollment_device(record, device, actor=actor)
            nonce = secrets.token_urlsafe(32)
            challenge_id = _id("chal")
            nonce_hash = _hash_secret(nonce, purpose="device_challenge_nonce")
            challenge = DeviceChallengeRecord(challenge_id=challenge_id, enrollment_id=enrollment_id, device_id=device_id, nonce_hash=nonce_hash, organization_id=record.organization_id)
            self.device_challenges[challenge_id] = challenge
            self._event("device.challenge_issued", actor, {"challenge_id": challenge_id, "enrollment_id": enrollment_id, "device_id": device_id, "organization_id": record.organization_id})
            self._persist()
            result = self._record(challenge)
            result["nonce"] = nonce
            result["signing_message"] = _device_signing_message(challenge_id=challenge_id, nonce_hash=nonce_hash).decode("utf-8")
            result["signature_algorithms"] = ["ed25519", "p256_ecdsa_sha256"]
            result.pop("nonce_hash", None)
            return result

    def verify_device_challenge(self, *, actor: str, enrollment_id: str, challenge_id: str, device_id: str, signature: str, allow_legacy_hmac: bool = True) -> dict[str, Any]:
        with self._lock:
            challenge = self.device_challenges.get(challenge_id)
            if not challenge or challenge.enrollment_id != enrollment_id or challenge.device_id != device_id:
                raise KeyError("challenge not found")
            self._require_actor_org(actor, challenge.organization_id)
            if challenge.status != "pending":
                raise ValueError("challenge already used")
            if challenge.expires_at < _now():
                challenge.status = "expired"
                self._persist()
                raise ValueError("challenge expired")
            enrollment = self.device_enrollments.get(enrollment_id)
            if not enrollment:
                raise KeyError("enrollment not found")
            if self._normalize_org_id(enrollment.organization_id) != self._normalize_org_id(challenge.organization_id):
                raise PermissionError("challenge belongs to a different organization")
            self._require_paired_enrollment_locked(enrollment, device_id=device_id)
            device = self.devices.get(device_id)
            if not device:
                raise KeyError("device not found")
            self._check_enrollment_device(enrollment, device, actor=actor)
            # Verify against the key bound by the pairing code, never a key the
            # device registered on its own.
            public_key = enrollment.public_key or ""
            if self._legacy_hmac_refused(public_key, allow_legacy_hmac=allow_legacy_hmac):
                raise PermissionError(LEGACY_HMAC_KEY_REFUSED)
            message = _device_signing_message(challenge_id=challenge.challenge_id, nonce_hash=challenge.nonce_hash)
            if not _verify_device_signature(public_key, message, signature):
                raise ValueError("device challenge signature mismatch")
            now = _now()
            challenge.status = "verified"
            challenge.verified_at = now
            token = secrets.token_urlsafe(32)
            token_hash = _hash_secret(token, purpose="device_token")
            device.public_key = public_key
            device.credential_status = "verified"
            device.trust_level = "challenge_verified"
            device.token_hash = token_hash
            device.last_challenge_nonce_hash = challenge.nonce_hash
            device.last_challenge_expires_at = challenge.expires_at
            device.updated_at = now
            # Single use: the enrollment and its outstanding challenges are spent.
            enrollment.status = "completed"
            enrollment.completed_at = now
            for other in self.device_challenges.values():
                if other.enrollment_id == enrollment_id and other.status == "pending":
                    other.status = "expired"
            self._event("device.challenge_verified", actor, {"challenge_id": challenge_id, "enrollment_id": enrollment_id, "device_id": device_id, "algorithm": "asymmetric", "organization_id": challenge.organization_id})
            self._persist()
            return {"device_id": device_id, "device_token": token, "token_hash": token_hash, "trust_level": "challenge_verified"}

    def rotate_device_token(self, *, actor: str, device_id: str) -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(device_id)
            if not device:
                raise KeyError("device not found")
            self._require_actor_org(actor, device.organization_id)
            if device.credential_status == "revoked":
                raise ValueError("device revoked")
            token = secrets.token_urlsafe(32)
            device.token_hash = _hash_secret(token, purpose="device_token")
            device.updated_at = _now()
            self._event("device.token_rotated", actor, {"device_id": device_id, "organization_id": device.organization_id})
            self._persist()
            result = self._record(device)
            result["device_token"] = token
            return result

    def revoke_device(self, *, actor: str, device_id: str, reason: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(device_id)
            if not device:
                raise KeyError("device not found")
            self._require_actor_org(actor, device.organization_id)
            device.credential_status = "revoked"
            device.trust_level = "revoked"
            device.revoked_at = _now()
            device.online = False
            device.updated_at = device.revoked_at
            # A lost or revoked device gets no further pushes, including queued ones.
            device.push_token = None
            for item in self.push_outbox.values():
                if item.device_id == device_id and item.status == "pending":
                    item.status = "failed"
                    item.last_error = "device_revoked"
                    item.updated_at = device.revoked_at
            self._event(
                "device.revoked",
                actor,
                {"device_id": device_id, "reason": reason, "organization_id": device.organization_id},
            )
            self._persist()
            return self._record(device)

    def pending_push_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            values: list[PushOutboxRecord] = []
            dropped = False
            for item in self.push_outbox.values():
                if item.status != "pending":
                    continue
                device = self.devices.get(item.device_id)
                if device is None or not self._device_receives_push(device):
                    # Queued while the device was unverified, or before it was revoked.
                    item.status = "failed"
                    item.last_error = "device_not_eligible"
                    item.updated_at = _now()
                    dropped = True
                    continue
                values.append(item)
            if dropped:
                self._persist()
            return [self._record(v) for v in sorted(values, key=lambda item: item.created_at)[: max(1, min(limit, 500))]]

    def claim_push_deliveries(self, *, worker_id: str, limit: int = 50, lease_seconds: float = 60.0, max_attempts: int = 5, max_age_seconds: float = 86_400.0) -> list[dict[str, Any]]:
        """Leases due pushes to one dispatcher, each with its device's current push target.

        A push whose device is no longer eligible, that is older than
        ``max_age_seconds``, or whose lease expired ``max_attempts`` times is
        given up instead. The lease holder reports with complete_push_delivery.
        """
        with self._lock:
            now = _now()
            claimed: list[dict[str, Any]] = []
            changed = False
            for item in sorted(self.push_outbox.values(), key=lambda value: value.created_at):
                lease_expired = item.status == "sending" and (item.lease_expires_at or 0.0) <= now
                if not (lease_expired or (item.status == "pending" and item.next_attempt_at <= now)):
                    continue
                device = self.devices.get(item.device_id)
                if device is None or not self._device_receives_push(device):
                    reason: Optional[str] = "device_not_eligible"
                elif item.created_at <= now - max_age_seconds:
                    reason = "expired"
                elif item.attempt_count >= max_attempts:
                    reason = "max_attempts"
                else:
                    reason = None
                if reason is not None or device is None:
                    item.status, item.last_error, item.claimed_by, item.lease_expires_at, item.updated_at = "failed", reason, None, None, now
                    changed = True
                    continue
                if len(claimed) >= max(1, limit):
                    continue
                item.status, item.claimed_by, item.lease_expires_at, item.updated_at = "sending", worker_id, now + lease_seconds, now
                item.attempt_count += 1
                changed = True
                claimed.append({**self._record(item), "push_token": device.push_token})
            if changed:
                self._persist()
            return claimed

    def complete_push_delivery(self, *, push_id: str, worker_id: str, outcome: PushOutcome, provider: str, error: Optional[str] = None, provider_message_id: Optional[str] = None, retry_at: Optional[float] = None, invalid_target: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Records the lease holder's result; returns None if the lease was lost.

        ``invalid_target`` is the push target the provider no longer knows; the
        device stops receiving pushes until the app registers a new one.
        """
        with self._lock:
            item = self.push_outbox.get(push_id)
            if item is None or item.status != "sending" or item.claimed_by != worker_id:
                return None
            now = _now()
            item.status = "pending" if outcome == "retry" else outcome
            if outcome == "retry":
                item.next_attempt_at = float(retry_at if retry_at is not None else now)
            item.last_error, item.provider, item.provider_message_id = error, provider, provider_message_id
            item.claimed_by, item.lease_expires_at, item.updated_at = None, None, now
            device = self.devices.get(item.device_id)
            if invalid_target and device is not None and device.push_token == invalid_target:
                device.push_token = None
                self._event("device.push_token_invalidated", "push-provider", {"device_id": device.device_id, "organization_id": device.organization_id})
            if outcome != "retry":
                self._event("push.delivery", "push-provider", {"push_id": push_id, "status": item.status, "organization_id": item.organization_id})
            self._persist()
            return self._record(item)

    def register_approval_key(self, *, actor: str, device_id: str, public_key: str) -> dict[str, Any]:
        public_key = (public_key or "").strip()
        if not _is_ed25519_public_key(public_key):
            raise ValueError("approval key must be an Ed25519 public key (base64:<32 bytes>)")
        with self._lock:
            device = self.devices.get(device_id)
            if not device:
                raise KeyError("device not found")
            if device.actor != actor:
                raise PermissionError("device is bound to a different actor")
            if device.revoked_at or device.credential_status == "revoked":
                raise PermissionError("device is revoked")
            if device.credential_status != "verified" or device.trust_level != "challenge_verified":
                raise PermissionError("device must be challenge-verified before registering an approval key")
            if device.device_type != "mobile":
                raise PermissionError("approval keys are only supported for mobile devices")
            if device.approval_public_key:
                if device.approval_public_key == public_key:
                    return self._record(device)
                # Write-once: a stolen device token must not be able to swap it.
                raise PermissionError("approval key already registered; re-enroll the device to replace it")
            device.approval_public_key = public_key
            device.approval_key_registered_at = _now()
            device.updated_at = _now()
            self._event("device.approval_key_registered", actor, {"device_id": device_id, "organization_id": device.organization_id})
            self._persist()
            return self._record(device)

    def device_approval_key(self, device_id: str) -> Optional[str]:
        with self._lock:
            device = self.devices.get(device_id)
            return device.approval_public_key if device else None

    def verify_approval_signature(
        self,
        *,
        device_id: str,
        approval_id: str,
        decision: str,
        timestamp: str,
        nonce: str,
        signature: str,
        max_skew_seconds: int = 300,
        nonce_ttl_seconds: int = 600,
    ) -> tuple[bool, str]:
        now = _now()
        timestamp = (timestamp or "").strip()
        nonce = (nonce or "").strip()
        signature = (signature or "").strip()
        if not self.device_approval_key(device_id):
            return False, "approval_key_not_registered"
        if not timestamp or not nonce or not signature:
            return False, "missing_approval_signature"
        try:
            ts = float(timestamp)
            if ts > 10_000_000_000:
                ts = ts / 1000.0
        except ValueError:
            return False, "invalid_timestamp"
        if abs(now - ts) > max(30, int(max_skew_seconds or 300)):
            return False, "timestamp_out_of_window"
        if len(nonce) < 16 or len(nonce) > 180:
            return False, "invalid_nonce"
        with self._lock:
            device = self.devices.get(device_id)
            if not device or not device.approval_public_key:
                return False, "approval_key_not_registered"
            # Approval keys are registered as Ed25519 only; this covers rows written otherwise.
            if self._legacy_hmac_refused(device.approval_public_key):
                return False, "legacy_hmac_key_not_allowed"
            nonce_key = f"approval:{device_id}:{nonce}"
            if nonce_key in self.device_request_nonces:
                return False, "nonce_replay"
            message = _approval_decision_message(approval_id=approval_id, decision=decision, timestamp=timestamp, nonce=nonce)
            if not _verify_device_signature(device.approval_public_key, message, signature):
                return False, "approval_signature_mismatch"
            self.device_request_nonces[nonce_key] = now + max(60, int(nonce_ttl_seconds or 600))
            self._persist()
            return True, "ok"

    def verify_device_request_signature(
        self,
        *,
        device_id: str,
        method: str,
        path: str,
        body: bytes,
        timestamp: str,
        nonce: str,
        signature: str,
        expected_actor: str,
        required_device_types: set[str] | None = None,
        max_skew_seconds: int = 300,
        nonce_ttl_seconds: int = 600,
        allow_legacy_hmac: bool = True,
    ) -> tuple[bool, str]:
        """Verify a device-signed request made under ``expected_actor``'s credentials.

        The device must belong to that actor and its organization, so one actor's
        device key cannot act as the second factor for another actor's token.
        """
        now = _now()
        device_id = (device_id or "").strip()
        timestamp = (timestamp or "").strip()
        nonce = (nonce or "").strip()
        signature = (signature or "").strip()
        if not device_id:
            return False, "missing_device_id"
        if not timestamp:
            return False, "missing_timestamp"
        if not nonce:
            return False, "missing_nonce"
        if not signature:
            return False, "missing_signature"
        try:
            ts = float(timestamp)
            if ts > 10_000_000_000:  # tolerate millisecond clients
                ts = ts / 1000.0
        except ValueError:
            return False, "invalid_timestamp"
        if abs(now - ts) > max(30, int(max_skew_seconds or 300)):
            return False, "timestamp_out_of_window"
        if len(nonce) < 16 or len(nonce) > 180:
            return False, "invalid_nonce"
        with self._lock:
            device = self.devices.get(device_id)
            if not device:
                return False, "device_not_found"
            if required_device_types and device.device_type not in required_device_types:
                return False, "wrong_device_type"
            if device.revoked_at or device.credential_status == "revoked":
                return False, "device_revoked"
            if device.credential_status != "verified" or device.trust_level != "challenge_verified":
                return False, "device_not_challenge_verified"
            if not expected_actor or device.actor != expected_actor:
                return False, "device_actor_mismatch"
            if self._normalize_org_id(device.organization_id) != self._organization_for_actor(expected_actor):
                return False, "device_organization_mismatch"
            public_key = device.public_key or ""
            if not public_key:
                return False, "missing_public_key"
            if self._legacy_hmac_refused(public_key, allow_legacy_hmac=allow_legacy_hmac):
                return False, "legacy_hmac_key_not_allowed"
            # Opportunistically garbage-collect expired nonce records.
            for key, expires_at in list(self.device_request_nonces.items()):
                if expires_at <= now:
                    self.device_request_nonces.pop(key, None)
            nonce_key = f"{device_id}:{nonce}"
            if nonce_key in self.device_request_nonces:
                return False, "nonce_replay"
            body_sha256 = _sha256_hex(body or b"")
            message = _device_request_message(method=method, path=path, body_sha256=body_sha256, timestamp=timestamp, nonce=nonce)
            if not _verify_device_signature(public_key, message, signature):
                return False, "signature_mismatch"
            self.device_request_nonces[nonce_key] = now + max(60, int(nonce_ttl_seconds or 600))
            device.last_seen_at = now
            device.updated_at = now
            # No sync event: one per signed request would make the feed grow with
            # traffic. Routes count verified and rejected signatures in metrics.
            self._persist()
            return True, "ok"

    def bootstrap(self, actor: str, *, role: str = "viewer") -> dict[str, Any]:
        """``role`` is the one the gateway authenticated; the stored profile role is
        not a credential. Viewers see only their own devices."""
        with self._lock:
            user = self.ensure_user(actor)
            organization_id = self._normalize_org_id(user["organization_id"])
            sees_organization_devices = role in {"owner", "operator"}
            return {
                "organization": {**self._record(self.organization), "organization_id": organization_id},
                "user": {**user, "role": role},
                "devices": [public_device_view(self._record(v)) for v in self.devices.values() if self._normalize_org_id(v.organization_id) == organization_id and (v.actor == actor or sees_organization_devices)],
                "runtime_status": [self._record(v) for v in self.runtimes.values() if self._normalize_org_id(v.organization_id) == organization_id],
                "pending_approvals": [self._record(v) for v in self.approvals.values() if v.status == "pending" and self._normalize_org_id(v.organization_id) == organization_id],
                "notifications": [self._record(v) for v in sorted((n for n in self.notifications.values() if self._normalize_org_id(n.organization_id) == organization_id and self._notification_visible(n, actor)), key=lambda n: n.created_at, reverse=True)[:50]],
                "sync_seq": self._seq,
            }

    def list_conversations(self, actor: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.conversations.values())
            if actor:
                organization_id = self._organization_for_actor(actor)
                values = [v for v in values if v.actor == actor and self._normalize_org_id(v.organization_id) == organization_id]
            return [self._record(v) for v in sorted(values, key=lambda c: c.updated_at, reverse=True)]

    def get_conversation(
        self,
        conversation_id: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        with self._lock:
            return self._record(self._owned_conversation_locked(conversation_id, actor))

    def list_messages(self, conversation_id: str, actor: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            conversation = self.conversations.get(conversation_id)
            if not conversation:
                raise KeyError("conversation not found")
            if actor and (conversation.actor != actor or not self._actor_can_access_org(actor, conversation.organization_id)):
                return []
            # Messages another actor wrote into the conversation are never chat history.
            values = [item for item in self.messages.values() if item.conversation_id == conversation_id and item.actor == conversation.actor]
            return [self._record(v) for v in sorted(values, key=lambda m: m.created_at)]

    def get_task(self, task_id: str, actor: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError("task not found")
            if actor and not self._actor_can_access_org(actor, task.organization_id):
                raise KeyError("task not found")
            return self._task_for_actor(self._record(task), actor)

    def list_approvals(self, status: Optional[str] = None, actor: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.approvals.values())
            if actor:
                organization_id = self._organization_for_actor(actor)
                values = [v for v in values if self._normalize_org_id(v.organization_id) == organization_id]
            if status:
                values = [v for v in values if v.status == status]
            return [self._record(v) for v in sorted(values, key=lambda a: a.created_at, reverse=True)]

    def list_notifications(self, audience: Optional[str] = None, unread_only: bool = False, actor: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.notifications.values())
            if actor:
                organization_id = self._organization_for_actor(actor)
                values = [v for v in values if self._normalize_org_id(v.organization_id) == organization_id and self._notification_visible(v, actor)]
            if audience:
                values = [v for v in values if v.audience in {audience, "all"}]
            if unread_only:
                values = [v for v in values if not v.read]
            return [self._record(v) for v in sorted(values, key=lambda n: n.created_at, reverse=True)]

    def sync_since(self, since_seq: int, actor: Optional[str] = None, limit: int = SYNC_PAGE_LIMIT) -> dict[str, Any]:
        """One page of the sync feed after ``since_seq``, oldest first.

        See :func:`sync_page` for the response. The in-memory store keeps the
        newest 1000 events, so a cursor below them must re-bootstrap.
        """
        since = max(0, int(since_seq or 0))
        with self._lock:
            head = self._seq
            pruned_through = int(self.events[0].get("seq", 0)) - 1 if self.events else head
            if since > head or since < pruned_through:
                return sync_reset(head=head, pruned_through=pruned_through)
            organization_id = self._organization_for_actor(actor) if actor else None
            candidates = [
                event
                for event in self.events
                if int(event.get("seq", 0)) > since
                and (organization_id is None or self._organization_for_event(event) == organization_id)
            ]
            return sync_page(
                candidates,
                head=head,
                pruned_through=pruned_through,
                limit=limit,
                scope=(lambda event: scope_sync_event_to_actor(event, actor=actor, conversation_owner=self._conversation_owner)) if actor else None,
            )
