from __future__ import annotations

import json

import pytest

from omnidesk_agent.config import AppConfig


class DummyRequest:
    def __init__(self, body: bytes, headers=None, query_params=None):
        self._body = body
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.client = type("C", (), {"host": "127.0.0.1"})()

    async def body(self):
        return self._body


def _find_guard(app):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        closure = getattr(endpoint, "__closure__", None) or []
        for cell in closure:
            obj = cell.cell_contents
            if callable(obj) and getattr(obj, "__name__", "") == "_guard_webhook":
                return obj
    raise AssertionError("_guard_webhook not found")


class Adapter:
    def extract_envelope(self, payload):
        from omnidesk_agent.channels.base import WebhookEnvelope
        return WebhookEnvelope(source_key="u", sender_id="u", message_id="m", raw=payload)


@pytest.mark.asyncio
async def test_enabled_telegram_webhook_requires_secret_header(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi", reason="FastAPI is required for server webhook integration tests")
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
    cfg.channels.telegram.enabled = True
    cfg.gateway.allow_local_admin_without_token = True
    cfg.ensure_dirs()
    monkeypatch.setenv(cfg.channels.telegram.webhook_secret_env, "s3cret")
    app = create_app(cfg)
    try:
        guard = _find_guard(app)

        body1 = json.dumps({"message": {"message_id": 1, "from": {"id": 2}, "text": "hi"}}).encode()
        with pytest.raises(fastapi.HTTPException) as exc:
            await guard("telegram", Adapter(), DummyRequest(body1, headers={}))
        assert exc.value.status_code == 403

        body2 = json.dumps({"message": {"message_id": 2, "from": {"id": 2}, "text": "hi"}}).encode()
        await guard("telegram", Adapter(), DummyRequest(body2, headers={"x-telegram-bot-api-secret-token": "s3cret"}))
    finally:
        app.state.runtime.close()
