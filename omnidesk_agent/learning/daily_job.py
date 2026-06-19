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
from omnidesk_agent.self_learning.drift import DriftDetectionSuite
from omnidesk_agent.self_learning.governance import MemoryCurator
from omnidesk_agent.self_learning.observability.audit import LearningAuditLog
from omnidesk_agent.self_learning.observability.schema import LearningEvent
from omnidesk_agent.self_learning.replay import ReplayReportBuilder
from omnidesk_agent.self_learning.skill_learning import SkillLearningPipeline


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
        self.memory_curator = MemoryCurator()
        self.replay = ReplayReportBuilder()
        self.drift = DriftDetectionSuite()
        self.audit = LearningAuditLog(self.workspace_root / "learning_audit.jsonl")
        self.skill_learning = SkillLearningPipeline(self.workspace_root / "skill_candidates")

    def run(self, days: int = 7) -> dict[str, Any]:
        growth_plan = GrowthPlan.load(self.workspace_root / "growth_plan.json")
        metrics = self.memory.metrics_report(days=days)
        failure_counts = self.memory.summarize_failures(days=days)
        recent_failures = self.memory.search_similar("failed error timeout permission selector captcha login", limit=50)
        repeated = self.failure_analyzer.summarize_repeated_failures(recent_failures)
        recent_memories = self.memory.list_structured(days=days, limit=100)
        memory_reviews = self.memory_curator.curate_store(self.memory, days=days, limit=100)
        for review in memory_reviews:
            self.audit.append(LearningEvent(
                event_type="memory_review",
                experience_id=str(review.get("experience_id")),
                memory_status=review.get("memory_status"),
                confidence=review.get("confidence"),
                contradiction=bool(review.get("contradiction")),
                stale=bool(review.get("stale")),
                memory_precision_score=review.get("confidence"),
                experience_generalization_score=1.0 if review.get("memory_status") in {"validated", "trusted"} else 0.5,
                metadata={"reason": review.get("reason")},
            ))

        replay_report = self.replay.from_experiences(recent_memories, limit=25)
        if replay_report["trace_count"]:
            self.audit.append(LearningEvent(
                event_type="replay_evaluation",
                policy_improvement_score=replay_report.get("policy_improvement_score"),
                learning_regression_score=replay_report.get("learning_regression_score"),
                metadata={"average_improvement_delta": replay_report.get("average_improvement_delta")},
            ))

        drift_signals = self.drift.detect(metrics=metrics, failure_counts=failure_counts, experiences=recent_memories)
        for signal in drift_signals:
            self.audit.append(LearningEvent(
                event_type="drift_detected",
                drift_type=signal.get("drift_type"),
                metadata=signal,
            ))

        reusable_memories = [item for item in self.memory.list_structured(days=days, limit=100, statuses=["validated", "trusted"]) if item.get("reusable_skill")]
        skill_candidates = self.skill_learning.run(
            reusable_memories,
            replay_score=replay_report.get("policy_improvement_score"),
            limit=10,
        )
        for candidate in skill_candidates:
            self.audit.append(LearningEvent(
                event_type="skill_promotion",
                skill_id=candidate.get("name"),
                skill_promotion_success=candidate.get("status") in {"candidate", "canary", "stable"},
                metadata={"status": candidate.get("status"), "source_experience_id": candidate.get("source_experience_id")},
            ))

        proposals = self.growth_planner.propose(growth_plan=growth_plan, failure_summary=repeated or failure_counts, metrics=metrics)
        for signal in drift_signals:
            proposals.append({
                "title": f"Investigate drift: {signal.get('drift_type')}",
                "priority": "high" if signal.get("severity") == "high" else "medium",
                "upgrade_type": "test",
                "reason": signal.get("reason"),
                "recommended_action": "Create replay and regression checks for the drift signal before promoting new memory.",
            })

        safe_proposals = []
        for proposal in proposals:
            risk = "high" if proposal["upgrade_type"] == "core" else "medium"
            recommended_action = str(proposal.get("recommended_action") or proposal.get("reason") or "Review learning proposal before implementation.")
            decision = self.approval_gate.classify_upgrade(
                UpgradeRequest(
                    title=proposal["title"],
                    reason=recommended_action,
                    source="daily-self-learning",
                    risk=risk,
                )
            )
            proposal["recommended_action"] = recommended_action
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
            "memory_reviews": memory_reviews,
            "replay_report": replay_report,
            "drift_signals": drift_signals,
            "skill_candidates": skill_candidates,
            "proposals": safe_proposals,
            "persisted_upgrade_proposals": persisted_proposals,
        }
        out_dir = self.workspace_root / "learning_reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"learning_report_{time.strftime('%Y%m%d')}.json"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(out_path)
        return report
