from __future__ import annotations

import json

from omnidesk_agent.channels.telegram import TelegramChannel
from omnidesk_agent.config import TelegramConfig


def test_telegram_adapter_has_verify_request(monkeypatch):
    cfg = TelegramConfig(enabled=True)
    monkeypatch.setenv(cfg.webhook_secret_env, "s")
    adapter = TelegramChannel(cfg)
    assert hasattr(adapter, "verify_request")
    payload = {"message": {"message_id": 1, "from": {"id": 2}, "text": "hi"}}
    adapter.verify_request({"x-telegram-bot-api-secret-token": "s"}, json.dumps(payload).encode(), {}, payload)
