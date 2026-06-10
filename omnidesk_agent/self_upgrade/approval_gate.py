from __future__ import annotations
from typing import Optional

from dataclasses import dataclass
from pathlib import Path

from omnidesk_agent.self_upgrade.models import UpgradeRequest


FORBIDDEN_ACTIONS = {
    "disable_permission",
    "disable_approval",
    "force_push_main",
    "auto_merge_main",
    "auto_restart_daemon",
    "delete_audit_log",
}

HIGH_RISK_KEYWORDS = [
    "permission", "security", "approval", "credential", "token", "shell", "browser.evaluate",
    "gmail.send", "delete", "payment", "core planner", "orchestrator", "self_upgrade",
]


@dataclass
class UpgradeGateDecision:
    allowed: bool
    mode: str
    reason: str


class UpgradeApprovalGate:
    """Classify upgrade actions into allowed / approval-required / forbidden."""

    def classify_action(self, action: str, files_to_change: Optional[list[str]] = None) -> UpgradeGateDecision:
        lower = action.lower()
        files = [f.lower() for f in files_to_change or []]
        if any(forbidden in lower for forbidden in FORBIDDEN_ACTIONS):
            return UpgradeGateDecision(False, "forbidden", "Forbidden self-upgrade action")
        if any(keyword in lower for keyword in HIGH_RISK_KEYWORDS) or any(
            f.startswith("omnidesk_agent/security/") or f.startswith("omnidesk_agent/core/") or f.startswith("omnidesk_agent/self_upgrade/")
            for f in files
        ):
            return UpgradeGateDecision(True, "require_human_approval", "High-risk core/security upgrade")
        return UpgradeGateDecision(True, "allowed", "Low-risk skill/workflow/report upgrade")

    def classify_upgrade(self, request: UpgradeRequest) -> dict:
        decision = self.classify_action(request.title + " " + request.reason, [])
        return decision.__dict__
