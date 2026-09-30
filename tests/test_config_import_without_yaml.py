
from __future__ import annotations

import builtins
import importlib.util
import sys

import pytest

from omnidesk_agent import config


def test_config_import_without_yaml(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location("omnidesk_agent._config_without_yaml_test", config.__file__)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as isolated:
        isolated.setattr(builtins, "__import__", fake_import)
        # Resolve annotations in the isolated namespace, without replacing the
        # canonical module or poisoning subsequent production-config tests.
        isolated.setitem(sys.modules, spec.name, mod)
        spec.loader.exec_module(mod)
        assert mod.PermissionConfig is not None
        assert mod.yaml is None
        with pytest.raises(RuntimeError, match="PyYAML is required"):
            mod._safe_yaml_load("gateway: {}")
    assert real_import("omnidesk_agent.config", fromlist=["config"]) is config
    assert config.yaml is not None
