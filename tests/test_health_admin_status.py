from __future__ import annotations

import asyncio

import pytest


def _isolated_config(tmp_path):
    from omnidesk_agent.config import AppConfig

    cfg = AppConfig()
    cfg.workspace.root = tmp_path
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.learning.growth_plan_file = tmp_path / "growth_plan.json"
    return cfg


class DummyRequest:
    headers = {}
    client = type("Client", (), {"host": "127.0.0.1"})()
    url = type("Url", (), {"path": "/admin/status"})()


def _endpoint(app, path: str):
    for route in app.routes:
        if getattr(route, "path", "") == path:
            return route.endpoint
    raise AssertionError(f"route not found: {path}")


def test_health_is_public_and_redacted(tmp_path, monkeypatch):
    from omnidesk_agent.server import create_app

    cfg = _isolated_config(tmp_path)
    cfg.gateway.allow_local_admin_without_token = True
    cfg.ensure_dirs()
    app = create_app(cfg)
    data = asyncio.run(_endpoint(app, "/health")())
    assert data["ok"] is True
    assert "version" in data
    assert "workspace" not in data
    assert "tools" not in data


def test_admin_status_requires_auth_when_local_bypass_disabled(tmp_path):
    fastapi = pytest.importorskip("fastapi")
    from omnidesk_agent.server import create_app

    cfg = _isolated_config(tmp_path)
    cfg.gateway.allow_local_admin_without_token = False
    cfg.ensure_dirs()
    app = create_app(cfg)
    with pytest.raises(fastapi.HTTPException) as exc:
        asyncio.run(_endpoint(app, "/admin/status")(DummyRequest()))
    assert exc.value.status_code == 403
