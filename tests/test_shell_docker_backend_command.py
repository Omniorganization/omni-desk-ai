from __future__ import annotations


from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.tools.shell import ShellTool


def test_shell_docker_backend_wraps_command(tmp_path):
    cfg = PermissionConfig()
    cfg.shell_backend = "docker"
    cfg.shell_docker_image = "python:3.11-slim"
    tool = ShellTool(tmp_path, cfg)
    argv = tool._docker_argv(["python3", "-m", "compileall", "omnidesk_agent"])
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network" in argv
    assert "none" in argv
    assert "python:3.11-slim" in argv
    assert argv[-4:] == ["python3", "-m", "compileall", "omnidesk_agent"]
