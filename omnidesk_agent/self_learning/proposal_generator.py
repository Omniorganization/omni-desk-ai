from __future__ import annotations

import re
from typing import Iterable, Optional

from omnidesk_agent.self_learning.policy import STAGE_CODE_PR, STAGE_KNOWLEDGE_PROMPT, STAGE_OBSERVE, SelfLearningBoundaryPolicy
from omnidesk_agent.self_learning.schemas import LearningDraftArtifact, LearningFinding, LearningProposal
from omnidesk_agent.self_upgrade.evidence_bundle import EvidenceBundle
from omnidesk_agent.self_upgrade.pr_generator import PRGenerator, PullRequestDraft


class ControlledProposalGenerator:
    """Generate stage-aware proposals from learning findings."""

    def from_findings(
        self,
        findings: Iterable[LearningFinding],
        *,
        drafts: Optional[Iterable[LearningDraftArtifact]] = None,
    ) -> list[LearningProposal]:
        draft_by_finding = {draft.source_finding_id: draft for draft in drafts or []}
        proposals: list[LearningProposal] = []
        for finding in findings:
            if finding.finding_type == "knowledge_gap":
                proposals.append(self._knowledge_update(finding, draft_by_finding.get(finding.finding_id)))
            elif finding.finding_type == "prompt_issue":
                proposals.append(self._prompt_update(finding, draft_by_finding.get(finding.finding_id)))
            elif finding.finding_type == "code_gap":
                proposals.append(self._code_pr(finding))
            elif finding.finding_type in {"workflow_rule", "approval_policy", "tool_reliability", "evidence_gap"}:
                proposals.append(self._workflow_update(finding, draft_by_finding.get(finding.finding_id)))
            else:
                proposals.append(self._observe_only(finding))
        return proposals

    def draft_code_repair_pr(self, proposal: LearningProposal, *, base: str = "main") -> PullRequestDraft:
        if proposal.proposal_type not in {"code_fix", "test_improvement"}:
            raise ValueError("only code or test improvement proposals can produce PR drafts")
        SelfLearningBoundaryPolicy().assert_allowed(proposal)
        branch = proposal.branch_name or ""
        if not branch.startswith("ai/"):
            raise PermissionError("self-learning code repair PRs must use ai/* branches")
        bundle = EvidenceBundle(
            incident_id=proposal.proposal_id,
            branch=branch,
            tests=tuple(proposal.test_plan),
            gates=("human_approval_required", "no_auto_merge", "rollback_plan_required"),
            rollback_plan=proposal.rollback_plan,
            external_evidence_status="not_required_for_source_pr",
        )
        draft = PRGenerator().draft(
            incident_id=proposal.proposal_id,
            branch=branch,
            summary=proposal.title,
            bundle=bundle,
            change_types=("code_fix",),
            base=base,
        )
        proposal.pr_draft = draft.to_dict()
        proposal.metadata.setdefault("auto_merge", False)
        proposal.metadata["pr_only"] = True
        return draft

    def _knowledge_update(self, finding: LearningFinding, draft: Optional[LearningDraftArtifact]) -> LearningProposal:
        return LearningProposal(
            stage=STAGE_KNOWLEDGE_PROMPT,
            proposal_type="knowledge_update",
            title=f"Approve knowledge update: {finding.title}",
            problem=finding.title,
            proposed_change=finding.recommended_action,
            expected_benefit="Reduce repeated user or task failures by updating reviewed knowledge.",
            risk_level=finding.severity,
            affected_modules=["knowledge_base", "memory"],
            test_plan=["Replay affected historical tasks before staging the knowledge entry."],
            rollback_plan="Remove the staged knowledge entry and restore the previous index snapshot.",
            source_finding_id=finding.finding_id,
            metadata={"draft_artifact_id": getattr(draft, "artifact_id", None)},
        )

    def _prompt_update(self, finding: LearningFinding, draft: Optional[LearningDraftArtifact]) -> LearningProposal:
        return LearningProposal(
            stage=STAGE_KNOWLEDGE_PROMPT,
            proposal_type="prompt_template",
            title=f"Approve prompt update: {finding.title}",
            problem=finding.title,
            proposed_change=finding.recommended_action,
            expected_benefit="Improve prompt consistency without changing runtime behavior before approval.",
            risk_level=finding.severity,
            affected_modules=["prompt_registry"],
            test_plan=["Run replay prompts and compare policy/safety outcomes before staging."],
            rollback_plan="Restore the previous prompt template version and invalidate the staged draft.",
            source_finding_id=finding.finding_id,
            metadata={"draft_artifact_id": getattr(draft, "artifact_id", None)},
        )

    def _workflow_update(self, finding: LearningFinding, draft: Optional[LearningDraftArtifact]) -> LearningProposal:
        return LearningProposal(
            stage=STAGE_KNOWLEDGE_PROMPT,
            proposal_type="workflow_rule",
            title=f"Approve workflow rule: {finding.title}",
            problem=finding.title,
            proposed_change=finding.recommended_action,
            expected_benefit="Make repeated failures reviewable through explicit workflow controls.",
            risk_level=finding.severity,
            affected_modules=["workflow_rules", "approval_policy"],
            test_plan=["Run replay validation for the affected workflow before staging."],
            rollback_plan="Disable the staged workflow rule and restore the previous rule manifest.",
            source_finding_id=finding.finding_id,
            metadata={"draft_artifact_id": getattr(draft, "artifact_id", None)},
        )

    def _code_pr(self, finding: LearningFinding) -> LearningProposal:
        slug = self._slug(finding.title)
        return LearningProposal(
            stage=STAGE_CODE_PR,
            proposal_type="code_fix",
            title=f"Generate code repair PR: {finding.title}",
            problem=finding.title,
            proposed_change=finding.recommended_action,
            expected_benefit="Provide a human-reviewable patch with tests instead of direct runtime mutation.",
            risk_level=finding.severity,
            affected_modules=["source_code", "tests"],
            test_plan=["pytest tests/regression/test_self_upgrade_governance.py", "git diff --check"],
            rollback_plan="Close or revert the PR branch; do not merge if validation or review fails.",
            source_finding_id=finding.finding_id,
            branch_name=f"ai/self-learning-{slug}",
            metadata={"auto_merge": False, "pr_only": True},
        )

    def _observe_only(self, finding: LearningFinding) -> LearningProposal:
        return LearningProposal(
            stage=STAGE_OBSERVE,
            proposal_type="observation",
            title=f"Observe learning signal: {finding.title}",
            problem=finding.title,
            proposed_change="Keep as a learning finding until enough evidence exists for an upgrade proposal.",
            expected_benefit="Avoid premature system mutation from weak evidence.",
            risk_level=finding.severity,
            affected_modules=[],
            test_plan=[],
            rollback_plan="No runtime change was made.",
            requires_human_approval=False,
            source_finding_id=finding.finding_id,
            metadata={"applies_system_change": False},
        )

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return (slug or "repair")[:48]
