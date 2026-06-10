from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app


def test_health_is_public_and_redacted(tmp_path, monkeypatch):
    cfg = AppConfig()
    cfg.workspace.root = tmp_path
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.gateway.allow_local_admin_without_token = True
    cfg.ensure_dirs()
    app = create_app(cfg)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "version" in data
    assert "workspace" not in data
    assert "tools" not in data


def test_admin_status_requires_auth_when_local_bypass_disabled(tmp_path):
    cfg = AppConfig()
    cfg.workspace.root = tmp_path
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.gateway.allow_local_admin_without_token = False
    cfg.ensure_dirs()
    app = create_app(cfg)
    client = TestClient(app)
    assert client.get("/admin/status").status_code == 403
