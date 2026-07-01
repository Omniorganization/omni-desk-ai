from __future__ import annotations

import hashlib
import hmac
import json
import base64
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal, Optional

DeviceType = Literal["desktop", "mobile", "web_admin"]
ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]
TaskStatus = Literal["queued", "running", "blocked", "completed", "failed", "cancelled"]


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


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload or b"").hexdigest()


def _device_request_message(*, method: str, path: str, body_sha256: str, timestamp: str, nonce: str) -> bytes:
    return f"omnidesk-device-request:v1:{method.upper()}:{path}:{body_sha256}:{timestamp}:{nonce}".encode("utf-8")


def _verify_device_signature(public_key: str, message: bytes, signature: str) -> bool:
    """Verify device challenge signatures.

    Production GA path: Ed25519 or P-256 public key in PEM/base64 plus base64/hex signature.
    Legacy HMAC is accepted only for keys prefixed with ``legacy-hmac:`` so old test
    fixtures cannot accidentally masquerade as production device credentials.
    """
    public_key = (public_key or "").strip()
    signature = (signature or "").strip()
    if not public_key or not signature:
        return False
    if public_key.startswith("legacy-hmac:"):
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
    status: Literal["pending", "completed", "expired", "revoked"] = "pending"
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
    status: Literal["pending", "sent", "failed"] = "pending"
    attempt_count: int = 0
    last_error: Optional[str] = None
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

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
    status: Literal["pending", "syncing", "synced", "failed", "conflict"] = "pending"
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

    def _organization_for_actor(self, actor: Optional[str]) -> str:
        user = self.users.get(actor or "unknown")
        return self._normalize_org_id(user.organization_id if user else None)

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
            self.network_state = {"state": state, "reason": reason, "updated_at": _now()}
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
                if remote_event_id in self.local_inbox:
                    duplicates += 1
                    self.local_inbox[remote_event_id].status = "duplicate"
                    continue
                key = str(event.get("idempotency_key") or payload.get("idempotency_key") or "")
                payload_hash = _fingerprint(payload)
                matched = [
                    item for item in self.local_outbox.values()
                    if key and item.idempotency_key == key and item.status in {"pending", "failed", "syncing"}
                ]
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
                    self.local_inbox[remote_event_id] = LocalInboxRecord(remote_event_id=remote_event_id, event_type=str(event.get("event_type") or "event"), payload=payload, actor=str(event.get("actor") or actor), organization_id=organization_id, source_device_id=device_id, idempotency_key=key or None, payload_hash=payload_hash, status="conflict")
                    continue
                inbox = LocalInboxRecord(remote_event_id=remote_event_id, event_type=str(event.get("event_type") or "event"), payload=payload, actor=str(event.get("actor") or actor), organization_id=organization_id, source_device_id=device_id, idempotency_key=key or None, payload_hash=payload_hash, status="applied")
                self.local_inbox[remote_event_id] = inbox
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
                },
                "inbox": {"received": len(inbox)},
                "conflicts": {"open": sum(1 for item in conflicts if item.status == "open")},
                "cursors": [self._record(cursor) for cursor in self.sync_cursors.values() if not actor or self._normalize_org_id(cursor.organization_id) == self._organization_for_actor(actor)],
            }

    def ensure_user(self, actor: str, role: str = "operator", display_name: Optional[str] = None, organization_id: Optional[str] = None) -> dict[str, Any]:
        actor = actor or "unknown"
        requested_organization_id = organization_id
        organization_id = self._normalize_org_id(organization_id)
        with self._lock:
            if actor not in self.users:
                self.users[actor] = UserProfile(user_id=actor, display_name=display_name or actor, role=role, organization_id=organization_id)
                self._event("user.created", actor, {"user_id": actor, "role": role, "organization_id": organization_id})
                self._persist()
            elif requested_organization_id is not None and self._normalize_org_id(self.users[actor].organization_id) != organization_id:
                raise PermissionError("actor is already bound to a different organization")
            return self._record(self.users[actor])

    def register_device(self, *, actor: str, device_id: Optional[str], device_type: DeviceType, name: str, platform: str, push_token: Optional[str] = None, capabilities: Optional[list[str]] = None, public_key: Optional[str] = None, token_hash: Optional[str] = None, organization_id: str = "org_default", idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        actor = actor or "unknown"
        organization_id = self._normalize_org_id(organization_id)
        with self._lock:
            self.ensure_user(actor, organization_id=organization_id)
            cached = self._idempotency_get(actor=actor, endpoint="devices.register", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached
            device_id = device_id or _id("dev")
            now = _now()
            existing = self.devices.get(device_id)
            if existing:
                if self._normalize_org_id(existing.organization_id) != organization_id:
                    raise PermissionError("device is already bound to a different organization")
                existing.device_type = device_type
                existing.name = name or existing.name
                existing.platform = platform or existing.platform
                existing.actor = actor
                existing.organization_id = organization_id
                existing.push_token = push_token or existing.push_token
                existing.public_key = public_key or existing.public_key
                existing.token_hash = token_hash or existing.token_hash
                if public_key:
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

    def get_idempotency_response(self, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None = None) -> Optional[Any]:
        with self._lock:
            return self._idempotency_get(actor=actor, endpoint=endpoint, key=key, payload=payload)

    def put_idempotency_response(self, *, actor: str, endpoint: str, key: Optional[str], payload: dict[str, Any] | None, response: Any) -> None:
        with self._lock:
            self._idempotency_put(actor=actor, endpoint=endpoint, key=key, payload=payload, response=response)
            self._persist()

    def add_chat_user_message(self, *, actor: str, conversation_id: str, content: str, source_device_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            conversation = self.conversations.get(conversation_id)
            if not conversation:
                raise KeyError("conversation not found")
            self._require_actor_org(actor, conversation.organization_id)
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
            conversation = self.conversations.get(conversation_id)
            if not conversation:
                raise KeyError("conversation not found")
            self._require_actor_org(actor, conversation.organization_id)
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

    def add_message_and_task(self, *, actor: str, conversation_id: str, content: str, source_device_id: Optional[str] = None, requires_desktop_runtime: bool = False, risk: str = "medium", idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            cached = self._idempotency_get(actor=actor, endpoint="messages.create", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached
            conversation = self.conversations.get(conversation_id)
            if not conversation:
                raise KeyError("conversation not found")
            self._require_actor_org(actor, conversation.organization_id)
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
            )
            self.messages[mid] = message
            self.tasks[task_id] = task
            self._event("message.created", actor, {"message_id": mid, "conversation_id": conversation_id, "task_id": task_id, "organization_id": conversation.organization_id})
            self._event("task.created", actor, {"task_id": task_id, "conversation_id": conversation_id, "requires_desktop_runtime": requires_desktop_runtime, "organization_id": conversation.organization_id})
            if risk in {"high", "critical"} or requires_desktop_runtime:
                approval = self.create_approval_locked(task_id=task_id, actor=actor, risk="high" if risk not in {"critical"} else "critical", action=task.title, reason="Desktop/runtime action requires cross-device approval")
                task.status = "blocked"
                task.approval_id = approval["approval_id"]
            result = {"message": self._record(message), "task": self._record(task), "approval": self._record(self.approvals[task.approval_id]) if task.approval_id else None}
            self._idempotency_put(actor=actor, endpoint="messages.create", key=idempotency_key, payload=idempotency_payload, response=result)
            self._persist()
            return result

    def create_approval_locked(self, *, task_id: str, actor: str, risk: Literal["low", "medium", "high", "critical"], action: str, reason: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        organization_id = self._normalize_org_id(task.organization_id if task else self._organization_for_actor(actor))
        approval_id = _id("appr")
        approval = ApprovalRecord(approval_id=approval_id, task_id=task_id, risk=risk, action=action, reason=reason, requested_by=actor, organization_id=organization_id, expires_at=_now() + 900)
        self.approvals[approval_id] = approval
        self._event("approval.requested", actor, {"approval_id": approval_id, "task_id": task_id, "risk": risk, "organization_id": organization_id})
        self._notify_locked(audience="mobile", title="Omni approval required", body=action[:180], event_type="approval.requested", actor=actor, related_id=approval_id)
        self._notify_locked(audience="web_admin", title="High-risk action pending", body=reason, event_type="approval.requested", actor=actor, related_id=approval_id)
        return self._record(approval)

    def decide_approval(self, *, approval_id: str, actor: str, decision: Literal["approved", "rejected"], reason: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
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
            approval.status = decision
            approval.decided_by = actor
            approval.decision_reason = reason
            approval.decision_idempotency_key = idempotency_key
            approval.decided_at = _now()
            task = self.tasks.get(approval.task_id)
            if task:
                task.status = "queued" if decision == "approved" else "cancelled"
                task.updated_at = _now()
            self._event("approval.decided", actor, {"approval_id": approval_id, "decision": decision, "task_id": approval.task_id, "organization_id": approval.organization_id})
            self._notify_locked(audience="desktop", title=f"Approval {decision}", body=approval.action[:180], event_type="approval.decided", actor=actor, related_id=approval.task_id)
            result = self._record(approval)
            self._idempotency_put(actor=actor, endpoint="approvals.decide", key=idempotency_key, payload=idempotency_payload, response={"approval": result})
            self._persist()
            return result

    def heartbeat_runtime(self, *, actor: str, device_id: str, status: Literal["online", "offline", "degraded"], version: Optional[str] = None, hostname: Optional[str] = None, active_task_id: Optional[str] = None, capabilities: Optional[list[str]] = None) -> dict[str, Any]:
        with self._lock:
            organization_id = self._organization_for_device(device_id) if device_id in self.devices else self._organization_for_actor(actor)
            if active_task_id and active_task_id in self.tasks and self._normalize_org_id(self.tasks[active_task_id].organization_id) != organization_id:
                raise PermissionError("runtime cannot attach to a task from another organization")
            record = RuntimeStatusRecord(device_id=device_id, status=status, organization_id=organization_id, version=version, hostname=hostname, active_task_id=active_task_id, capabilities=list(capabilities or []))
            self.runtimes[device_id] = record
            if device_id in self.devices:
                self.devices[device_id].online = status == "online"
                self.devices[device_id].last_seen_at = record.last_heartbeat_at
                self.devices[device_id].updated_at = record.last_heartbeat_at
            self._event("runtime.heartbeat", actor, {"device_id": device_id, "status": status, "active_task_id": active_task_id, "organization_id": organization_id})
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
            for task in sorted(self.tasks.values(), key=lambda item: item.created_at):
                if self._normalize_org_id(task.organization_id) != organization_id:
                    continue
                if not task.requires_desktop_runtime:
                    continue
                if task.status not in {"queued", "running"}:
                    continue
                if task.status == "running" and task.lease_expires_at and task.lease_expires_at > now:
                    continue
                if task.approval_id:
                    approval = self.approvals.get(task.approval_id)
                    if not approval or approval.status != "approved":
                        continue
                task.status = "running"
                task.assigned_runtime_device_id = device_id
                task.claimed_by_device_id = device_id
                task.lease_expires_at = now + lease_seconds
                task.attempt_count += 1
                task.updated_at = now
                self._event("task.claimed", actor, {"task_id": task.task_id, "device_id": device_id, "lease_expires_at": task.lease_expires_at, "capabilities": list(capabilities or []), "organization_id": organization_id})
                self._persist()
                return self._record(task)
            return None

    def update_task_status(self, *, task_id: str, actor: str, status: TaskStatus, result_summary: Optional[str] = None, assigned_runtime_device_id: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            cached = self._idempotency_get(actor=actor, endpoint="tasks.status", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError("task not found")
            runtime_device_id = assigned_runtime_device_id or task.assigned_runtime_device_id or task.claimed_by_device_id
            if runtime_device_id and runtime_device_id in self.devices:
                if self._organization_for_device(runtime_device_id) != self._normalize_org_id(task.organization_id):
                    raise PermissionError("runtime device belongs to a different organization")
            else:
                self._require_actor_org(actor, task.organization_id)
            task.status = status
            task.result_summary = result_summary or task.result_summary
            task.assigned_runtime_device_id = assigned_runtime_device_id or task.assigned_runtime_device_id
            task.updated_at = _now()
            self._event("task.status", actor, {"task_id": task_id, "status": status, "organization_id": task.organization_id})
            if status in {"completed", "failed", "cancelled"}:
                task.lease_expires_at = None
                self._notify_locked(audience="all", title=f"Task {status}", body=task.title[:180], event_type="task.status", actor=actor, related_id=task_id)
            result = self._record(task)
            self._idempotency_put(actor=actor, endpoint="tasks.status", key=idempotency_key, payload=idempotency_payload, response=result)
            self._persist()
            return result

    def _notify_locked(self, *, audience: Literal["desktop", "mobile", "web_admin", "all"], title: str, body: str, event_type: str, actor: str, related_id: Optional[str]) -> dict[str, Any]:
        nid = _id("notif")
        organization_id = self._organization_for_related(related_id, actor)
        notification = NotificationRecord(notification_id=nid, audience=audience, title=title, body=body, event_type=event_type, actor=actor, organization_id=organization_id, related_id=related_id)
        self.notifications[nid] = notification
        for device in self.devices.values():
            if self._normalize_org_id(device.organization_id) != organization_id:
                continue
            if not device.push_token:
                continue
            if audience not in {"all", device.device_type}:
                continue
            push_id = _id("push")
            self.push_outbox[push_id] = PushOutboxRecord(push_id=push_id, device_id=device.device_id, platform=device.platform, audience=audience, title=title, body=body, organization_id=organization_id, related_id=related_id)
        self._event("notification.created", actor, {"notification_id": nid, "audience": audience, "event_type": event_type, "related_id": related_id, "organization_id": organization_id})
        return self._record(notification)

    def register_push_token(self, *, actor: str, device_id: str, push_token: str, platform: str = "unknown") -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(device_id)
            if not device:
                raise KeyError("device not found")
            self._require_actor_org(actor, device.organization_id)
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
            record.status = "completed"
            record.device_id = device_id
            record.public_key = public_key
            record.completed_at = _now()
            if device_id in self.devices and self._normalize_org_id(self.devices[device_id].organization_id) != self._normalize_org_id(record.organization_id):
                raise PermissionError("device belongs to a different organization")
            self._event("device.enrollment_completed", actor, {"enrollment_id": enrollment_id, "device_id": device_id, "organization_id": record.organization_id})
            self._persist()
            result = self._record(record)
            result.pop("pairing_code_hash", None)
            return result

    def issue_device_challenge(self, *, actor: str, enrollment_id: str, device_id: str) -> dict[str, Any]:
        with self._lock:
            record = self.device_enrollments.get(enrollment_id)
            if not record:
                raise KeyError("enrollment not found")
            self._require_actor_org(actor, record.organization_id)
            if record.status not in {"pending", "completed"}:
                raise ValueError("enrollment is not active")
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

    def verify_device_challenge(self, *, actor: str, enrollment_id: str, challenge_id: str, device_id: str, signature: str) -> dict[str, Any]:
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
            device = self.devices.get(device_id)
            enrollment = self.device_enrollments.get(enrollment_id)
            if not enrollment:
                raise KeyError("enrollment not found")
            if self._normalize_org_id(enrollment.organization_id) != self._normalize_org_id(challenge.organization_id):
                raise PermissionError("challenge belongs to a different organization")
            public_key = (device.public_key if device else None) or enrollment.public_key or ""
            if device and self._normalize_org_id(device.organization_id) != self._normalize_org_id(challenge.organization_id):
                raise PermissionError("device belongs to a different organization")
            message = _device_signing_message(challenge_id=challenge.challenge_id, nonce_hash=challenge.nonce_hash)
            if not _verify_device_signature(public_key, message, signature):
                raise ValueError("device challenge signature mismatch")
            challenge.status = "verified"
            challenge.verified_at = _now()
            token = secrets.token_urlsafe(32)
            token_hash = _hash_secret(token, purpose="device_token")
            if device:
                device.credential_status = "verified"
                device.trust_level = "challenge_verified"
                device.token_hash = token_hash
                device.last_challenge_nonce_hash = challenge.nonce_hash
                device.last_challenge_expires_at = challenge.expires_at
                device.updated_at = _now()
            enrollment.status = "completed"
            enrollment.device_id = device_id
            enrollment.completed_at = enrollment.completed_at or _now()
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
            self._event(
                "device.revoked",
                actor,
                {"device_id": device_id, "reason": reason, "organization_id": device.organization_id},
            )
            self._persist()
            return self._record(device)

    def pending_push_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            values = [v for v in self.push_outbox.values() if v.status == "pending"]
            return [self._record(v) for v in sorted(values, key=lambda item: item.created_at)[: max(1, min(limit, 500))]]

    def mark_push_delivery(self, *, push_id: str, status: Literal["sent", "failed"], error: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            item = self.push_outbox.get(push_id)
            if not item:
                raise KeyError("push outbox item not found")
            item.status = status
            item.attempt_count += 1
            item.last_error = error
            item.updated_at = _now()
            self._event(
                "push.delivery",
                "push-provider",
                {"push_id": push_id, "status": status, "organization_id": item.organization_id},
            )
            self._persist()
            return self._record(item)

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
        required_device_types: set[str] | None = None,
        max_skew_seconds: int = 300,
        nonce_ttl_seconds: int = 600,
    ) -> tuple[bool, str]:
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
            public_key = device.public_key or ""
            if not public_key:
                return False, "missing_public_key"
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
            self._event("device.request_signature_verified", device.actor, {"device_id": device_id, "method": method.upper(), "path": path})
            self._persist()
            return True, "ok"

    def bootstrap(self, actor: str) -> dict[str, Any]:
        with self._lock:
            user = self.ensure_user(actor)
            organization_id = self._normalize_org_id(user["organization_id"])
            return {
                "organization": {**self._record(self.organization), "organization_id": organization_id},
                "user": user,
                "devices": [self._record(v) for v in self.devices.values() if self._normalize_org_id(v.organization_id) == organization_id and (v.actor == actor or user["role"] in {"owner", "operator"})],
                "runtime_status": [self._record(v) for v in self.runtimes.values() if self._normalize_org_id(v.organization_id) == organization_id],
                "pending_approvals": [self._record(v) for v in self.approvals.values() if v.status == "pending" and self._normalize_org_id(v.organization_id) == organization_id],
                "notifications": [self._record(v) for v in sorted((n for n in self.notifications.values() if self._normalize_org_id(n.organization_id) == organization_id), key=lambda n: n.created_at, reverse=True)[:50]],
                "sync_seq": self._seq,
            }

    def list_conversations(self, actor: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.conversations.values())
            if actor:
                organization_id = self._organization_for_actor(actor)
                values = [v for v in values if v.actor == actor and self._normalize_org_id(v.organization_id) == organization_id]
            return [self._record(v) for v in sorted(values, key=lambda c: c.updated_at, reverse=True)]

    def list_messages(self, conversation_id: str, actor: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            conversation = self.conversations.get(conversation_id)
            if not conversation:
                raise KeyError("conversation not found")
            if actor and (conversation.actor != actor or not self._actor_can_access_org(actor, conversation.organization_id)):
                return []
            values = [item for item in self.messages.values() if item.conversation_id == conversation_id]
            return [self._record(v) for v in sorted(values, key=lambda m: m.created_at)]

    def get_task(self, task_id: str, actor: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError("task not found")
            if actor and not self._actor_can_access_org(actor, task.organization_id):
                raise KeyError("task not found")
            return self._record(task)

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
                values = [v for v in values if self._normalize_org_id(v.organization_id) == organization_id]
            if audience:
                values = [v for v in values if v.audience in {audience, "all"}]
            if unread_only:
                values = [v for v in values if not v.read]
            return [self._record(v) for v in sorted(values, key=lambda n: n.created_at, reverse=True)]

    def sync_since(self, since_seq: int, actor: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            events = [event for event in self.events if int(event.get("seq", 0)) > since_seq]
            if actor:
                organization_id = self._organization_for_actor(actor)
                events = [event for event in events if self._organization_for_event(event) == organization_id]
            return {"sync_seq": self._seq, "events": events}
