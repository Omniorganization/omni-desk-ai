from __future__ import annotations

import pytest

from omnidesk_agent.self_learning import (
    HumanApprovalGate,
    LearningFinding,
    LearningProposal,
    PromotionEngine,
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
