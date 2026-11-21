"""Shell, test and git tools run sandboxed work with the hardened lifecycle and policy.

Regression tests for the 2026-11-21 audit (medium): the shell docker backend
killed only the docker client on timeout and mounted OmniDesk's state, a plan
could raise the approved command's timeout, TestTool ran pytest at medium risk
while ShellTool calls the same thing critical, and GitTool ran git without the
repository-config hardening and accepted the refspec 'ai/x:main'.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import textwrap
from types import SimpleNamespace

import pytest

from omnidesk_agent.config import PermissionConfig, SandboxConfig
from omnidesk_agent.tools import git_tool as git_tool_module
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.git_tool import GitTool
from omnidesk_agent.tools.shell import ShellTool
from omnidesk_agent.tools.test_tool import TestTool


class _RecordingPermissions:
    """Allows everything and records what was proposed; the subject is the tool, not the policy."""

    def __init__(self) -> None:
        self.proposals: list = []
        self.metrics = None

    def verify(self, proposal):
        self.proposals.append(proposal)
        return SimpleNamespace(allowed=True, mode="allow", reason="test")


def _ctx(permissions=None) -> ToolContext:
    return ToolContext(permissions=permissions or _RecordingPermissions())  # type: ignore[arg-type]


def _fake_docker_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    (bin_dir / "docker").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> {log}
        if [ "$1" = "run" ]; then exec sleep 30; fi
        exit 0
        """))
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


@pytest.mark.skipif(os.name != "posix", reason="POSIX fake container runtime")
def test_self_upgrade_runner_timeout_kills_the_container(tmp_path, monkeypatch):
    from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner

    log = _fake_docker_on_path(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    result = asyncio.run(SandboxRunner(repo, backend="docker").run(["python", "-m", "compileall", "."], timeout=1))

    assert not result.ok and "Timed out after 1s" in result.output
    calls = log.read_text().splitlines()
    name = calls[0].split()[2]
    assert f"kill {name}" in calls and f"rm -f {name}" in calls


@pytest.mark.skipif(os.name != "posix", reason="POSIX fake container runtime")
def test_shell_docker_timeout_kills_the_container(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    (bin_dir / "docker").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> {log}
        if [ "$1" = "run" ]; then exec sleep 30; fi
        exit 0
        """))
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    shell = ShellTool(workspace, PermissionConfig(), SandboxConfig(backend="docker", timeout_seconds=1))

    result = asyncio.run(shell.call("run", {"argv": ["pytest", "-q"], "timeout": 600}, _ctx()))

    assert not result.ok and "timed out after 1s" in result.error  # the plan could not raise it to 600
    calls = log.read_text().splitlines()
    name = calls[0].split()[2]
    assert calls[0].startswith("run --name omnidesk-sbx-")
    assert f"kill {name}" in calls and f"rm -f {name}" in calls


def test_shell_docker_argv_masks_runtime_state(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "approvals.sqlite3").write_text("state")
    (workspace / "code.py").write_text("x")
    shell = ShellTool(workspace, PermissionConfig(), SandboxConfig(backend="docker"))

    argv = shell._docker_argv(["pytest"])

    assert "type=bind,src=/dev/null,dst=/workspace/approvals.sqlite3,readonly" in argv
    assert argv.index("type=bind,src=/dev/null,dst=/workspace/approvals.sqlite3,readonly") < argv.index(shell.sandbox_cfg.docker_image)


def test_test_tool_proposes_pytest_as_critical(tmp_path, monkeypatch):
    tool = TestTool(tmp_path, SandboxConfig(backend="docker"))

    async def fake_run(command, timeout=120):
        return SimpleNamespace(ok=True, output="ok")

    monkeypatch.setattr(tool.tester, "run", fake_run)
    permissions = _RecordingPermissions()

    asyncio.run(tool.call("run", {"command": "pytest -q"}, _ctx(permissions)))

    assert [p.risk for p in permissions.proposals] == ["critical"]


@pytest.mark.parametrize("branch", ["ai/x:main", "ai/x:refs/heads/main", "+ai/x", "ai/../main", "refs/heads/ai/x", "ai/-x", "ai/x.lock", "main", "ai/"])
def test_git_tool_refuses_refspecs_and_non_agent_branches(tmp_path, monkeypatch, branch):
    calls: list = []
    monkeypatch.setattr(git_tool_module.subprocess, "run", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))

    for action in ("push", "checkout_new_branch"):
        result = asyncio.run(GitTool(tmp_path).call(action, {"branch": branch}, _ctx()))
        assert not result.ok and "ai/" in result.error

    assert calls == []


def test_git_tool_runs_hardened_git_without_gateway_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_ADMIN_TOKEN", "a" * 40)
    calls: list[tuple[list, dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(git_tool_module.subprocess, "run", fake_run)

    assert asyncio.run(GitTool(tmp_path).call("push", {"branch": "ai/fix-1"}, _ctx())).ok
    assert asyncio.run(GitTool(tmp_path).call("diff", {}, _ctx())).ok

    push_argv, push_kwargs = calls[0]
    assert push_argv[:3] == ["git", "-c", "core.fsmonitor="] and "core.hooksPath=/dev/null" in push_argv
    assert push_argv[-4:] == ["--receive-pack=git-receive-pack", "-u", "origin", "ai/fix-1"]
    assert "OMNIDESK_ADMIN_TOKEN" not in push_kwargs["env"]
    assert "--no-ext-diff" in calls[1][0]
