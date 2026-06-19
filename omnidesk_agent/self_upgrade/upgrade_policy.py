from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class UpgradePolicyDecision:
    allowed: bool
    reason: str
    requires_pr: bool = True
    requires_human_review: bool = True


class UpgradePolicyEngine:
    FORBIDDEN_PATHS = {
        "omnidesk_agent/security/permissions.py",
        "omnidesk_agent/security/admin_auth.py",
        "omnidesk_agent/plugins/registry.py",
    }

    def evaluate_paths(self, changed_paths: Iterable[str]) -> UpgradePolicyDecision:
        paths = set(changed_paths)
        if any(p in self.FORBIDDEN_PATHS for p in paths):
            return UpgradePolicyDecision(False, "core security/plugin runtime changes require manual external review")
        if any(p.startswith(".github/workflows/") for p in paths):
            return UpgradePolicyDecision(False, "workflow changes require token with workflow scope and manual review")
        return UpgradePolicyDecision(True, "PR-only upgrade allowed after tests and human review")
