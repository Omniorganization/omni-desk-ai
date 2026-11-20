"""Every human approval decision is on the tamper-evident audit chain.

Found while reviewing gateway-clients' approval plane (#282): approve, deny
and dual-approve decisions were kept only in the approval tables, which can be
edited, so the security audit log could not say who approved an action. The
stores now record each decision on the chain inside the deciding
transaction, for every route and plane and for both backends. A decision the
chain cannot take is rolled back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.repositories.postgres_state import PostgresApprovalStore, PostgresDualApprovalStore
from omnidesk_agent.security.audit_chain import read_audit_events
from omnidesk_agent.security.audit_errors import AuditIntegrityError

TOKENS = {"operator": "p" * 40, "owner": "o" * 40, "admin": "a" * 40}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    for role, token in TOKENS.items():
        monkeypatch.setenv(f"OMNIDESK_{role.upper()}_TOKEN", token)
        monkeypatch.setenv(f"OMNIDESK_{role.upper()}_ACTOR", f"{role}-a")
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1"]
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    from omnidesk_agent.server import create_app

    return create_app(cfg)


def _h(role: str, key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {TOKENS[role]}", "idempotency-key": key}


def _decisions(app) -> list[dict]:
    return [e for e in read_audit_events(app.state.runtime.permissions.audit_log) if str(e.get("event", "")).startswith("approval.")]


def _proposal(risk: str = "high", **extra) -> dict:
    return {"tool": "files", "action": "write_text", "risk": risk, "actor": "operator-a", "created_by": "operator-a", "args": {"text": "secret body"}, **extra}


def test_approve_and_deny_are_recorded_with_who_decided(app):
    store = app.state.runtime.approval_store
    approved, denied = store.create(_proposal()), store.create(_proposal())
    with TestClient(app) as client:
        assert client.post(f"/approvals/{approved}/approve", headers=_h("owner", "a-1"), json={}).status_code == 200
        assert client.post(f"/approvals/{denied}/deny", headers=_h("owner", "d-1"), json={}).status_code == 200

    entries = {e["approval_id"]: e for e in _decisions(app)}
    assert entries[approved]["decision"] == "approved" and entries[approved]["decided_by"] == "owner-a"
    assert entries[denied]["decision"] == "denied" and entries[denied]["requesters"] == ["operator-a"]
    assert all("args" not in e and "secret body" not in str(e) for e in entries.values())


def test_each_dual_approval_is_recorded(app):
    store = app.state.runtime.approval_store
    critical = store.create(_proposal("critical", requires_dual_approval=True))
    with TestClient(app) as client:
        assert client.post(f"/approvals/{critical}/dual-approve", headers=_h("owner", "x-1"), json={}).status_code == 200
        assert client.post(f"/approvals/{critical}/dual-approve", headers=_h("admin", "x-2"), json={}).status_code == 200

    dual = [e for e in _decisions(app) if e["event"] == "approval.dual_approved"]
    assert [(e["approver"], e["ready"]) for e in dual] == [("owner-a", False), ("admin-a", True)]


def test_a_decision_the_audit_log_cannot_take_does_not_stand(app):
    rt = app.state.runtime
    pending = rt.approval_store.create(_proposal())
    rt.permissions.mark_audit_integrity_failure("audit log failed verification")
    with TestClient(app, raise_server_exceptions=False) as client:
        refused = client.post(f"/approvals/{pending}/approve", headers=_h("owner", "a-1"), json={})

    assert refused.status_code >= 400
    assert rt.approval_store.get(pending)["status"] == "pending"  # rolled back with the failed entry


class _FakeJsonState:
    """update_locked(namespace, key, updater) like _PostgresJsonState, in memory."""

    def __init__(self, rows):
        self.rows = rows

    def update_locked(self, namespace, key, updater):
        row = dict(self.rows[key])
        updated = updater(row)  # an exception here is the transaction rolling back
        self.rows[key] = updated
        return updated

    def get(self, namespace, key):
        return self.rows.get(key)


def test_the_postgresql_stores_record_inside_their_transaction():
    recorded: list[tuple[str, dict]] = []
    state = _FakeJsonState({
        "ap-1": {"status": "pending", "proposal": _proposal(), "expires_at": None},
        "du-1": {"proposal": _proposal("critical"), "first_approver": None, "second_approver": None},
    })
    approvals, dual = PostgresApprovalStore(state), PostgresDualApprovalStore(state)  # type: ignore[arg-type]
    approvals.decision_audit = dual.decision_audit = lambda event, payload: recorded.append((event, payload))

    approvals.decide("ap-1", "approved", {"decided_by": "owner-a"})
    dual.approve("du-1", "owner-a")

    assert [(e, p.get("decided_by") or p.get("approver")) for e, p in recorded] == [("approval.decided", "owner-a"), ("approval.dual_approved", "owner-a")]

    def broken(event, payload):
        raise AuditIntegrityError("audit log cannot be written")

    approvals.decision_audit = broken
    state.rows["ap-2"] = {"status": "pending", "proposal": _proposal(), "expires_at": None}
    with pytest.raises(AuditIntegrityError):
        approvals.decide("ap-2", "approved", {"decided_by": "owner-a"})
    assert state.rows["ap-2"]["status"] == "pending"


def test_the_permission_manager_wires_both_stores(tmp_path):
    from omnidesk_agent.config import PermissionConfig
    from omnidesk_agent.security.permissions import PermissionManager

    cfg = PermissionConfig()
    cfg.audit_log = tmp_path / "audit.log"
    dual = SimpleNamespace(decision_audit=None)
    store = SimpleNamespace(decision_audit=None, dual_approval_store=dual)
    manager = PermissionManager(cfg, store)

    assert store.decision_audit == manager._audit and dual.decision_audit == manager._audit
