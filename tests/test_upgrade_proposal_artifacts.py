from __future__ import annotations

from omnidesk_agent.self_upgrade.proposal.proposal_schema import UpgradeProposal
from omnidesk_agent.self_upgrade.proposal.proposal_store import UpgradeProposalStore


def test_upgrade_proposal_artifact_metadata_round_trips(tmp_path):
    store = UpgradeProposalStore(tmp_path)
    proposal = UpgradeProposal(title="t", source="s", problem="p", proposed_change="c", expected_benefit="b")
    store.create(proposal)
    updated = store.attach_artifacts(
        proposal.proposal_id,
        artifact_hash="sha256:abc",
        test_report_path="reports/test.json",
        pr_url="https://github.com/acme/repo/pull/1",
        merge_sha="deadbeef",
    )
    loaded = store.get(proposal.proposal_id)
    assert loaded is not None
    assert loaded.artifact_hash == "sha256:abc"
    assert updated.test_report_path == "reports/test.json"
    assert loaded.pr_url.endswith("/pull/1")
    assert loaded.merge_sha == "deadbeef"
