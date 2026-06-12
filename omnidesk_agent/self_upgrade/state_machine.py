from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

INDUSTRIAL_UPGRADE_STATES = [
    "PROPOSED",
    "RISK_CLASSIFIED",
    "ARTIFACT_GENERATED",
    "REGRESSION_TESTED",
    "SECURITY_TESTED",
    "HUMAN_REVIEW",
    "SHADOW_MODE",
    "CANARY",
    "APPROVED_FOR_PR",
    "MERGED_MANUALLY",
    "ROLLED_BACK",
    "COMPLETED",
    "BLOCKED",
]

ALLOWED_TRANSITIONS = {
    "PROPOSED": {"RISK_CLASSIFIED", "BLOCKED"},
    "RISK_CLASSIFIED": {"ARTIFACT_GENERATED", "HUMAN_REVIEW", "BLOCKED"},
    "ARTIFACT_GENERATED": {"REGRESSION_TESTED", "BLOCKED"},
    "REGRESSION_TESTED": {"SECURITY_TESTED", "BLOCKED"},
    "SECURITY_TESTED": {"HUMAN_REVIEW", "SHADOW_MODE", "BLOCKED"},
    "HUMAN_REVIEW": {"SHADOW_MODE", "APPROVED_FOR_PR", "BLOCKED"},
    "SHADOW_MODE": {"CANARY", "HUMAN_REVIEW", "BLOCKED"},
    "CANARY": {"APPROVED_FOR_PR", "ROLLED_BACK", "BLOCKED"},
    "APPROVED_FOR_PR": {"MERGED_MANUALLY", "ROLLED_BACK"},
    "MERGED_MANUALLY": {"COMPLETED", "ROLLED_BACK"},
    "ROLLED_BACK": {"COMPLETED"},
    "BLOCKED": set(),
    "COMPLETED": set(),
}


@dataclass
class UpgradeStateTransition:
    proposal_id: str
    old_state: str
    new_state: str
    reason: str = ""


class UpgradeStateMachine:
    def can_transition(self, old_state: str, new_state: str) -> bool:
        return new_state in ALLOWED_TRANSITIONS.get(old_state, set())

    def transition(self, proposal_id: str, old_state: Optional[str], new_state: str, reason: str = "") -> UpgradeStateTransition:
        old_state = old_state or "PROPOSED"
        if new_state not in INDUSTRIAL_UPGRADE_STATES:
            raise ValueError(f"unknown upgrade state: {new_state}")
        if old_state != new_state and not self.can_transition(old_state, new_state):
            raise ValueError(f"invalid upgrade transition: {old_state} -> {new_state}")
        return UpgradeStateTransition(proposal_id=proposal_id, old_state=old_state, new_state=new_state, reason=reason)


    def assert_can_promote_to_pr(self, proposal: dict) -> None:
        metadata = proposal.get("metadata", {}) if isinstance(proposal, dict) else getattr(proposal, "metadata", {})
        state = metadata.get("state")
        if state != "CANARY":
            raise PermissionError(f"proposal must be in CANARY before PR approval; current={state}")
        for key in ("regression_result", "security_result"):
            result = metadata.get(key) or {}
            if not result.get("ok"):
                raise PermissionError(f"proposal cannot be promoted: {key} is not ok")
        human = metadata.get("human_review") or {}
        if human.get("decision") not in {"approved", "approve"}:
            raise PermissionError("proposal requires explicit human approval before PR")
