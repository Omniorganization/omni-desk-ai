from __future__ import annotations

import asyncio

from omnidesk_agent.self_upgrade.governance import GovernedSelfImprovement
from omnidesk_agent.self_upgrade.proposal.proposal_schema import UpgradeProposal


def test_governance_evaluation_writes_results_back(tmp_path):
    gov = GovernedSelfImprovement(tmp_path / "workspace", tmp_path)
    proposal = UpgradeProposal(
        title="Improve prompt fallback",
        source="test",
        problem="model misunderstood",
        proposed_change="add prompt example",
        expected_benefit="better plan quality",
        upgrade_type="prompt",
        affected_modules=["planner"],
        risk_level="low",
    )
    gov.proposal_store.create(proposal)

    result = asyncio.run(gov.evaluate_proposal(
        proposal.proposal_id,
        old_permissions=["browser.read"],
        new_permissions=["browser.read"],
        stable_plan={"steps": [{"risk": "medium"}, {"risk": "medium"}]},
        shadow_plan={"steps": [{"risk": "low"}]},
    ))

    stored = gov.proposal_store.get(proposal.proposal_id)
    evidence = stored.metadata["governance_evaluation"]

    assert "regression_result" in evidence
    assert "security_result" in evidence
    assert "shadow_result" in evidence
    assert "canary_result" in evidence
    assert "permission_diff" in evidence
    assert result["verdict"] in {"effective", "pending_review", "requires_human_approval", "blocked_by_tests"}
