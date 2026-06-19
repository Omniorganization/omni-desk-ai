from __future__ import annotations

from omnidesk_agent.security.approval_store import ApprovalStore


def test_approval_cannot_be_decided_twice(tmp_path):
    store = ApprovalStore(tmp_path / "approval.sqlite3")
    aid = store.create({"tool": "shell", "action": "run"})
    first = store.decide(aid, "approved")
    assert first["status"] == "approved"
    try:
        store.decide(aid, "denied")
    except PermissionError:
        pass
    else:
        raise AssertionError("second decision should fail")
