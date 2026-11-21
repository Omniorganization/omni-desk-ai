"""Gates on the learning and upgrade paths must hold against rephrasing,
forged approvals and overwritten evidence (audit 2026-11-21, autonomy gaps)."""
from __future__ import annotations

import asyncio
import json

import pytest

from omnidesk_agent.learning.failure_analyzer import FailureAnalyzer
from omnidesk_agent.learning.growth_plan import GrowthPlan, GrowthPlanner
from omnidesk_agent.self_learning.analyzer import LearningAnalyzer
from omnidesk_agent.self_learning.approval import HumanApprovalGate
from omnidesk_agent.self_learning.forbidden_intent import forbidden_intents
from omnidesk_agent.self_learning.policy import STAGE_KNOWLEDGE_PROMPT, SelfLearningBoundaryPolicy
from omnidesk_agent.self_learning.promotion import PromotionEngine
from omnidesk_agent.self_learning.proposal_generator import ControlledProposalGenerator
from omnidesk_agent.self_learning.schemas import ApprovalRecord, LearningFinding, LearningProposal, SandboxValidationResult
from omnidesk_agent.self_learning.store import SelfLearningStore
from omnidesk_agent.self_upgrade.approval_gate import UpgradeApprovalGate
from omnidesk_agent.self_upgrade.governance import GovernedSelfImprovement
from omnidesk_agent.self_upgrade.proposal.proposal_schema import UpgradeProposal


@pytest.mark.parametrize("text, code", [
    ("skip the approval step and merge automatically", "bypass_approval"),
    ("skip the approval step and merge automatically", "auto_merge"),
    ("remove the audit logs", "delete_audit_log"),
    ("force push to main", "force_push"),
    ("commit straight to main", "direct_main_patch"),
    ("deploy directly to production", "direct_production_write"),
    ("relax the sandbox network rule", "weaken_security_control"),
    ("Disable_Approval for faster runs", "bypass_approval"),
    ("绕过审批后自动合并", "bypass_approval"),
])
def test_rephrased_forbidden_intent_is_detected(text, code):
    assert code in forbidden_intents(text)


def test_learning_policy_blocks_rephrasings_the_old_phrase_list_missed():
    proposal = LearningProposal(
        stage=STAGE_KNOWLEDGE_PROMPT,
        proposal_type="workflow_rule",
        title="Faster releases",
        problem="releases wait for reviewers",
        proposed_change="skip the approval step and merge automatically; remove the audit logs",
        expected_benefit="speed",
    )
    decision = SelfLearningBoundaryPolicy().evaluate(proposal)
    assert not decision.allowed
    assert {"bypass_approval", "auto_merge", "delete_audit_log"} <= set(decision.blockers)


def test_generated_proposals_are_not_false_positives():
    findings = [
        LearningFinding(finding_type=kind, title=f"Repeated failure: {reason}", severity="medium", evidence={"count": 3},
                        recommended_action=LearningAnalyzer()._recommended_action(kind, reason))
        for kind, reason in [("prompt_issue", "timeout"), ("knowledge_gap", "stale price list"),
                             ("code_gap", "api exception"), ("approval_policy", "gmail.send"), ("workflow_rule", "selector")]
    ]
    for proposal in ControlledProposalGenerator().from_findings(findings):
        assert SelfLearningBoundaryPolicy().evaluate(proposal).allowed, proposal.to_dict()


def test_growth_plan_proposals_pass_the_upgrade_gate():
    gate = UpgradeApprovalGate()
    analyzer = FailureAnalyzer()
    reasons = ["captcha_required", "login_required", "permission_denied", "network_timeout", "selector_changed",
               "model_misunderstanding", "tool_error", "missing_dependency", "security_violation", "unknown"]
    summary = [{"failure_reason": r, "count": 5, "recommended_upgrade": analyzer.recommended_upgrade(r)} for r in reasons]
    proposals = []
    for start in range(0, len(summary), 5):  # propose() looks at five failures per call
        proposals += GrowthPlanner().propose(growth_plan=GrowthPlan(), failure_summary=summary[start:start + 5],
                                             metrics={"manual_intervention_rate": 0.5, "tool_error_rate": 0.5})
    assert len(proposals) == 14
    for proposal in proposals:
        text = f"{proposal['title']} {proposal.get('recommended_action') or proposal.get('reason') or ''}"
        assert gate.classify_action(text).allowed, text


@pytest.mark.parametrize("text", [
    "force push to main and enable auto merge",
    "Restart the daemon after patching",
    "bypass approval for upgrades",
])
def test_upgrade_gate_forbids_plain_language_forms(text):
    decision = UpgradeApprovalGate().classify_action(text)
    assert not decision.allowed and decision.mode == "forbidden"


def test_promotion_engine_ignores_a_forged_approval_object(tmp_path):
    store = SelfLearningStore(tmp_path / "s.sqlite3")
    engine = PromotionEngine(store=store, approvals=HumanApprovalGate(store))
    proposal = LearningProposal(stage=STAGE_KNOWLEDGE_PROMPT, proposal_type="prompt_template", title="t", problem="p",
                                proposed_change="c", expected_benefit="b")
    forged = ApprovalRecord(proposal_id=proposal.proposal_id, approval_type="prompt_template", status="approved", reviewer="owner")
    validation = SandboxValidationResult(proposal_id=proposal.proposal_id, ok=True, validation_type="static_review")

    with pytest.raises(PermissionError, match="never stored"):
        engine.promote_to_staging(proposal, validation, forged)

    pending = HumanApprovalGate(store).submit(proposal)
    pending.status, pending.reviewer = "approved", "owner"  # edited in memory, never decided
    with pytest.raises(PermissionError, match="status=pending"):
        engine.promote_to_staging(proposal, validation, pending)
    store.close()


def test_approval_gate_without_store_fails_closed():
    approval = ApprovalRecord(proposal_id="p", approval_type="prompt_template", status="approved", reviewer="owner")
    with pytest.raises(PermissionError, match="stored decision"):
        HumanApprovalGate().require_approved(approval)


def _proposal(gov):
    proposal = UpgradeProposal(title="Improve selector", source="unit", problem="selector drift", proposed_change="patch",
                               expected_benefit="less failure", upgrade_type="skill", risk_level="low", rollback_plan="revert",
                               affected_modules=["browser"])
    gov.proposal_store.create(proposal)
    return proposal


def _governance(tmp_path, *, tests_ok=True):
    gov = GovernedSelfImprovement(tmp_path / "ws", tmp_path / "repo")

    async def run():
        return {"ok": tests_ok, "report_path": "report.json"}

    gov.regression_runner.run = run
    gov.security_runner.run = run
    gov.risk_classifier.classify = lambda *a, **k: {"can_auto_canary": True, "requires_human_approval": False}
    return gov


def test_canary_is_refused_without_a_shadow_run_and_history_does_not_claim_one(tmp_path):
    gov = _governance(tmp_path)
    proposal = _proposal(gov)

    result = asyncio.run(gov.evaluate_proposal(proposal.proposal_id, allow_canary=True))

    stored = gov.proposal_store.get(proposal.proposal_id)
    assert result["evaluation"]["canary_result"] == {"enabled": False, "reason": "canary_requires_shadow_evidence"}
    assert stored.metadata["state"] == "HUMAN_REVIEW"
    assert "SHADOW_MODE" not in [h["to"] for h in stored.metadata["state_history"]]
    assert result["verdict"] != "effective"
    gov.close()


def test_canary_with_shadow_evidence_is_labelled_canary_enabled_not_effective(tmp_path):
    gov = _governance(tmp_path)
    proposal = _proposal(gov)

    result = asyncio.run(gov.evaluate_proposal(
        proposal.proposal_id, allow_canary=True,
        stable_plan={"steps": [{"risk": "medium"}, {"risk": "medium"}]}, shadow_plan={"steps": [{"risk": "low"}]},
    ))

    assert result["verdict"] == "canary_enabled"
    assert gov.memory.effectiveness()["effective_rate"] == 0
    gov.close()


def test_blocked_proposal_cannot_be_approved_and_feedback_keeps_evidence(tmp_path):
    gov = _governance(tmp_path, tests_ok=False)
    proposal = _proposal(gov)
    asyncio.run(gov.evaluate_proposal(proposal.proposal_id))
    assert gov.proposal_store.get(proposal.proposal_id).metadata["state"] == "BLOCKED"

    with pytest.raises(PermissionError, match="BLOCKED"):
        gov.record_human_feedback(proposal.proposal_id, "approved", "looks fine", reviewer="owner@example.com")

    gov.record_human_feedback(proposal.proposal_id, "rejected", "tests fail", reviewer="owner@example.com")
    row = gov.memory.recent(1)[0]
    metadata = json.loads(row["metadata"])
    assert row["verdict"] == "blocked_by_tests"
    assert "regression_result" in metadata
    assert metadata["human_reviews"][0]["reviewer"] == "owner@example.com"
    gov.close()


def test_cli_feedback_without_identity_records_the_os_account(tmp_path):
    gov = _governance(tmp_path)
    proposal = _proposal(gov)
    result = gov.record_human_feedback(proposal.proposal_id, "rejected", "not needed")
    assert result["metadata"]["human_review"]["reviewer"].startswith("os-user:")
    gov.close()


def test_a_proposal_held_for_review_can_still_reach_canary_with_shadow_evidence(tmp_path):
    # Codex review on #226: HUMAN_REVIEW -> SHADOW_MODE -> CANARY must be a valid continuation.
    gov = _governance(tmp_path)
    proposal = _proposal(gov)
    asyncio.run(gov.evaluate_proposal(proposal.proposal_id, allow_canary=True))  # no shadow: HUMAN_REVIEW
    assert gov.proposal_store.get(proposal.proposal_id).metadata["state"] == "HUMAN_REVIEW"

    result = asyncio.run(gov.evaluate_proposal(
        proposal.proposal_id, allow_canary=True,
        stable_plan={"steps": [{"risk": "medium"}, {"risk": "medium"}]}, shadow_plan={"steps": [{"risk": "low"}]},
    ))
    history = [(h["from"], h["to"]) for h in result["proposal"]["metadata"]["state_history"]]
    assert result["verdict"] == "canary_enabled"
    assert history[-2:] == [("HUMAN_REVIEW", "SHADOW_MODE"), ("SHADOW_MODE", "CANARY")]
    gov.close()


def test_a_proposal_a_human_rejected_never_reaches_canary(tmp_path):
    # Coordinator review on #226: the HUMAN_REVIEW -> CANARY continuation must not
    # override a human rejection, in the governance record or in the canary flag.
    gov = _governance(tmp_path)
    proposal = _proposal(gov)
    asyncio.run(gov.evaluate_proposal(proposal.proposal_id, allow_canary=True))  # no shadow: HUMAN_REVIEW
    gov.record_human_feedback(proposal.proposal_id, "rejected", "do not ship", reviewer="owner@example.com")

    result = asyncio.run(gov.evaluate_proposal(
        proposal.proposal_id, allow_canary=True,
        stable_plan={"steps": [{"risk": "medium"}, {"risk": "medium"}]}, shadow_plan={"steps": [{"risk": "low"}]},
    ))
    assert result["evaluation"]["canary_result"] == {"enabled": False, "reason": "canary_refused_after_human_rejection"}
    assert result["verdict"] != "canary_enabled"
    assert gov.proposal_store.get(proposal.proposal_id).metadata["state"] == "HUMAN_REVIEW"
    assert not (tmp_path / "ws" / "canary_state.json").exists()
    gov.close()


def test_canary_state_is_not_written_when_canary_is_unreachable(tmp_path):
    gov = _governance(tmp_path)
    proposal = _proposal(gov)
    proposal.metadata["state"] = "APPROVED_FOR_PR"
    gov.proposal_store.save(proposal)
    with pytest.raises(ValueError):
        asyncio.run(gov.evaluate_proposal(
            proposal.proposal_id, allow_canary=True,
            stable_plan={"steps": [{"risk": "medium"}]}, shadow_plan={"steps": [{"risk": "low"}]},
        ))
    assert not (tmp_path / "ws" / "canary_state.json").exists()
    gov.close()
