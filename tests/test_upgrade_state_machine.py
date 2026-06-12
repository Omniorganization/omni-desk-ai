from __future__ import annotations

import pytest

from omnidesk_agent.self_upgrade.state_machine import UpgradeStateMachine


def test_upgrade_state_machine_allows_valid_path():
    sm = UpgradeStateMachine()
    t = sm.transition("p1", "PROPOSED", "RISK_CLASSIFIED", "classified")
    assert t.new_state == "RISK_CLASSIFIED"


def test_upgrade_state_machine_blocks_invalid_skip():
    sm = UpgradeStateMachine()
    with pytest.raises(ValueError):
        sm.transition("p1", "PROPOSED", "COMPLETED", "skip")
