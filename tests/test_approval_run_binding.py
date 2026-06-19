from __future__ import annotations
from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.security.approval_store import ApprovalStore


def test_run_store_binds_approval(tmp_path):
    runs = RunStore(tmp_path / "runs.sqlite3")
    approvals = ApprovalStore(tmp_path / "approvals.sqlite3")
    proposal = {"tool": "computer", "action": "click", "source": "local-cli", "actor": "owner"}
    approval_id = approvals.create(proposal)
    run_id = runs.create({"channel": "local-cli", "sender_id": "owner", "text": "click"})
    runs.save_waiting(run_id, {"goal": "g", "steps": [], "plan_id": "p", "rationale": "r"}, 0, [], approval_id, proposal)

    run = runs.get(run_id)
    assert run["waiting_approval_id"] == approval_id
    assert approvals.get(approval_id)["status"] == "pending"
    approvals.decide(approval_id, "approved")
    assert approvals.require_approved(approval_id, proposal)["status"] == "approved"
