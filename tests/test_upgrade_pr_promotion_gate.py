from __future__ import annotations

import pytest

from omnidesk_agent.self_upgrade.state_machine import UpgradeStateMachine


def test_upgrade_promotion_gate_requires_canary_tests_and_human_approval():
    sm = UpgradeStateMachine()
    proposal = {
        "metadata": {
            "state": "CANARY",
            "regression_result": {"ok": True},
            "security_result": {"ok": True},
            "human_review": {"decision": "approved"},
        }
    }
    sm.assert_can_promote_to_pr(proposal)


def test_upgrade_promotion_gate_blocks_missing_human_review():
    sm = UpgradeStateMachine()
    proposal = {
        "metadata": {
            "state": "CANARY",
            "regression_result": {"ok": True},
            "security_result": {"ok": True},
        }
    }
    with pytest.raises(PermissionError):
        sm.assert_can_promote_to_pr(proposal)
