from __future__ import annotations

from omnidesk_agent.config import PermissionConfig, SandboxConfig
from omnidesk_agent.tools.shell import ShellTool


def test_shell_tool_uses_sandbox_config_for_docker_backend(tmp_path):
    tool = ShellTool(
        tmp_path,
        PermissionConfig(),
        SandboxConfig(backend="docker", docker_image="python:3.12-slim", memory_limit="768m", cpus="1.5"),
    )
    argv = tool._docker_argv(["python3", "-m", "compileall", "omnidesk_agent"])
    assert tool.backend == "docker"
    assert "python:3.12-slim" in argv
    assert "768m" in argv
    assert "1.5" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--security-opt" in argv and "no-new-privileges" in argv
    assert "--pids-limit" in argv and "128" in argv
    assert "--user" in argv and "65534:65534" in argv
