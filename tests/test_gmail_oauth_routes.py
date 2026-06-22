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


class FakeOAuth:
    def __init__(self):
        self.started = []
        self.exchanged = []

    def build_authorization_url(self, *, redirect_uri: str, actor: str | None = None):
        self.started.append({"redirect_uri": redirect_uri, "actor": actor})
        return {"authorization_url": redirect_uri + "?state=server-state", "state": "server-state"}

    def exchange_code(self, *, code: str, redirect_uri: str, state: str | None = None, actor: str | None = None):
        self.exchanged.append({"code": code, "redirect_uri": redirect_uri, "state": state, "actor": actor})
        return {"access_token": "token"}


def test_gmail_oauth_routes_bind_state_to_authenticated_actor(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OWNER_ACTOR", "alice")
    app = create_app(_cfg(tmp_path))
    fake = FakeOAuth()
    app.state.runtime.adapters["gmail"].oauth = fake

    with TestClient(app) as client:
        headers = {"authorization": "Bearer owner-token", "x-omnidesk-actor": "alice"}
        started = client.get(
            "/oauth/gmail/start?redirect_uri=http://localhost/callback&state=attacker-state",
            headers=headers,
        )
        assert started.status_code == 200, started.text
        assert started.json()["state"] == "server-state"
        assert fake.started == [{"redirect_uri": "http://localhost/callback", "actor": "alice"}]

        callback = client.get(
            "/oauth/gmail/callback?code=code-1&redirect_uri=http://localhost/callback&state=server-state",
            headers=headers,
        )
        assert callback.status_code == 200, callback.text
        assert fake.exchanged == [{"code": "code-1", "redirect_uri": "http://localhost/callback", "state": "server-state", "actor": "alice"}]
