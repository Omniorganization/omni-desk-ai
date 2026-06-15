from __future__ import annotations

import hashlib
import hmac
import json
import base64
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


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


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
    source_device_id: Optional[str] = None
    task_id: Optional[str] = None
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
    related_id: Optional[str] = None
    read: bool = False
    created_at: float = field(default_factory=_now)


@dataclass
class RuntimeStatusRecord:
    device_id: str
    status: Literal["online", "offline", "degraded"]
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
    status: Literal["pending", "verified", "expired"] = "pending"
    created_at: float = field(default_factory=_now)
    expires_at: float = field(default_factory=lambda: _now() + 300)
    verified_at: Optional[float] = None


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

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _event(self, event_type: str, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        event = {"seq": self._seq, "ts": _now(), "event_type": event_type, "actor": actor, "payload": payload}
        self.events.append(event)
        self.events = self.events[-1000:]
        return event

    @staticmethod
    def _record(obj: Any) -> dict[str, Any]:
        return asdict(obj)

    def _idempotency_scope(self, *, actor: str, endpoint: str, key: str) -> str:
        return f"{self.organization.organization_id}:{actor or 'unknown'}:{endpoint}:{key}"

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
        scoped = self._idempotency_scope(actor=actor, endpoint=endpoint, key=key)
        self.idempotency[scoped] = {
            "organization_id": self.organization.organization_id,
            "actor": actor or "unknown",
            "endpoint": endpoint,
            "key": key,
            "payload_hash": _fingerprint(payload),
            "response": response,
            "created_at": _now(),
        }

    def ensure_user(self, actor: str, role: str = "operator", display_name: Optional[str] = None) -> dict[str, Any]:
        actor = actor or "unknown"
        with self._lock:
            if actor not in self.users:
                self.users[actor] = UserProfile(user_id=actor, display_name=display_name or actor, role=role)
                self._event("user.created", actor, {"user_id": actor, "role": role})
                self._persist()
            return self._record(self.users[actor])

    def register_device(self, *, actor: str, device_id: Optional[str], device_type: DeviceType, name: str, platform: str, push_token: Optional[str] = None, capabilities: Optional[list[str]] = None, public_key: Optional[str] = None, token_hash: Optional[str] = None, organization_id: str = "org_default", idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        actor = actor or "unknown"
        with self._lock:
            cached = self._idempotency_get(actor=actor, endpoint="devices.register", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached
            self.ensure_user(actor)
            device_id = device_id or _id("dev")
            now = _now()
            existing = self.devices.get(device_id)
            if existing:
                existing.device_type = device_type
                existing.name = name or existing.name
                existing.platform = platform or existing.platform
                existing.actor = actor
                existing.organization_id = organization_id or existing.organization_id
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
                device = DeviceRecord(device_id=device_id, device_type=device_type, name=name, platform=platform, actor=actor, organization_id=organization_id or "org_default", push_token=push_token, public_key=public_key, token_hash=token_hash, credential_status="enrolled" if public_key else "pending", trust_level="public_key_registered" if public_key else "unverified", capabilities=list(capabilities or []))
                self.devices[device_id] = device
            self._event("device.registered", actor, {"device_id": device_id, "device_type": device_type})
            result = self._record(device)
            self._idempotency_put(actor=actor, endpoint="devices.register", key=idempotency_key, payload=idempotency_payload, response=result)
            self._persist()
            return result

    def create_conversation(self, *, actor: str, title: str, source_device_id: Optional[str] = None, idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            cached = self._idempotency_get(actor=actor, endpoint="conversations.create", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached
            self.ensure_user(actor)
            cid = _id("conv")
            convo = ConversationRecord(conversation_id=cid, title=title or "New conversation", actor=actor, source_device_id=source_device_id)
            self.conversations[cid] = convo
            self._event("conversation.created", actor, {"conversation_id": cid, "title": convo.title})
            result = self._record(convo)
            self._idempotency_put(actor=actor, endpoint="conversations.create", key=idempotency_key, payload=idempotency_payload, response=result)
            self._persist()
            return result

    def add_message_and_task(self, *, actor: str, conversation_id: str, content: str, source_device_id: Optional[str] = None, requires_desktop_runtime: bool = False, risk: str = "medium", idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            cached = self._idempotency_get(actor=actor, endpoint="messages.create", key=idempotency_key, payload=idempotency_payload)
            if cached is not None:
                return cached
            if conversation_id not in self.conversations:
                raise KeyError("conversation not found")
            mid = _id("msg")
            task_id = _id("task")
            message = MessageRecord(message_id=mid, conversation_id=conversation_id, role="user", content=content, actor=actor, source_device_id=source_device_id, task_id=task_id)
            task = TaskRecord(
                task_id=task_id,
                conversation_id=conversation_id,
                title=(content or "Task")[:120],
                actor=actor,
                status="queued",
                requires_desktop_runtime=bool(requires_desktop_runtime),
                idempotency_key=idempotency_key,
            )
            self.messages[mid] = message
            self.tasks[task_id] = task
            self._event("message.created", actor, {"message_id": mid, "conversation_id": conversation_id, "task_id": task_id})
            self._event("task.created", actor, {"task_id": task_id, "conversation_id": conversation_id, "requires_desktop_runtime": requires_desktop_runtime})
            if risk in {"high", "critical"} or requires_desktop_runtime:
                approval = self.create_approval_locked(task_id=task_id, actor=actor, risk="high" if risk not in {"critical"} else "critical", action=task.title, reason="Desktop/runtime action requires cross-device approval")
                task.status = "blocked"
                task.approval_id = approval["approval_id"]
            result = {"message": self._record(message), "task": self._record(task), "approval": self._record(self.approvals[task.approval_id]) if task.approval_id else None}
            self._idempotency_put(actor=actor, endpoint="messages.create", key=idempotency_key, payload=idempotency_payload, response=result)
            self._persist()
            return result

    def create_approval_locked(self, *, task_id: str, actor: str, risk: Literal["low", "medium", "high", "critical"], action: str, reason: str) -> dict[str, Any]:
        approval_id = _id("appr")
        approval = ApprovalRecord(approval_id=approval_id, task_id=task_id, risk=risk, action=action, reason=reason, requested_by=actor, expires_at=_now() + 900)
        self.approvals[approval_id] = approval
        self._event("approval.requested", actor, {"approval_id": approval_id, "task_id": task_id, "risk": risk})
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
            if approval.status != "pending":
                raise ValueError("approval already decided")
            if approval.expires_at and approval.expires_at < _now():
                approval.status = "expired"
                self._event("approval.expired", actor, {"approval_id": approval_id, "task_id": approval.task_id})
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
            self._event("approval.decided", actor, {"approval_id": approval_id, "decision": decision, "task_id": approval.task_id})
            self._notify_locked(audience="desktop", title=f"Approval {decision}", body=approval.action[:180], event_type="approval.decided", actor=actor, related_id=approval.task_id)
            result = self._record(approval)
            self._idempotency_put(actor=actor, endpoint="approvals.decide", key=idempotency_key, payload=idempotency_payload, response={"approval": result})
            self._persist()
            return result

    def heartbeat_runtime(self, *, actor: str, device_id: str, status: Literal["online", "offline", "degraded"], version: Optional[str] = None, hostname: Optional[str] = None, active_task_id: Optional[str] = None, capabilities: Optional[list[str]] = None) -> dict[str, Any]:
        with self._lock:
            record = RuntimeStatusRecord(device_id=device_id, status=status, version=version, hostname=hostname, active_task_id=active_task_id, capabilities=list(capabilities or []))
            self.runtimes[device_id] = record
            if device_id in self.devices:
                self.devices[device_id].online = status == "online"
                self.devices[device_id].last_seen_at = record.last_heartbeat_at
                self.devices[device_id].updated_at = record.last_heartbeat_at
            self._event("runtime.heartbeat", actor, {"device_id": device_id, "status": status, "active_task_id": active_task_id})
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
            for task in sorted(self.tasks.values(), key=lambda item: item.created_at):
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
                self._event("task.claimed", actor, {"task_id": task.task_id, "device_id": device_id, "lease_expires_at": task.lease_expires_at, "capabilities": list(capabilities or [])})
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
            task.status = status
            task.result_summary = result_summary or task.result_summary
            task.assigned_runtime_device_id = assigned_runtime_device_id or task.assigned_runtime_device_id
            task.updated_at = _now()
            self._event("task.status", actor, {"task_id": task_id, "status": status})
            if status in {"completed", "failed", "cancelled"}:
                task.lease_expires_at = None
                self._notify_locked(audience="all", title=f"Task {status}", body=task.title[:180], event_type="task.status", actor=actor, related_id=task_id)
            result = self._record(task)
            self._idempotency_put(actor=actor, endpoint="tasks.status", key=idempotency_key, payload=idempotency_payload, response=result)
            self._persist()
            return result

    def _notify_locked(self, *, audience: Literal["desktop", "mobile", "web_admin", "all"], title: str, body: str, event_type: str, actor: str, related_id: Optional[str]) -> dict[str, Any]:
        nid = _id("notif")
        notification = NotificationRecord(notification_id=nid, audience=audience, title=title, body=body, event_type=event_type, actor=actor, related_id=related_id)
        self.notifications[nid] = notification
        for device in self.devices.values():
            if not device.push_token:
                continue
            if audience not in {"all", device.device_type}:
                continue
            push_id = _id("push")
            self.push_outbox[push_id] = PushOutboxRecord(push_id=push_id, device_id=device.device_id, platform=device.platform, audience=audience, title=title, body=body, related_id=related_id)
        self._event("notification.created", actor, {"notification_id": nid, "audience": audience, "event_type": event_type, "related_id": related_id})
        return self._record(notification)

    def register_push_token(self, *, actor: str, device_id: str, push_token: str, platform: str = "unknown") -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(device_id)
            if not device:
                raise KeyError("device not found")
            device.push_token = push_token
            device.platform = platform or device.platform
            device.last_seen_at = _now()
            device.updated_at = device.last_seen_at
            self._event("device.push_token_registered", actor, {"device_id": device_id, "platform": platform})
            self._persist()
            return self._record(device)

    def start_device_enrollment(self, *, actor: str, device_type: DeviceType, pairing_code: str) -> dict[str, Any]:
        with self._lock:
            if device_type not in {"desktop", "mobile", "web_admin"}:
                raise ValueError("invalid device_type")
            enrollment_id = _id("enroll")
            record = DeviceEnrollmentRecord(
                enrollment_id=enrollment_id,
                device_type=device_type,
                requested_by=actor or "unknown",
                pairing_code_hash=_hash_secret(pairing_code),
            )
            self.device_enrollments[enrollment_id] = record
            self._event("device.enrollment_started", actor, {"enrollment_id": enrollment_id, "device_type": device_type})
            self._persist()
            result = self._record(record)
            result.pop("pairing_code_hash", None)
            return result

    def complete_device_enrollment(self, *, actor: str, enrollment_id: str, pairing_code: str, device_id: str, public_key: str) -> dict[str, Any]:
        with self._lock:
            record = self.device_enrollments.get(enrollment_id)
            if not record:
                raise KeyError("enrollment not found")
            if record.status != "pending":
                raise ValueError("enrollment is not pending")
            if record.expires_at < _now():
                record.status = "expired"
                self._persist()
                raise ValueError("enrollment expired")
            if record.pairing_code_hash != _hash_secret(pairing_code):
                raise ValueError("pairing code mismatch")
            record.status = "completed"
            record.device_id = device_id
            record.public_key = public_key
            record.completed_at = _now()
            self._event("device.enrollment_completed", actor, {"enrollment_id": enrollment_id, "device_id": device_id})
            self._persist()
            result = self._record(record)
            result.pop("pairing_code_hash", None)
            return result

    def issue_device_challenge(self, *, actor: str, enrollment_id: str, device_id: str) -> dict[str, Any]:
        with self._lock:
            record = self.device_enrollments.get(enrollment_id)
            if not record:
                raise KeyError("enrollment not found")
            if record.status not in {"pending", "completed"}:
                raise ValueError("enrollment is not active")
            nonce = secrets.token_urlsafe(32)
            challenge_id = _id("chal")
            nonce_hash = _hash_secret(nonce)
            challenge = DeviceChallengeRecord(challenge_id=challenge_id, enrollment_id=enrollment_id, device_id=device_id, nonce_hash=nonce_hash)
            self.device_challenges[challenge_id] = challenge
            self._event("device.challenge_issued", actor, {"challenge_id": challenge_id, "enrollment_id": enrollment_id, "device_id": device_id})
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
            public_key = (device.public_key if device else None) or enrollment.public_key or ""
            message = _device_signing_message(challenge_id=challenge.challenge_id, nonce_hash=challenge.nonce_hash)
            if not _verify_device_signature(public_key, message, signature):
                raise ValueError("device challenge signature mismatch")
            challenge.status = "verified"
            challenge.verified_at = _now()
            token = secrets.token_urlsafe(32)
            token_hash = _hash_secret(token)
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
            self._event("device.challenge_verified", actor, {"challenge_id": challenge_id, "enrollment_id": enrollment_id, "device_id": device_id, "algorithm": "asymmetric"})
            self._persist()
            return {"device_id": device_id, "device_token": token, "token_hash": token_hash, "trust_level": "challenge_verified"}

    def rotate_device_token(self, *, actor: str, device_id: str) -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(device_id)
            if not device: raise KeyError("device not found")
            if device.credential_status == "revoked": raise ValueError("device revoked")
            token = secrets.token_urlsafe(32); device.token_hash = _hash_secret(token); device.updated_at = _now(); self._event("device.token_rotated", actor, {"device_id": device_id}); self._persist(); result = self._record(device); result["device_token"] = token; return result

    def revoke_device(self, *, actor: str, device_id: str, reason: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(device_id)
            if not device: raise KeyError("device not found")
            device.credential_status = "revoked"; device.trust_level = "revoked"; device.revoked_at = _now(); device.online = False; device.updated_at = device.revoked_at; self._event("device.revoked", actor, {"device_id": device_id, "reason": reason}); self._persist(); return self._record(device)

    def pending_push_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            values = [v for v in self.push_outbox.values() if v.status == "pending"]
            return [self._record(v) for v in sorted(values, key=lambda item: item.created_at)[: max(1, min(limit, 500))]]

    def mark_push_delivery(self, *, push_id: str, status: Literal["sent", "failed"], error: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            item = self.push_outbox.get(push_id)
            if not item: raise KeyError("push outbox item not found")
            item.status = status; item.attempt_count += 1; item.last_error = error; item.updated_at = _now(); self._event("push.delivery", "push-provider", {"push_id": push_id, "status": status}); self._persist(); return self._record(item)

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
            return {
                "organization": self._record(self.organization),
                "user": user,
                "devices": [self._record(v) for v in self.devices.values() if v.actor == actor or user["role"] in {"owner", "operator"}],
                "runtime_status": [self._record(v) for v in self.runtimes.values()],
                "pending_approvals": [self._record(v) for v in self.approvals.values() if v.status == "pending"],
                "notifications": [self._record(v) for v in sorted(self.notifications.values(), key=lambda n: n.created_at, reverse=True)[:50]],
                "sync_seq": self._seq,
            }

    def list_conversations(self, actor: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.conversations.values())
            if actor:
                values = [v for v in values if v.actor == actor]
            return [self._record(v) for v in sorted(values, key=lambda c: c.updated_at, reverse=True)]

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError("task not found")
            return self._record(task)

    def list_approvals(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.approvals.values())
            if status:
                values = [v for v in values if v.status == status]
            return [self._record(v) for v in sorted(values, key=lambda a: a.created_at, reverse=True)]

    def list_notifications(self, audience: Optional[str] = None, unread_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.notifications.values())
            if audience:
                values = [v for v in values if v.audience in {audience, "all"}]
            if unread_only:
                values = [v for v in values if not v.read]
            return [self._record(v) for v in sorted(values, key=lambda n: n.created_at, reverse=True)]

    def sync_since(self, since_seq: int) -> dict[str, Any]:
        with self._lock:
            events = [event for event in self.events if int(event.get("seq", 0)) > since_seq]
            return {"sync_seq": self._seq, "events": events}
