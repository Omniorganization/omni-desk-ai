
from __future__ import annotations

import builtins
import importlib
import sys


def test_channel_envelope_import_without_httpx(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "httpx":
            raise ModuleNotFoundError("No module named 'httpx'")
        return real_import(name, *args, **kwargs)

    # monkeypatch puts the original modules back afterwards, so later tests
    # keep using the same class objects their adapters were imported with.
    for name in list(sys.modules):
        if name.startswith("omnidesk_agent.channels") or name == "httpx":
            monkeypatch.delitem(sys.modules, name, raising=False)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    telegram = importlib.import_module("omnidesk_agent.channels.telegram")
    from omnidesk_agent.config import TelegramConfig

    env = telegram.TelegramChannel(TelegramConfig()).extract_envelope({
        "message": {"message_id": 5, "from": {"id": 9}, "chat": {"id": 1}}
    })
    assert env.sender_id == "9"
    assert env.message_id == "chat:1:5"
