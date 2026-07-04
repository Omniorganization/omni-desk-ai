from __future__ import annotations

from typing import Any, Optional

from omnidesk_agent.self_learning.approval import HumanApprovalGate
from omnidesk_agent.self_learning.policy import SelfLearningBoundaryPolicy
from omnidesk_agent.self_learning.schemas import ApprovalRecord, LearningProposal, PromotionRecord, SandboxValidationResult
from omnidesk_agent.self_learning.store import SelfLearningStore


class PromotionEngine:
    """Controlled staging/canary/production promotion for self-learning output."""

    def __init__(
        self,
        *,
        policy: Optional[SelfLearningBoundaryPolicy] = None,
        approvals: Optional[HumanApprovalGate] = None,
        store: Optional[SelfLearningStore] = None,
    ):
        self.policy = policy or SelfLearningBoundaryPolicy()
        self.approvals = approvals or HumanApprovalGate(store)
        self.store = store

    def promote_to_staging(
        self,
        proposal: LearningProposal,
        validation: SandboxValidationResult,
        approval: ApprovalRecord,
    ) -> PromotionRecord:
        decision = self.policy.assert_allowed(proposal)
        self.approvals.require_approved(approval)
        self._require_records_for_proposal(proposal, validation, approval)
        self._require_validation_ok(validation)
        if decision.requires_pr:
            raise PermissionError("code repair proposals cannot be staged directly; create a PR record instead")
        promotion = PromotionRecord(
            proposal_id=proposal.proposal_id,
            environment="staging",
            status="staged",
            approval_id=approval.approval_id,
            validation_id=validation.validation_id,
            metadata={"mode": decision.mode, "proposal_type": proposal.proposal_type},
        )
        proposal.status = "staged"
        self._save(promotion)
        return promotion

    def promote_to_canary(
        self,
        proposal: LearningProposal,
        validation: SandboxValidationResult,
        approval: ApprovalRecord,
        *,
        staging: PromotionRecord,
        canary_evidence: dict[str, Any],
    ) -> PromotionRecord:
        decision = self.policy.assert_allowed(proposal)
        self.approvals.require_approved(approval)
        self._require_records_for_proposal(proposal, validation, approval)
        self._require_promotion_for_proposal(proposal, staging)
        self._require_validation_ok(validation)
        if decision.requires_pr:
            raise PermissionError("code repair proposals cannot be promoted to canary; create a PR record instead")
        if staging.environment != "staging" or staging.status != "staged":
            raise PermissionError("canary promotion requires a staged record first")
        if not canary_evidence.get("ok"):
            raise PermissionError("canary promotion requires passing evidence")
        promotion = PromotionRecord(
            proposal_id=proposal.proposal_id,
            environment="canary",
            status="canary",
            approval_id=approval.approval_id,
            validation_id=validation.validation_id,
            metadata={"staging_promotion_id": staging.promotion_id, "canary_evidence": canary_evidence},
        )
        proposal.status = "canary"
        self._save(promotion)
        return promotion

    def promote_to_production(
        self,
        proposal: LearningProposal,
        validation: SandboxValidationResult,
        approval: ApprovalRecord,
        *,
        canary: PromotionRecord,
        rollback_plan_confirmed: bool,
    ) -> PromotionRecord:
        decision = self.policy.assert_allowed(proposal)
        self.approvals.require_approved(approval)
        self._require_records_for_proposal(proposal, validation, approval)
        self._require_promotion_for_proposal(proposal, canary)
        self._require_validation_ok(validation)
        if decision.requires_pr:
            raise PermissionError("code repair proposals cannot be promoted to production; create a PR record instead")
        if canary.environment != "canary" or canary.status != "canary":
            raise PermissionError("production promotion requires a canary record first")
        if not rollback_plan_confirmed:
            raise PermissionError("production promotion requires a confirmed rollback plan")
        promotion = PromotionRecord(
            proposal_id=proposal.proposal_id,
            environment="production",
            status="promoted",
            approval_id=approval.approval_id,
            validation_id=validation.validation_id,
            metadata={"canary_promotion_id": canary.promotion_id, "rollback_plan": proposal.rollback_plan},
        )
        proposal.status = "promoted"
        self._save(promotion)
        return promotion

    def create_pr_record(
        self,
        proposal: LearningProposal,
        validation: SandboxValidationResult,
        approval: ApprovalRecord,
        *,
        pr_draft: dict[str, Any],
    ) -> PromotionRecord:
        decision = self.policy.assert_allowed(proposal)
        if not decision.requires_pr:
            raise PermissionError("non-code proposals should use staging/canary promotion")
        self.approvals.require_approved(approval)
        self._require_records_for_proposal(proposal, validation, approval)
        self._require_validation_ok(validation)
        if pr_draft.get("base") != "main":
            raise PermissionError("self-learning repair PRs must target main")
        if not str(pr_draft.get("head", "")).startswith("ai/"):
            raise PermissionError("self-learning repair PR head must start with ai/")
        promotion = PromotionRecord(
            proposal_id=proposal.proposal_id,
            environment="github_pr",
            status="pr_draft_ready",
            approval_id=approval.approval_id,
            validation_id=validation.validation_id,
            metadata={"pr_draft": pr_draft, "auto_merge": False},
        )
        proposal.status = "pr_draft_ready"
        self._save(promotion)
        return promotion

    @staticmethod
    def _require_validation_ok(validation: SandboxValidationResult) -> None:
        if not validation.ok:
            raise PermissionError(f"self-learning validation failed: {validation.reason}")

    @staticmethod
    def _require_records_for_proposal(
        proposal: LearningProposal,
        validation: SandboxValidationResult,
        approval: ApprovalRecord,
    ) -> None:
        if approval.proposal_id != proposal.proposal_id:
            raise PermissionError("self-learning approval is not bound to this proposal")
        if validation.proposal_id != proposal.proposal_id:
            raise PermissionError("self-learning validation is not bound to this proposal")

    @staticmethod
    def _require_promotion_for_proposal(proposal: LearningProposal, promotion: PromotionRecord) -> None:
        if promotion.proposal_id != proposal.proposal_id:
            raise PermissionError("self-learning promotion record is not bound to this proposal")

    def _save(self, promotion: PromotionRecord) -> None:
        if self.store is not None:
            self.store.save_promotion(promotion)
