from __future__ import annotations
import json

import pytest

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.security.audit_chain import AuditIntegrityError
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


def test_audit_chain_handles_large_lines_and_refuses_an_unkeyed_external_append(tmp_path):
    cfg = PermissionConfig(audit_log=tmp_path / "audit.log")
    pm = PermissionManager(cfg)

    # Entries larger than the tail-read chunk still chain correctly.
    pm._audit("large", {"blob": "x" * 70_000})
    pm._audit("after_large", {})
    events = [line for line in cfg.audit_log.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 3  # genesis + 2

    # Anyone can append a line with a plausible hash; it must not be accepted as the
    # chain tail (the unkeyed chain used to continue from it silently).
    with cfg.audit_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"hash": "a" * 64}) + "\n")

    with pytest.raises(AuditIntegrityError):
        pm._audit("next", {})
