from __future__ import annotations

import builtins
import importlib
import sys


def test_browser_import_without_httpx(monkeypatch):
    real_import = builtins.__import__
    def fake_import(name, *args, **kwargs):
        if name == "httpx":
            raise ModuleNotFoundError("No module named 'httpx'")
        return real_import(name, *args, **kwargs)
    sys.modules.pop("omnidesk_agent.tools.browser", None)
    sys.modules.pop("httpx", None)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    mod = importlib.import_module("omnidesk_agent.tools.browser")
    assert mod.httpx is None
