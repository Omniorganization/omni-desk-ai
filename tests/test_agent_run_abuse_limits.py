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


class FakeOrchestrator:
    def __init__(self):
        self.messages = []

    async def handle_message(self, msg):
        self.messages.append(msg)
        return {"ok": True, "sender": msg.sender_id, "text": msg.text}


def test_agent_run_actor_rate_limit_blocks_repeated_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "alice")
    cfg = _cfg(tmp_path)
    cfg.api_resource_guard.agent_run_max_requests_per_actor = 1
    cfg.api_resource_guard.max_requests_per_actor = 10
    app = create_app(cfg)
    fake = FakeOrchestrator()
    app.state.runtime.orchestrator = fake

    with TestClient(app) as client:
        headers = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "alice"}
        first = client.post("/agent/run", headers={**headers, "idempotency-key": "run-first"}, json={"message": "first"})
        assert first.status_code == 200, first.text
        assert first.json()["sender"] == "alice"

        second = client.post("/agent/run", headers={**headers, "idempotency-key": "run-second"}, json={"message": "second"})
        assert second.status_code == 429
        assert second.json()["detail"] == "agent rate limit exceeded"
        assert [msg.text for msg in fake.messages] == ["first"]
