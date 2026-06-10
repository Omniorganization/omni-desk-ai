from __future__ import annotations
from pathlib import Path
from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.security.permissions import PermissionManager


def test_session_allow_is_scoped_by_actor_risk_scope_hash(tmp_path):
    cfg = PermissionConfig(audit_log=tmp_path / "audit.log")
    pm = PermissionManager(cfg)
    p1 = {"tool": "computer", "action": "click", "source": "web", "actor": "u1", "risk": "high", "scope_hash": "abc"}
    p2 = {"tool": "computer", "action": "click", "source": "web", "actor": "u2", "risk": "high", "scope_hash": "abc"}
    k1 = pm.session_key(p1)
    k2 = pm.session_key(p2)
    assert k1 != k2
    pm.session_allows.add(k1)
    assert k1 in pm.session_allows
    assert k2 not in pm.session_allows
