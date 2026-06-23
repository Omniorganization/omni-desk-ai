from __future__ import annotations

from pathlib import Path

from omnidesk_agent.config import PermissionConfig, SandboxConfig
from omnidesk_agent.plugins.docker_runner import DockerPluginTool
from omnidesk_agent.sandbox.runner_server import RunnerConfig, _build_docker_command
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner
from omnidesk_agent.tools.shell import ShellTool


def _assert_docker_sandbox_contract(argv: list[str]) -> None:
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv and argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert "--user" in argv and argv[argv.index("--user") + 1] == "65534:65534"
    assert "--pids-limit" in argv and argv[argv.index("--pids-limit") + 1] == "128"
    tmpfs = argv[argv.index("--tmpfs") + 1]
    assert tmpfs.startswith("/tmp:rw,")
    assert "noexec" in tmpfs
    assert "nosuid" in tmpfs


def test_shell_tool_tmpfs_nosec_is_backed_by_docker_sandbox_contract(tmp_path: Path) -> None:
    tool = ShellTool(tmp_path, PermissionConfig(), SandboxConfig(backend="docker"))
    _assert_docker_sandbox_contract(tool._docker_argv(["python3", "-m", "compileall", "omnidesk_agent"]))


def test_self_upgrade_tmpfs_nosec_is_backed_by_docker_sandbox_contract(tmp_path: Path) -> None:
    runner = SandboxRunner(tmp_path, backend="docker")
    _assert_docker_sandbox_contract(runner._docker_command(["pytest", "tests"]))


def test_plugin_tmpfs_nosec_is_backed_by_docker_sandbox_contract(tmp_path: Path) -> None:
    entrypoint = tmp_path / "plugin.py"
    entrypoint.write_text("print('ok')\n", encoding="utf-8")
    tool = DockerPluginTool("plugin", entrypoint, ["plugin.call"])
    _assert_docker_sandbox_contract(tool.docker_argv())


def test_remote_runner_tmpfs_nosec_is_backed_by_docker_sandbox_contract(tmp_path: Path) -> None:
    cfg = RunnerConfig()
    argv = _build_docker_command({"argv": ["python", "-m", "compileall", "."], "image": cfg.default_image}, tmp_path, cfg)
    _assert_docker_sandbox_contract(argv)
