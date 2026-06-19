from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from omnidesk_agent.channels.capability_matrix import evaluate_channel_action

TrustLevel = Literal["unknown", "pairing_required", "paired", "verified", "trusted", "blocked"]
IdentityDecisionCode = Literal[
    "allow",
    "pairing_required",
    "reverification_required",
    "owner_approval_required",
    "blocked",
]


@dataclass(frozen=True)
class SenderIdentity:
    channel: str
    sender_id: str
    trust_level: TrustLevel = "pairing_required"
    oauth_subject_hash: str | None = None
    device_id: str | None = None
    paired_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    history: tuple[dict[str, Any], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.sender_id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IdentityDecision:
    decision: IdentityDecisionCode
    trust_level: TrustLevel
    reason: str
    requires_pairing_code: bool = False
    requires_owner_approval: bool = False
    required_controls: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hash_identity(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SenderIdentityStore:
    """Small JSON-backed identity firewall for channel ingress.

    The store is intentionally conservative: unknown senders and OAuth drift do
    not become trusted implicitly. Runtime adapters can call `evaluate()` before
    handing a message to the planner, and operators can call `pair_sender()`
    after a successful pairing challenge.
    """

    def __init__(self, path: Path):
        self.path = path.expanduser()
        self._items: dict[str, SenderIdentity] = {}
        self._load()

    def pair_sender(
        self,
        *,
        channel: str,
        sender_id: str,
        oauth_subject: str | None = None,
        device_id: str | None = None,
        trust_level: TrustLevel = "paired",
    ) -> SenderIdentity:
        if trust_level in {"unknown", "pairing_required"}:
            raise ValueError("paired sender must have paired or stronger trust")
        existing = self.get(channel, sender_id)
        history = list(existing.history if existing else ())
        history.append({"event": "paired", "at": time.time(), "trust_level": trust_level, "device_id": device_id})
        identity = SenderIdentity(
            channel=channel,
            sender_id=sender_id,
            trust_level=trust_level,
            oauth_subject_hash=_hash_identity(oauth_subject),
            device_id=device_id,
            history=tuple(history[-20:]),
        )
        self._items[identity.key] = identity
        self._save()
        return identity

    def block_sender(self, channel: str, sender_id: str, *, reason: str) -> SenderIdentity:
        existing = self.get(channel, sender_id)
        history = list(existing.history if existing else ())
        history.append({"event": "blocked", "at": time.time(), "reason": reason})
        identity = SenderIdentity(channel=channel, sender_id=sender_id, trust_level="blocked", history=tuple(history[-20:]))
        self._items[identity.key] = identity
        self._save()
        return identity

    def get(self, channel: str, sender_id: str) -> SenderIdentity | None:
        return self._items.get(f"{channel}:{sender_id}")

    def evaluate(
        self,
        *,
        channel: str,
        sender_id: str,
        action: str = "receive_message",
        channel_signature_verified: bool = False,
        oauth_subject: str | None = None,
        device_id: str | None = None,
        risk: Literal["low", "medium", "high", "critical"] = "medium",
    ) -> IdentityDecision:
        capability = evaluate_channel_action(channel, action, risk=risk)
        controls = tuple(capability.get("required_controls", ()))
        if not capability["allowed"]:
            return IdentityDecision("blocked", "blocked", str(capability["reason"]), required_controls=controls, requires_owner_approval=True)
        if not channel_signature_verified and "webhook_signature_or_oauth" in controls:
            return IdentityDecision("pairing_required", "pairing_required", "channel signature or OAuth verification is required", True, False, controls)
        identity = self.get(channel, sender_id)
        if identity is None:
            return IdentityDecision("pairing_required", "pairing_required", "unknown sender must pair before execution", True, False, controls)
        if identity.trust_level == "blocked":
            return IdentityDecision("blocked", "blocked", "sender is blocked", False, True, controls)
        if identity.oauth_subject_hash and _hash_identity(oauth_subject) != identity.oauth_subject_hash:
            return IdentityDecision("reverification_required", identity.trust_level, "OAuth subject changed; identity drift detected", True, True, controls)
        if identity.device_id and device_id and identity.device_id != device_id:
            return IdentityDecision("reverification_required", identity.trust_level, "device binding changed; identity drift detected", True, True, controls)
        if capability["requires_owner_approval"]:
            return IdentityDecision("owner_approval_required", identity.trust_level, "high-risk channel action requires owner approval", False, True, controls)
        return IdentityDecision("allow", identity.trust_level, "sender identity and channel action accepted", False, False, controls)

    def to_dict(self) -> dict[str, Any]:
        return {key: value.to_dict() for key, value in sorted(self._items.items())}

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._items = {
            key: SenderIdentity(
                channel=value["channel"],
                sender_id=value["sender_id"],
                trust_level=value.get("trust_level", "pairing_required"),
                oauth_subject_hash=value.get("oauth_subject_hash"),
                device_id=value.get("device_id"),
                paired_at=float(value.get("paired_at", time.time())),
                last_seen_at=float(value.get("last_seen_at", time.time())),
                history=tuple(value.get("history", ())),
            )
            for key, value in raw.items()
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
