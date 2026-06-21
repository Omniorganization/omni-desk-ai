from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1", "::1"]
    cfg.plugins.enabled = False
    return cfg


def test_public_ready_redacts_failure_details_while_admin_ready_keeps_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "viewer-token")
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    app.state.runtime.repository_factory.health_check = lambda: (_ for _ in ()).throw(RuntimeError("database unavailable"))

    with TestClient(app) as client:
        public = client.get("/ready")
        assert public.status_code == 503
        assert public.json() == {"detail": {"ok": False}}

        admin = client.get("/admin/ready", headers={"authorization": "Bearer viewer-token"})
        assert admin.status_code == 503
        detail = admin.json()["detail"]
        assert detail["ok"] is False
        assert detail["checks"]["database"] is False
        assert "database unavailable" in detail["checks"]["database_error"]
