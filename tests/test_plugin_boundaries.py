from __future__ import annotations

import pytest
from omnidesk_agent.plugins.registry import PluginRegistry


def test_plugin_registry_rejects_bad_names(tmp_path):
    registry = PluginRegistry(tmp_path)
    with pytest.raises(PermissionError):
        registry._check_manifest_name("../../bad")


def test_plugin_manifest_entrypoint_escape_is_rejected(tmp_path):
    root = tmp_path / "plugins"
    plugin = root / "p"
    plugin.mkdir(parents=True)
    outside = root / "outside.py"
    outside.write_text("print('x')", encoding="utf-8")
    (plugin / "plugin.yaml").write_text("name: p\ntrusted: true\nentrypoint: ../outside.py\nsandbox: subprocess\n", encoding="utf-8")
    class Tools:
        def register(self, tool): pass
    registry = PluginRegistry(root)
    with pytest.raises(PermissionError):
        registry.load_into(Tools())
