
from __future__ import annotations

import builtins
import importlib
import sys


def test_config_import_without_yaml(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    sys.modules.pop("omnidesk_agent.config", None)
    sys.modules.pop("yaml", None)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    mod = importlib.import_module("omnidesk_agent.config")
    assert mod.PermissionConfig is not None
    assert mod.yaml is None
