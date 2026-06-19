from __future__ import annotations
from pathlib import Path
from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.permissions import PermissionManager


def test_remote_approval_raises(tmp_path: Path):
    cfg = PermissionConfig(approval_mode="remote_approval", audit_log=tmp_path / "audit.log")
    store = ApprovalStore(tmp_path / "approvals.sqlite3")
    mgr = PermissionManager(cfg, store)
    try:
        mgr.verify({"tool": "computer", "action": "click", "risk": "high", "source": "test"})
    except ApprovalRequired as exc:
        assert exc.approval_id
    else:
        raise AssertionError("ApprovalRequired was not raised")
