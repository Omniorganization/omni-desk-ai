from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

GuardDecision = Literal["allow", "approval_required", "block"]


@dataclass(frozen=True)
class CIKInput:
    capability: str
    identity_trust: str
    knowledge_sources: tuple[str, ...] = ()
    requested_action: str = "execute"
    risk: Literal["low", "medium", "high", "critical"] = "medium"


@dataclass(frozen=True)
class CIKDecision:
    decision: GuardDecision
    reasons: tuple[str, ...] = field(default_factory=tuple)
    required_controls: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DENIED_CAPABILITIES = {
    "read_secret",
    "disable_audit",
    "change_security_policy",
    "bypass_approval",
    "self_modify_runtime_policy",
}

UNTRUSTED_KNOWLEDGE_PREFIXES = ("external_unverified:", "channel_unpaired:", "memory_untrusted:")


def evaluate_cik(input: CIKInput) -> CIKDecision:
    reasons: list[str] = []
    controls = ["audit_log"]
    if input.capability in DENIED_CAPABILITIES:
        return CIKDecision("block", ("capability_guard_denied",), ("audit_log", "owner_review"))
    if input.identity_trust in {"unknown", "pairing_required", "blocked"}:
        reasons.append("identity_guard_requires_pairing")
        controls.append("operator_pairing")
    if input.identity_trust == "blocked":
        return CIKDecision("block", tuple(reasons), tuple(controls))
    if any(source.startswith(UNTRUSTED_KNOWLEDGE_PREFIXES) for source in input.knowledge_sources):
        reasons.append("knowledge_guard_untrusted_source")
        controls.append("source_attestation")
    if input.risk in {"high", "critical"}:
        reasons.append("high_risk_action")
        controls.append("owner_approval")
    if reasons:
        return CIKDecision("approval_required", tuple(reasons), tuple(dict.fromkeys(controls)))
    return CIKDecision("allow", ("cik_boundaries_satisfied",), tuple(controls))
