from __future__ import annotations
from omnidesk_agent.self_upgrade.approval_gate import UpgradeApprovalGate


def test_upgrade_gate_requires_approval_for_core_security():
    gate = UpgradeApprovalGate()
    decision = gate.classify_action("change permission system", ["omnidesk_agent/security/permissions.py"])
    assert decision.mode == "require_human_approval"


def test_upgrade_gate_forbids_auto_merge():
    gate = UpgradeApprovalGate()
    decision = gate.classify_action("force_push_main and auto_merge_main", [])
    assert not decision.allowed
    assert decision.mode == "forbidden"
