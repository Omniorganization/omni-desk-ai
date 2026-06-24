from __future__ import annotations
import json

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


def test_audit_hash_tail_reader_handles_large_lines_and_external_append(tmp_path):
    cfg = PermissionConfig(audit_log=tmp_path / "audit.log")
    pm = PermissionManager(cfg)

    pm._audit("large", {"blob": "x" * 70_000})
    first_hash = pm._last_audit_hash()

    assert len(first_hash) == 64
    assert pm._cached_last_hash == first_hash

    appended_hash = "a" * 64
    with cfg.audit_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"hash": appended_hash}) + "\n")

    assert pm._last_audit_hash() == appended_hash
