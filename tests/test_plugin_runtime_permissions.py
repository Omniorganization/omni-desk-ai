from __future__ import annotations

import pytest

from omnidesk_agent.plugins.subprocess_runner import validate_plugin_permissions
from omnidesk_agent.plugins.docker_runner import DockerPluginTool


def test_plugin_permissions_are_validated(tmp_path):
    with pytest.raises(PermissionError):
        validate_plugin_permissions(["network.raw"])


def test_docker_plugin_builds_no_network_command(tmp_path):
    entry = tmp_path / "plugin.py"
    entry.write_text("print('{}')", encoding="utf-8")
    tool = DockerPluginTool("p", entry, ["plugin.call"])
    argv = tool.docker_argv()
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network" in argv
    assert "none" in argv
    assert "--read-only" in argv
    assert argv[-3:] == ["python3", "-I", "plugin.py"]
