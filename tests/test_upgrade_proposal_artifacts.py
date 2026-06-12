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
        artifact_sha256="abc",
        branch_name="ai/upgrade-1",
        test_report_path="reports/test.json",
        regression_report_path="reports/regression.json",
        security_report_path="reports/security.json",
        pr_url="https://github.com/acme/repo/pull/1",
        pr_number=1,
        merge_sha="deadbeef",
        merge_commit_sha="deadbeef",
        approved_by="owner@example.com",
        approved_at=123.0,
        rollback_artifact_path="dist/rollback.patch",
    )
    loaded = store.get(proposal.proposal_id)
    assert loaded is not None
    assert loaded.artifact_hash == "sha256:abc"
    assert updated.test_report_path == "reports/test.json"
    assert loaded.pr_url.endswith("/pull/1")
    assert loaded.merge_sha == "deadbeef"
    assert loaded.artifact_sha256 == "abc"
    assert loaded.branch_name == "ai/upgrade-1"
    assert loaded.regression_report_path.endswith("regression.json")
    assert loaded.security_report_path.endswith("security.json")
    assert loaded.pr_number == 1
    assert loaded.merge_commit_sha == "deadbeef"
    assert loaded.approved_by == "owner@example.com"
    assert loaded.approved_at == 123.0
    assert loaded.rollback_artifact_path.endswith("rollback.patch")
