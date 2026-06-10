from __future__ import annotations
from omnidesk_agent.self_upgrade.risk.permission_diff_checker import PermissionDiffChecker
from omnidesk_agent.self_upgrade.scoring.upgrade_scorer import UpgradeScorer
from omnidesk_agent.self_upgrade.proposal.proposal_schema import UpgradeProposal

def test_upgrade_scoring_prioritizes_high_impact_low_risk():
    proposal = UpgradeProposal(title="x", source="test", problem="p", proposed_change="c", expected_benefit="b", impact=1.0, frequency=1.0, strategic_value=1.0, testability=1.0, risk=0.1, effort=0.1)
    assert UpgradeScorer().score(proposal) > 0.7

def test_permission_diff_flags_shell_execute():
    diff = PermissionDiffChecker().compare(["browser.read"], ["browser.read", "shell.execute"])
    assert "shell.execute" in diff.added
    assert diff.requires_human_approval
    assert diff.risk == "high"
