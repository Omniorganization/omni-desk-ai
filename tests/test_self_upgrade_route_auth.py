from __future__ import annotations

import asyncio

import pytest


def _app(tmp_path):
    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.server import create_app

    cfg = AppConfig()
    cfg.workspace.root = tmp_path
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.learning.growth_plan_file = tmp_path / "growth_plan.json"
    cfg.gateway.allow_local_admin_without_token = False
    cfg.ensure_dirs()
    return create_app(cfg)


class DummyRequest:
    client = type("Client", (), {"host": "127.0.0.1"})()
    url = type("Url", (), {"path": "/self-upgrade/proposals/missing/evaluate"})()

    def __init__(self, token: str):
        self.headers = {"authorization": f"Bearer {token}"}


def _endpoint(app, path: str):
    for route in app.routes:
        if getattr(route, "path", "") == path:
            return route.endpoint
    raise AssertionError(f"route not found: {path}")


def test_self_upgrade_evaluate_requires_operator_role(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    monkeypatch.delenv("OMNIDESK_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "view")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "op")

    app = _app(tmp_path)
    try:
        endpoint = _endpoint(app, "/self-upgrade/proposals/{proposal_id}/evaluate")

        with pytest.raises(fastapi.HTTPException) as exc:
            asyncio.run(endpoint("missing", DummyRequest("view"), {}))
        assert exc.value.status_code == 403
    finally:
        app.state.runtime.close()


def test_self_upgrade_canary_evaluate_requires_owner_role(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    monkeypatch.delenv("OMNIDESK_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "op")
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "own")

    app = _app(tmp_path)
    try:
        endpoint = _endpoint(app, "/self-upgrade/proposals/{proposal_id}/evaluate")

        with pytest.raises(fastapi.HTTPException) as exc:
            asyncio.run(endpoint("missing", DummyRequest("op"), {"allow_canary": True}))
        assert exc.value.status_code == 403
    finally:
        app.state.runtime.close()
