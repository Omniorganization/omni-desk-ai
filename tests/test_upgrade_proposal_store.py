from __future__ import annotations
from omnidesk_agent.self_upgrade.proposal.proposal_schema import UpgradeProposal
from omnidesk_agent.self_upgrade.proposal.proposal_store import UpgradeProposalStore

def test_upgrade_proposal_lifecycle(tmp_path):
    store = UpgradeProposalStore(tmp_path / "upgrade_proposals")
    proposal = UpgradeProposal(title="Improve browser fallback", source="failure summary", problem="selector failed", proposed_change="add visual fallback", expected_benefit="fewer browser failures")
    store.create(proposal)
    assert store.get(proposal.proposal_id).status == "pending"
    store.approve(proposal.proposal_id, "ok")
    assert store.get(proposal.proposal_id).status == "approved"
    store.mark_implemented(proposal.proposal_id)
    assert store.get(proposal.proposal_id).status == "implemented"
