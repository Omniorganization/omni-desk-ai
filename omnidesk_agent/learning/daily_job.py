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
from omnidesk_agent.self_learning.analyzer import LearningAnalyzer
from omnidesk_agent.self_learning.approval import HumanApprovalGate
from omnidesk_agent.self_learning.collector import LearningDataCollector
from omnidesk_agent.self_learning.drift import DriftDetectionSuite
from omnidesk_agent.self_learning.governance import MemoryCurator
from omnidesk_agent.self_learning.knowledge_builder import KnowledgeBuilder
from omnidesk_agent.self_learning.observability.audit import LearningAuditLog
from omnidesk_agent.self_learning.observability.schema import LearningEvent
from omnidesk_agent.self_learning.policy import SelfLearningBoundaryPolicy
from omnidesk_agent.self_learning.proposal_generator import ControlledProposalGenerator
from omnidesk_agent.self_learning.rollback import RollbackManager
from omnidesk_agent.self_learning.schemas import ControlledLearningReport
from omnidesk_agent.self_learning.replay import ReplayReportBuilder
from omnidesk_agent.self_learning.skill_learning import SkillLearningPipeline
from omnidesk_agent.self_learning.store import SelfLearningStore
from omnidesk_agent.self_learning.validator import SandboxValidator


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
        self.self_learning_store = SelfLearningStore(self.workspace_root / "self_learning.sqlite3")
        self.data_collector = LearningDataCollector()
        self.learning_analyzer = LearningAnalyzer()
        self.knowledge_builder = KnowledgeBuilder()
        self.controlled_proposals = ControlledProposalGenerator()
        self.learning_policy = SelfLearningBoundaryPolicy()
        self.human_approval_gate = HumanApprovalGate(self.self_learning_store)
        self.sandbox_validator = SandboxValidator(self.workspace_root)
        self.rollback_manager = RollbackManager(self.self_learning_store)

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

        controlled_self_learning = self._run_controlled_self_learning(days=days)

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
            "controlled_self_learning": controlled_self_learning,
        }
        out_dir = self.workspace_root / "learning_reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"learning_report_{time.strftime('%Y%m%d')}.json"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(out_path)
        return report

    def _run_controlled_self_learning(self, *, days: int) -> dict[str, Any]:
        source_records = self.data_collector.collect(memory=self.memory, audit_log=self.audit, days=days, limit=200)
        for record in source_records:
            self.self_learning_store.record_event(record)

        findings = self.learning_analyzer.analyze(source_records)
        for finding in findings:
            self.self_learning_store.save_finding(finding)

        drafts = self.knowledge_builder.build_drafts(findings)
        draft_paths = self.knowledge_builder.write_pending_drafts(drafts, self.workspace_root)

        proposals = self.controlled_proposals.from_findings(findings, drafts=drafts)
        approvals = []
        validations = []
        rollback_plans = []
        policy_decisions = []
        pr_drafts = []

        for proposal in proposals:
            decision = self.learning_policy.evaluate(proposal)
            proposal.requires_human_approval = decision.requires_human_approval
            proposal.metadata["policy_decision"] = decision.to_dict()
            policy_decisions.append({"proposal_id": proposal.proposal_id, **decision.to_dict()})

            if decision.allowed and decision.requires_human_approval:
                approval = self.human_approval_gate.submit(proposal, reason=decision.reason)
                approvals.append(approval)

            validation = self.sandbox_validator.validate(proposal, commands=[])
            proposal.validation_id = validation.validation_id
            validations.append(validation)
            self.self_learning_store.save_validation(validation)

            if decision.allowed and decision.requires_pr:
                pr_draft = self.controlled_proposals.draft_code_repair_pr(proposal)
                pr_drafts.append(pr_draft.to_dict())

            rollback = self.rollback_manager.plan(proposal)
            rollback_plans.append(rollback)
            self.self_learning_store.save_proposal(proposal)

        report = ControlledLearningReport(
            phase_1={
                "mode": "observe_analyze_draft_only",
                "source_record_count": len(source_records),
                "findings": [finding.to_dict() for finding in findings],
                "system_changes_applied": False,
            },
            phase_2={
                "mode": "knowledge_prompt_workflow_pending_approval",
                "pending_updates": [draft.to_dict() for draft in drafts],
                "pending_update_paths": [str(path) for path in draft_paths],
                "approvals": [approval.to_dict() for approval in approvals if approval.approval_type in {"knowledge_update", "prompt_template", "workflow_rule"}],
                "production_updates_applied": False,
            },
            phase_3={
                "mode": "code_repair_pr_only_no_merge",
                "pr_drafts": pr_drafts,
                "approvals": [approval.to_dict() for approval in approvals if approval.approval_type in {"code_fix", "test_improvement"}],
                "prs_opened": False,
                "auto_merge": False,
            },
        ).to_dict()
        report["policy_decisions"] = policy_decisions
        report["validations"] = [validation.to_dict() for validation in validations]
        report["rollback_plans"] = [rollback.to_dict() for rollback in rollback_plans]
        return report
