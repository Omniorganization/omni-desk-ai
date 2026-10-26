"""Sandbox low-severity findings from the second review.

- A workspace or plugin path went unchecked into ``--mount type=bind,src=...``;
  Docker parses that value as CSV key=value fields, so a comma, '=' or quote
  in the path could add fields to the mount;
- the host argv backends (shell tool, upgrade test runner) passed the whole
  gateway environment to commands that run workspace code.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnidesk_agent.config import PermissionConfig, SandboxConfig
from omnidesk_agent.core.models import ApprovalDecision
from omnidesk_agent.plugins.docker_runner import DockerPluginTool
from omnidesk_agent.sandbox import runner_server
from omnidesk_agent.sandbox.isolation import bind_mount_source, minimal_host_env
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.shell import ShellTool
from omnidesk_agent.validation.production import REQUIRE_PRODUCTION_GUARDS_ENV

UNSAFE = [",", "=", '"', "'", "\n", "\r", "\t", "\x00", "\x1b", "\x7f", "\x85"]
SECRETS = {
    "OMNIDESK_ADMIN_TOKEN": "admin-token",
    "OMNIDESK_PLUGIN_SIGNING_SECRET": "plugin-secret",
    "OPENAI_API_KEY": "sk-test",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "GITHUB_TOKEN": "ghp-test",
}


@pytest.fixture(autouse=True)
def _non_production_env(monkeypatch):
    for name in ("OMNIDESK_ENV", "APP_ENV", "ENV", "KUBERNETES_SERVICE_HOST", REQUIRE_PRODUCTION_GUARDS_ENV):
        monkeypatch.delenv(name, raising=False)


class Allow:
    def verify(self, action_proposal: Any) -> ApprovalDecision:
        return ApprovalDecision(True, "allow", "test")


def _ctx() -> ToolContext:
    return ToolContext(permissions=Allow(), source="local-cli", actor="owner")


class _Spawns:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def __call__(self, *argv: str, **kwargs: Any):
        self.calls.append((argv, kwargs))

        class Proc:
            returncode = 0

            async def communicate(self, data: bytes | None = None):
                return b"", b""

        return Proc()


# --- --mount source ----------------------------------------------------------

@pytest.mark.parametrize("ch", UNSAFE)
def test_bind_mount_source_refuses_spec_and_control_characters(ch):
    with pytest.raises(ValueError, match="--mount source"):
        bind_mount_source(f"/srv/ws/a{ch}b")


def test_bind_mount_source_keeps_ordinary_paths(tmp_path):
    for path in (tmp_path, "/srv/omnidesk-sandbox-workspaces/job 1", "/tmp/工作区/x_y-z.1"):
        assert bind_mount_source(path) == str(path)
    with pytest.raises(ValueError):
        bind_mount_source("")


def test_runner_refuses_a_workspace_path_that_would_add_mount_fields(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIDESK_SANDBOX_IMAGE_ALLOWLIST", raising=False)
    cfg = runner_server.RunnerConfig(container_runtime="docker")
    payload = {"argv": ["pytest"], "readonly": True}
    ok = runner_server._build_docker_command(payload, tmp_path, cfg)
    assert f"type=bind,src={tmp_path},dst=/workspace,readonly" in ok

    evil = tmp_path / "ws,bind-propagation=shared"
    evil.mkdir()
    with pytest.raises(ValueError, match="--mount source"):
        runner_server._build_docker_command(payload, evil, cfg)


def test_runner_rejects_the_path_from_a_workspace_payload(tmp_path, monkeypatch):
    root = tmp_path / "root"
    evil = root / "job,readonly=false"
    evil.mkdir(parents=True)
    monkeypatch.delenv("OMNIDESK_SANDBOX_IMAGE_ALLOWLIST", raising=False)
    cfg = runner_server.RunnerConfig(container_runtime="docker", allowed_workspace_root=root.resolve(), allow_workspace_paths=True)
    payload = {"argv": ["pytest"], "workspace": str(evil)}
    workspace, tmp = runner_server._workspace_from_payload(payload, cfg)
    assert tmp is None
    with pytest.raises(ValueError, match="--mount source"):
        runner_server._build_docker_command(payload, workspace, cfg)


def test_docker_plugin_refuses_a_plugin_path_that_would_add_mount_fields(tmp_path):
    good = tmp_path / "plugin"
    good.mkdir()
    assert f"type=bind,src={good},dst=/plugin,readonly" in DockerPluginTool("p", good / "plugin.py", []).docker_argv()
    evil = tmp_path / "p'x"
    evil.mkdir()
    with pytest.raises(ValueError, match="--mount source"):
        DockerPluginTool("p", evil / "plugin.py", []).docker_argv()


def test_shell_docker_backend_refuses_a_workspace_that_would_add_mount_fields(tmp_path, monkeypatch):
    spawns = _Spawns()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawns)
    cfg = PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log")
    evil = tmp_path / "ws=x"
    evil.mkdir()
    tool = ShellTool(evil, cfg, SandboxConfig(backend="docker"))
    with pytest.raises(ValueError, match="--mount source"):
        asyncio.run(tool.call("run", {"argv": ["pytest"]}, _ctx()))
    assert spawns.calls == []


def test_upgrade_runner_docker_backend_refuses_a_repo_path_that_would_add_mount_fields(tmp_path):
    evil = tmp_path / "repo,src=/"
    evil.mkdir()
    with pytest.raises(ValueError, match="--mount source"):
        SandboxRunner(evil, backend="docker")._docker_command(["pytest"])
    good = tmp_path / "repo"
    good.mkdir()
    assert f"type=bind,src={good},dst=/workspace,readonly" in SandboxRunner(good, backend="docker")._docker_command(["pytest"])


def test_upgrade_runner_reports_an_unusable_repo_path_as_a_failed_result(tmp_path, monkeypatch):
    spawns = _Spawns()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawns)
    evil = tmp_path / "o'brien"
    evil.mkdir()
    result = asyncio.run(SandboxRunner(evil, backend="docker").run(["pytest"]))
    assert result.ok is False and result.exit_code == 126 and "--mount source" in result.output
    assert spawns.calls == []


# --- host argv environment ---------------------------------------------------

PROXY = {"HTTPS_PROXY": "http://user:pw@proxy:3128", "http_proxy": "http://proxy:3128"}
TOOL_SETTINGS = {
    "PYTHONPATH": "/srv/app", "SSL_CERT_FILE": "/etc/ssl/ca.pem", "REQUESTS_CA_BUNDLE": "/etc/ssl/ca.pem",
    "GIT_AUTHOR_NAME": "Bot", "GIT_COMMITTER_EMAIL": "bot@example.test", "NO_PROXY": "localhost",
}


def test_minimal_host_env_keeps_tool_settings_and_drops_secrets():
    environ = {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "SSH_AUTH_SOCK": "/tmp/agent", "PIP_INDEX_URL": "https://u:t@pypi.example.test/simple", **TOOL_SETTINGS, **PROXY, **SECRETS}
    env = minimal_host_env(["pytest", "-q"], environ)
    assert env == {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", **TOOL_SETTINGS}


@pytest.mark.parametrize("git", ["git", "/usr/bin/git", "C:\\Program Files\\Git\\cmd\\git.exe"])
@pytest.mark.parametrize("sub", ["pull", "push", "commit"])
def test_minimal_host_env_lets_git_remote_commands_use_the_ssh_agent(git, sub):
    environ = {"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/tmp/agent", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes", **PROXY, **SECRETS}
    env = minimal_host_env([git, sub], environ)
    assert env["SSH_AUTH_SOCK"] == "/tmp/agent" and env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert not set(SECRETS) & set(env)
    assert ("HTTPS_PROXY" in env) is (sub != "commit")
    assert "SSH_AUTH_SOCK" not in minimal_host_env([git, "status"], environ)
    assert "SSH_AUTH_SOCK" not in minimal_host_env(["pytest", sub], environ)


@pytest.mark.parametrize("argv", [["pip", "install", "-e", "."], ["python3", "-m", "pip", "install", "-e", "."]])
def test_minimal_host_env_gives_proxies_only_to_network_commands(argv):
    environ = {"PATH": "/usr/bin", **PROXY, **SECRETS}
    assert minimal_host_env(argv, environ) == {"PATH": "/usr/bin", **PROXY}
    assert minimal_host_env(["pytest"], environ) == {"PATH": "/usr/bin"}
    assert minimal_host_env(["python3", "-m", "compileall", "."], environ) == {"PATH": "/usr/bin"}


def test_shell_argv_backend_runs_workspace_code_without_gateway_secrets(tmp_path, monkeypatch):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    spawns = _Spawns()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawns)
    cfg = PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log")

    asyncio.run(ShellTool(tmp_path, cfg, SandboxConfig(backend="argv")).call("run", {"argv": ["pytest", "-q"]}, _ctx()))
    env = spawns.calls[0][1]["env"]
    assert env["PATH"] and not set(SECRETS) & set(env)

    # The docker CLI keeps its own environment (the container gets none of it).
    asyncio.run(ShellTool(tmp_path, cfg, SandboxConfig(backend="docker")).call("run", {"argv": ["pytest", "-q"]}, _ctx()))
    assert spawns.calls[1][0][0] == "docker" and spawns.calls[1][1]["env"] is None


def test_upgrade_runner_argv_backend_runs_without_gateway_secrets(tmp_path, monkeypatch):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    spawns = _Spawns()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawns)
    result = asyncio.run(SandboxRunner(tmp_path, backend="argv").run(["pytest", "-q"]))
    assert result.ok is True
    env = spawns.calls[0][1]["env"]
    assert env["PATH"] and not set(SECRETS) & set(env)
