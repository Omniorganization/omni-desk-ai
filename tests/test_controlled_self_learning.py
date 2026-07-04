from __future__ import annotations

import pytest

from omnidesk_agent.self_learning import (
    HumanApprovalGate,
    LearningFinding,
    LearningProposal,
    PromotionEngine,
    PromotionRecord,
    SandboxValidationResult,
    SelfLearningBoundaryPolicy,
    SelfLearningStore,
)
from omnidesk_agent.self_learning.policy import STAGE_CODE_PR, STAGE_KNOWLEDGE_PROMPT, STAGE_OBSERVE
from omnidesk_agent.self_learning.proposal_generator import ControlledProposalGenerator


def test_stage_one_observation_cannot_apply_system_changes():
    proposal = LearningProposal(
        stage=STAGE_OBSERVE,
        proposal_type="observation",
        title="Observe recurring issue",
        problem="p",
        proposed_change="draft only",
        expected_benefit="b",
        requires_human_approval=False,
        metadata={"applies_system_change": True},
    )

    decision = SelfLearningBoundaryPolicy().evaluate(proposal)

    assert not decision.allowed
    assert "stage_1_cannot_apply_system_change" in decision.blockers


def test_policy_blocks_forbidden_content_even_when_metadata_clean():
    proposal = LearningProposal(
        stage=STAGE_KNOWLEDGE_PROMPT,
        proposal_type="workflow_rule",
        title="Unsafe workflow rule",
        problem="Operators asked to bypass approval for repeated production updates.",
        proposed_change="Write directly to production without approval and delete audit logs afterwards.",
        expected_benefit="faster updates",
        metadata={},
    )

    decision = SelfLearningBoundaryPolicy().evaluate(proposal)

    assert not decision.allowed
    assert "bypass_approval" in decision.blockers
    assert "direct_production_write" in decision.blockers
    assert "delete_audit_log" in decision.blockers


def test_stage_two_promotion_requires_human_approval(tmp_path):
    store = SelfLearningStore(tmp_path / "self_learning.sqlite3")
    gate = HumanApprovalGate(store)
    engine = PromotionEngine(store=store, approvals=gate)
    proposal = LearningProposal(
        stage=STAGE_KNOWLEDGE_PROMPT,
        proposal_type="prompt_template",
        title="Approve prompt update",
        problem="answers are unclear",
        proposed_change="make refund answers shorter",
        expected_benefit="fewer manual interventions",
        rollback_plan="restore previous prompt template",
    )
    pending = gate.submit(proposal)
    validation = SandboxValidationResult(proposal_id=proposal.proposal_id, ok=True, validation_type="static_review")

    with pytest.raises(PermissionError):
        engine.promote_to_staging(proposal, validation, pending)

    approved = gate.decide(pending.approval_id, "approved", reviewer="owner@example.com", reason="ok")
    staged = engine.promote_to_staging(proposal, validation, approved)

    assert staged.environment == "staging"
    assert staged.status == "staged"
    assert store.list_records("self_learning_promotions")[0]["proposal_id"] == proposal.proposal_id


def test_approval_history_is_append_only(tmp_path):
    store = SelfLearningStore(tmp_path / "self_learning.sqlite3")
    gate = HumanApprovalGate(store)
    proposal = LearningProposal(
        stage=STAGE_KNOWLEDGE_PROMPT,
        proposal_type="prompt_template",
        title="Approve prompt update",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
    )

    pending = gate.submit(proposal)
    approved = gate.decide(pending.approval_id, "approved", reviewer="owner@example.com", reason="ok")

    snapshot = store.get_approval(pending.approval_id)
    history = store.list_records("self_learning_approval_history", limit=10)

    assert snapshot["status"] == "approved"
    assert approved.status == "approved"
    assert [item["status"] for item in history] == ["approved", "pending"]


def test_promotion_requires_approval_and_validation_bound_to_same_proposal(tmp_path):
    store = SelfLearningStore(tmp_path / "self_learning.sqlite3")
    gate = HumanApprovalGate(store)
    engine = PromotionEngine(store=store, approvals=gate)
    proposal = LearningProposal(
        stage=STAGE_KNOWLEDGE_PROMPT,
        proposal_type="workflow_rule",
        title="Real proposal",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
    )
    other = LearningProposal(
        stage=STAGE_KNOWLEDGE_PROMPT,
        proposal_type="workflow_rule",
        title="Other proposal",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
    )
    pending_other = gate.submit(other)
    approved_other = gate.decide(pending_other.approval_id, "approved", reviewer="owner@example.com")
    validation = SandboxValidationResult(proposal_id=proposal.proposal_id, ok=True, validation_type="static_review")

    with pytest.raises(PermissionError, match="approval is not bound"):
        engine.promote_to_staging(proposal, validation, approved_other)

    pending = gate.submit(proposal)
    approved = gate.decide(pending.approval_id, "approved", reviewer="owner@example.com")
    other_validation = SandboxValidationResult(proposal_id=other.proposal_id, ok=True, validation_type="static_review")

    with pytest.raises(PermissionError, match="validation is not bound"):
        engine.promote_to_staging(proposal, other_validation, approved)


def test_stage_three_generates_pr_draft_but_never_merge_record(tmp_path):
    store = SelfLearningStore(tmp_path / "self_learning.sqlite3")
    gate = HumanApprovalGate(store)
    engine = PromotionEngine(store=store, approvals=gate)
    finding = LearningFinding(
        finding_type="code_gap",
        title="Repeated failure: api exception traceback",
        severity="high",
        evidence={"count": 5},
        recommended_action="Prepare a code-fix PR with regression coverage.",
    )
    generator = ControlledProposalGenerator()
    proposal = generator.from_findings([finding])[0]
    pr_draft = generator.draft_code_repair_pr(proposal).to_dict()
    pending = gate.submit(proposal)
    approved = gate.decide(pending.approval_id, "approved", reviewer="owner@example.com", reason="tests attached")
    validation = SandboxValidationResult(proposal_id=proposal.proposal_id, ok=True, validation_type="sandbox")

    promotion = engine.create_pr_record(proposal, validation, approved, pr_draft=pr_draft)

    assert proposal.stage == STAGE_CODE_PR
    assert pr_draft["head"].startswith("ai/")
    assert promotion.environment == "github_pr"
    assert promotion.status == "pr_draft_ready"
    assert promotion.metadata["auto_merge"] is False


def test_code_repair_draft_preserves_forbidden_auto_merge_blocker():
    proposal = LearningProposal(
        stage=STAGE_CODE_PR,
        proposal_type="code_fix",
        title="Unsafe repair",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
        branch_name="ai/repair",
        metadata={"auto_merge": True},
    )

    with pytest.raises(PermissionError, match="auto_merge"):
        ControlledProposalGenerator().draft_code_repair_pr(proposal)

    assert proposal.metadata["auto_merge"] is True


def test_stage_three_pr_record_requires_bound_approval_and_validation(tmp_path):
    store = SelfLearningStore(tmp_path / "self_learning.sqlite3")
    gate = HumanApprovalGate(store)
    engine = PromotionEngine(store=store, approvals=gate)
    proposal = LearningProposal(
        stage=STAGE_CODE_PR,
        proposal_type="code_fix",
        title="Repair",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
        branch_name="ai/repair",
    )
    other = LearningProposal(
        stage=STAGE_CODE_PR,
        proposal_type="code_fix",
        title="Other repair",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
        branch_name="ai/other",
    )
    pending = gate.submit(proposal)
    approved = gate.decide(pending.approval_id, "approved", reviewer="owner@example.com")
    wrong_validation = SandboxValidationResult(proposal_id=other.proposal_id, ok=True, validation_type="sandbox")
    pr_draft = {"base": "main", "head": "ai/repair"}

    with pytest.raises(PermissionError, match="validation is not bound"):
        engine.create_pr_record(proposal, wrong_validation, approved, pr_draft=pr_draft)


def test_stage_three_blocks_auto_merge_and_non_ai_branch():
    proposal = LearningProposal(
        stage=STAGE_CODE_PR,
        proposal_type="code_fix",
        title="Bad repair",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
        branch_name="feature/bad",
        metadata={"merge_after_create": True},
    )

    decision = SelfLearningBoundaryPolicy().evaluate(proposal)

    assert not decision.allowed
    assert "code_repair_branch_must_start_with_ai" in decision.blockers
    assert "stage_3_must_not_merge_pr" in decision.blockers


def test_stage_three_cannot_bypass_pr_only_via_canary_or_production(tmp_path):
    store = SelfLearningStore(tmp_path / "self_learning.sqlite3")
    gate = HumanApprovalGate(store)
    engine = PromotionEngine(store=store, approvals=gate)
    proposal = LearningProposal(
        stage=STAGE_CODE_PR,
        proposal_type="code_fix",
        title="Repair",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
        branch_name="ai/repair",
    )
    pending = gate.submit(proposal)
    approved = gate.decide(pending.approval_id, "approved", reviewer="owner@example.com")
    validation = SandboxValidationResult(proposal_id=proposal.proposal_id, ok=True, validation_type="sandbox")
    staging = PromotionRecord(
        proposal_id=proposal.proposal_id,
        environment="staging",
        status="staged",
        approval_id=approved.approval_id,
        validation_id=validation.validation_id,
    )

    with pytest.raises(PermissionError, match="cannot be promoted to canary"):
        engine.promote_to_canary(
            proposal,
            validation,
            approved,
            staging=staging,
            canary_evidence={"ok": True},
        )

    canary = PromotionRecord(
        proposal_id=proposal.proposal_id,
        environment="canary",
        status="canary",
        approval_id=approved.approval_id,
        validation_id=validation.validation_id,
    )
    with pytest.raises(PermissionError, match="cannot be promoted to production"):
        engine.promote_to_production(
            proposal,
            validation,
            approved,
            canary=canary,
            rollback_plan_confirmed=True,
        )


def test_promotion_chain_requires_same_proposal_id(tmp_path):
    store = SelfLearningStore(tmp_path / "self_learning.sqlite3")
    gate = HumanApprovalGate(store)
    engine = PromotionEngine(store=store, approvals=gate)
    proposal = LearningProposal(
        stage=STAGE_KNOWLEDGE_PROMPT,
        proposal_type="prompt_template",
        title="Prompt update",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
    )
    other = LearningProposal(
        stage=STAGE_KNOWLEDGE_PROMPT,
        proposal_type="prompt_template",
        title="Other prompt update",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
    )
    pending = gate.submit(proposal)
    approved = gate.decide(pending.approval_id, "approved", reviewer="owner@example.com")
    validation = SandboxValidationResult(proposal_id=proposal.proposal_id, ok=True, validation_type="static_review")
    other_staging = PromotionRecord(
        proposal_id=other.proposal_id,
        environment="staging",
        status="staged",
        approval_id=approved.approval_id,
        validation_id=validation.validation_id,
    )

    with pytest.raises(PermissionError, match="promotion record is not bound"):
        engine.promote_to_canary(
            proposal,
            validation,
            approved,
            staging=other_staging,
            canary_evidence={"ok": True},
        )
