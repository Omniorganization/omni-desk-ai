from __future__ import annotations

from omnidesk_agent.config import AppConfig
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.plugins.manifest import PluginManifest
from omnidesk_agent.validation.extensions import validate_extensions


def _runtime_config(tmp_path):
    cfg = AppConfig()
    cfg.workspace.root = tmp_path
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.learning.growth_plan_file = tmp_path / "growth_plan.json"
    cfg.ensure_dirs()
    return cfg


def test_validate_extensions_runtime_without_plugins(tmp_path):
    runtime = OmniDeskRuntime(_runtime_config(tmp_path))
    try:
        result = validate_extensions(runtime)

        assert result["ok"] is True
        assert result["plugins"]["loaded_count"] == 0
        assert result["plugins"]["plugins"] == {}
        assert {"files", "test", "vision"} <= set(result["tools"])
        assert "shell" not in result["tools"]
    finally:
        runtime.close()


def test_validate_extensions_reports_loaded_plugin_state(tmp_path):
    runtime = OmniDeskRuntime(_runtime_config(tmp_path))
    try:
        runtime.plugins.loaded["echo"] = PluginManifest(
            name="echo",
            version="1.2.3",
            enabled=True,
            trusted=True,
            sandbox="docker",
            entrypoint="plugin.py",
            permissions=["plugin.call"],
        )

        result = validate_extensions(runtime)

        plugin = result["plugins"]["plugins"]["echo"]
        assert result["plugins"]["loaded_count"] == 1
        assert result["plugins"]["trusted_only"] is True
        assert plugin["version"] == "1.2.3"
        assert plugin["sandbox"] == "docker"
        assert plugin["permissions"] == ["plugin.call"]
    finally:
        runtime.close()
