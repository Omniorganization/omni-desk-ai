from __future__ import annotations

from pathlib import Path

import pytest

from omnidesk_agent.security.admin_auth import AdminAuth
from omnidesk_agent.security.break_glass import BreakGlassStore
from omnidesk_agent.security.dual_approval import DualApprovalStore


class Headers(dict):
    def get(self, key, default=None):  # FastAPI/Starlette headers are case-insensitive; enough for unit tests.
        return super().get(key.lower(), default)


def test_daemon_core_state_uses_repository_factory_not_direct_sqlite_paths():
    daemon = Path("omnidesk_agent/daemon.py").read_text(encoding="utf-8")
    for forbidden in [
        'cfg.workspace.root / "approvals.sqlite3"',
        'cfg.workspace.root / "dual_approvals.sqlite3"',
        'cfg.workspace.root / "break_glass.sqlite3"',
        'cfg.workspace.root / "webhooks.sqlite3"',
        'cfg.workspace.root / "jobs.sqlite3"',
        'cfg.workspace.root / "outbound_messages.sqlite3"',
        'cfg.workspace.root / "runs.sqlite3"',
    ]:
        assert forbidden not in daemon
    for required in [
        "repository_factory.approval_store",
        "repository_factory.dual_approval_store",
        "repository_factory.break_glass_store",
        "repository_factory.webhook_security",
        "repository_factory.job_queue",
        "repository_factory.outbound_messages",
        "repository_factory.run_store",
    ]:
        assert required in daemon


def test_break_glass_elevates_authenticated_actor_only(tmp_path, monkeypatch):
    monkeypatch.setenv("VIEWER_TOKEN", "viewer-token-value")
    store = BreakGlassStore(tmp_path / "break.sqlite3", audit_log=tmp_path / "audit.jsonl")
    store.open(session_id="s1", actor="alice", approved_by="bob", reason="production incident", ttl_seconds=60)
    auth = AdminAuth(
        viewer_token_env="VIEWER_TOKEN",
        operator_token_env="OPERATOR_TOKEN",
        owner_token_env="OWNER_TOKEN",
        break_glass_store=store,
        break_glass_enabled=True,
    )
    headers = Headers({
        "x-omnidesk-admin-token": "viewer-token-value",
        "x-omnidesk-actor": "alice",
        "x-omnidesk-break-glass-session": "s1",
    })
    decision = auth.verify_headers(headers, client_host="127.0.0.1", required_role="owner", path="/admin/outbound/x/cancel")
    assert decision.ok is True
    assert decision.actor == "alice"
    assert decision.role == "owner"

    wrong_actor = Headers({
        "x-omnidesk-admin-token": "viewer-token-value",
        "x-omnidesk-actor": "mallory",
        "x-omnidesk-break-glass-session": "s1",
    })
    denied = auth.verify_headers(wrong_actor, client_host="127.0.0.1", required_role="owner", path="/admin/outbound/x/cancel")
    assert denied.ok is False
    assert "break-glass denied" in denied.reason


def test_dual_approval_rejects_proposer_self_approval(tmp_path):
    store = DualApprovalStore(tmp_path / "dual.sqlite3")
    store.open("approval-1", {"requires_dual_approval": True, "created_by": "alice"})
    with pytest.raises(PermissionError):
        store.approve("approval-1", "alice")
    assert store.approve("approval-1", "bob").ready is False
    assert store.approve("approval-1", "carol").ready is True


def test_ga16_deployment_contract_assets_are_present():
    compose = Path("deploy/docker/docker-compose.full.yml").read_text(encoding="utf-8")
    assert "postgres:" in compose
    assert "OMNIDESK_POSTGRES_DSN" in compose
    assert "pg_isready" in compose
    assert "postgres_password" in compose
    assert "/ready" in compose

    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "requirements.enterprise.lock" in dockerfile
    assert "/ready" in dockerfile

    values = Path("deploy/kubernetes/helm/omnidesk/values.yaml").read_text(encoding="utf-8")
    assert "ingressNamespaceLabel:" in values and "ingress-nginx" in values
    assert "persistence:" in values and "enabled: false" in values
    assert "company.example" not in values
