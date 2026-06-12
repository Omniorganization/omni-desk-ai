from __future__ import annotations

import hashlib
import hmac

import pytest

from omnidesk_agent.plugins.docker_runner import DockerPluginTool
from omnidesk_agent.plugins.registry import PluginRegistry
from omnidesk_agent.plugins.subprocess_runner import SubprocessPluginTool


class Tools:
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        self.tools[tool.name] = tool


def _signed_plugin(root, name: str, sandbox: str, secret: str):
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    entrypoint = plugin_dir / "plugin.py"
    entrypoint.write_text("def call(action, args):\n    return {'ok': True}\n", encoding="utf-8")
    digest = hashlib.sha256(entrypoint.read_bytes()).hexdigest()
    signature = hmac.new(secret.encode("utf-8"), digest.encode("utf-8"), hashlib.sha256).hexdigest()
    (plugin_dir / "plugin.yaml").write_text(
        "\n".join([
            f"name: {name}",
            "version: 1.0.0",
            "enabled: true",
            "trusted: true",
            f"sandbox: {sandbox}",
            "entrypoint: plugin.py",
            "permissions: [plugin.call]",
            f"sha256: {digest}",
            f"signature: {signature}",
        ]),
        encoding="utf-8",
    )


def test_plugin_registry_loads_docker_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_PLUGIN_SIGNING_SECRET", "secret")
    _signed_plugin(tmp_path, "docker_echo", "docker", "secret")
    tools = Tools()

    result = PluginRegistry(tmp_path).load_into(tools)

    assert result == {"docker_echo": ["docker_echo"]}
    assert isinstance(tools.tools["docker_echo"], DockerPluginTool)
    cmd = tools.tools["docker_echo"].docker_argv()
    assert cmd[:4] == ["docker", "run", "--rm", "--network"]
    assert "none" in cmd
    assert "--read-only" in cmd
    assert "--memory" in cmd
    assert "--cpus" in cmd
    assert "--cap-drop" in cmd and "ALL" in cmd
    assert "--security-opt" in cmd and "no-new-privileges" in cmd
    assert "--pids-limit" in cmd and "128" in cmd
    assert "--user" in cmd and "65534:65534" in cmd


def test_plugin_registry_keeps_subprocess_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_PLUGIN_SIGNING_SECRET", "secret")
    _signed_plugin(tmp_path, "subprocess_echo", "subprocess", "secret")
    tools = Tools()

    PluginRegistry(tmp_path).load_into(tools)

    assert isinstance(tools.tools["subprocess_echo"], SubprocessPluginTool)


def test_plugin_registry_rejects_in_process_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_PLUGIN_SIGNING_SECRET", "secret")
    _signed_plugin(tmp_path, "unsafe_echo", "in_process", "secret")

    with pytest.raises(PermissionError, match="in_process plugin sandbox is forbidden"):
        PluginRegistry(tmp_path).load_into(Tools())
