from __future__ import annotations

import asyncio

import pytest

from omnidesk_agent.self_upgrade.governance import GovernedSelfImprovement
from omnidesk_agent.self_upgrade.proposal.proposal_schema import UpgradeProposal
from omnidesk_agent.self_upgrade.state_machine import UpgradeStateMachine


def test_upgrade_state_machine_allows_valid_path():
    sm = UpgradeStateMachine()
    t = sm.transition("p1", "PROPOSED", "RISK_CLASSIFIED", "classified")
    assert t.new_state == "RISK_CLASSIFIED"


def test_upgrade_state_machine_blocks_invalid_skip():
    sm = UpgradeStateMachine()
    with pytest.raises(ValueError):
        sm.transition("p1", "PROPOSED", "COMPLETED", "skip")


class _OkRunner:
    async def run(self, *args, **kwargs):
        return {"ok": True, "output": "ok"}


def test_generate_artifact_then_evaluate_canary_continues_from_current_state(tmp_path):
    gov = GovernedSelfImprovement(tmp_path / "ws", tmp_path)
    proposal = UpgradeProposal(
        title="Prompt polish",
        source="test",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
        upgrade_type="prompt",
        risk_level="low",
    )
    gov.proposal_store.create(proposal)
    generated = gov.generate_artifact(proposal.proposal_id)
    assert generated["proposal"]["metadata"]["state"] == "ARTIFACT_GENERATED"

    gov.regression_runner = _OkRunner()
    gov.security_runner = _OkRunner()
    result = asyncio.run(gov.evaluate_proposal(proposal.proposal_id, allow_canary=True))

    assert result["proposal"]["metadata"]["state"] == "CANARY"
    history = result["proposal"]["metadata"].get("state_history", [])
    transitions = [(item["from"], item["to"]) for item in history]
    assert ("PROPOSED", "RISK_CLASSIFIED") in transitions
    assert ("RISK_CLASSIFIED", "ARTIFACT_GENERATED") in transitions
    assert ("ARTIFACT_GENERATED", "REGRESSION_TESTED") in transitions
    assert ("SHADOW_MODE", "CANARY") in transitions
