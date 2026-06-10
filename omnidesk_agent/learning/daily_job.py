from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from omnidesk_agent.learning.failure_analyzer import FailureAnalyzer
from omnidesk_agent.learning.growth_plan import GrowthPlan, GrowthPlanner
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.self_upgrade.approval_gate import UpgradeApprovalGate
from omnidesk_agent.self_upgrade.models import UpgradeRequest
from omnidesk_agent.self_upgrade.proposal.proposal_generator import UpgradeProposalGenerator
from omnidesk_agent.self_upgrade.proposal.proposal_store import UpgradeProposalStore


class DailySelfLearningJob:
    """Collect metrics, analyze repeated failures, and create safe proposals.

    This job does not modify core code. It creates a learning report and optional
    reviewable upgrade request artifacts.
    """

    def __init__(self, memory: ExperienceStore, workspace_root: Path):
        self.memory = memory
        self.workspace_root = workspace_root.expanduser()
        self.failure_analyzer = FailureAnalyzer()
        self.growth_planner = GrowthPlanner(self.failure_analyzer)
        self.approval_gate = UpgradeApprovalGate()
        self.proposal_generator = UpgradeProposalGenerator()
        self.proposal_store = UpgradeProposalStore(self.workspace_root / 'upgrade_proposals')

    def run(self, days: int = 7) -> dict[str, Any]:
        growth_plan = GrowthPlan.load(self.workspace_root / "growth_plan.json")
        metrics = self.memory.metrics_report(days=days)
        failure_counts = self.memory.summarize_failures(days=days)
        recent_failures = self.memory.search_similar("failed error timeout permission selector captcha login", limit=50)
        repeated = self.failure_analyzer.summarize_repeated_failures(recent_failures)
        proposals = self.growth_planner.propose(growth_plan=growth_plan, failure_summary=repeated or failure_counts, metrics=metrics)

        safe_proposals = []
        for proposal in proposals:
            risk = "high" if proposal["upgrade_type"] == "core" else "medium"
            decision = self.approval_gate.classify_upgrade(
                UpgradeRequest(
                    title=proposal["title"],
                    reason=proposal["recommended_action"],
                    source="daily-self-learning",
                    risk=risk,
                )
            )
            proposal["approval_policy"] = decision
            safe_proposals.append(proposal)

        persisted_proposals = []
        for item in repeated or failure_counts:
            try:
                proposal = self.proposal_generator.from_failure_summary(item)
                self.proposal_store.create(proposal)
                persisted_proposals.append(proposal.to_dict())
            except Exception:
                pass

        report = {
            "generated_at": time.time(),
            "days": days,
            "growth_plan": growth_plan.__dict__,
            "metrics": metrics,
            "failure_counts": failure_counts,
            "repeated_failures": repeated,
            "proposals": safe_proposals,
            "persisted_upgrade_proposals": persisted_proposals,
        }
        out_dir = self.workspace_root / "learning_reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"learning_report_{time.strftime('%Y%m%d')}.json"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(out_path)
        return report
