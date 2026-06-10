from __future__ import annotations
from typing import Any, Optional
from omnidesk_agent.self_upgrade.proposal.proposal_schema import UpgradeProposal
from omnidesk_agent.self_upgrade.scoring.upgrade_scorer import UpgradeScorer

class UpgradeProposalGenerator:
    def __init__(self, scorer: Optional[UpgradeScorer] = None):
        self.scorer = scorer or UpgradeScorer()
    def from_failure_summary(self, item: dict[str, Any]) -> UpgradeProposal:
        reason = item.get("failure_reason") or "unknown"
        count = int(item.get("count") or 1)
        proposed = item.get("recommended_upgrade") or f"Improve handling for {reason}"
        proposal = UpgradeProposal(
            title=f"Improve handling for {reason}",
            source=f"repeated failure: {reason}",
            problem=f"{reason} occurred {count} times in the analysis window",
            proposed_change=proposed,
            expected_benefit="Reduce repeat failure rate and manual intervention",
            risk_level="medium",
            affected_modules=self._affected_modules(reason),
            test_plan=["unit test", "regression test", "sandbox dry-run"],
            rollback_plan="Revert proposal implementation commit or disable generated skill/workflow",
            upgrade_type=self._upgrade_type(reason),
            impact=min(1.0, 0.2 + count / 10), frequency=min(1.0, count / 10), effort=0.4, risk=0.4,
            testability=0.7, strategic_value=0.8 if reason in {"selector_changed", "captcha_required", "login_required"} else 0.5,
        )
        proposal.score = self.scorer.score(proposal)
        return proposal
    @staticmethod
    def _affected_modules(reason: str) -> list[str]:
        mapping = {
            "selector_changed": ["browser", "vision", "skills"], "captcha_required": ["browser", "ui_bridge", "skills"],
            "login_required": ["browser", "channels", "skills"], "permission_denied": ["security", "approval"],
            "model_misunderstanding": ["planner", "tools/spec"], "missing_dependency": ["pyproject", "tests"],
            "security_violation": ["security", "permissions"],
        }
        return mapping.get(reason, ["learning", "tests"])
    @staticmethod
    def _upgrade_type(reason: str) -> str:
        if reason in {"selector_changed", "captcha_required", "login_required"}:
            return "skill"
        if reason in {"permission_denied", "security_violation"}:
            return "permission"
        if reason == "model_misunderstanding":
            return "prompt"
        return "workflow"
