from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from omnidesk_agent.config import PermissionConfig, SandboxConfig
from omnidesk_agent.core.models import ApprovalDecision
from omnidesk_agent.security.command_policy import (
    GIT_EXEC_HARDENING_ARGS,
    SAFE_CI_ALLOWED_PREFIXES,
    UPGRADE_ALLOWED_PREFIXES,
    argv_allowed,
    harden_exec_argv,
)
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.shell import ShellTool

ALL_PREFIXES = SAFE_CI_ALLOWED_PREFIXES + UPGRADE_ALLOWED_PREFIXES


def test_shell_blocks_non_allowlisted(tmp_path: Path):
    tool = ShellTool(tmp_path, PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"))
    assert not tool._allowed(["bash", "-c", "echo x"])
    assert tool._allowed(["git", "status"])


def test_shell_blocks_argv_backend_in_production_runtime(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    tool = ShellTool(
        tmp_path,
        PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"),
        SandboxConfig(backend="argv"),
    )
    allowed, reason = tool._runtime_backend_allowed(["git", "status"])
    assert allowed is False
    assert reason and "argv backend is forbidden" in reason


def test_shell_upgrade_commands_require_isolated_runner(monkeypatch, tmp_path: Path):
    cfg = PermissionConfig(
        default_mode="allow",
        audit_log=tmp_path / "audit.log",
        shell_upgrade_enabled=True,
    )
    monkeypatch.delenv("OMNIDESK_UPGRADE_RUNNER_ISOLATED", raising=False)
    local_tool = ShellTool(tmp_path, cfg)
    assert not local_tool._allowed(["pip", "install", "-e", "."])

    monkeypatch.setenv("OMNIDESK_UPGRADE_RUNNER_ISOLATED", "true")
    isolated_tool = ShellTool(tmp_path, cfg)
    assert isolated_tool._allowed(["pip", "install", "-e", "."])


def test_shell_upgrade_git_add_and_push_are_scoped(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OMNIDESK_UPGRADE_RUNNER_ISOLATED", "true")
    cfg = PermissionConfig(
        default_mode="allow",
        audit_log=tmp_path / "audit.log",
        shell_upgrade_enabled=True,
    )
    tool = ShellTool(tmp_path, cfg)

    safe_file = tmp_path / "safe.py"
    safe_file.write_text("print('ok')\n", encoding="utf-8")

    assert tool._allowed(["git", "add", "safe.py"])
    assert not tool._allowed(["git", "add", "../outside.py"])
    assert not tool._allowed(["git", "add", "/tmp/outside.py"])
    assert not tool._allowed(["git", "add", "."])
    assert not tool._allowed(["git", "add", "*.py"])
    assert not tool._allowed(["git", "add", ":(top)safe.py"])
    assert tool._allowed(["git", "push", "origin", "HEAD:codex/source-fix"])
    assert not tool._allowed(["git", "push", "upstream", "main"])
    assert not tool._allowed(["git", "push", "origin", "main"])


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status", "--short"],
        ["git", "status", "-sb"],
        ["git", "diff", "--stat"],
        ["git", "diff", "HEAD~1..HEAD", "--", "omnidesk_agent"],
        ["git", "diff", "--output-indicator-new=+"],
        ["git", "log", "--oneline", "-n", "5"],
        ["git", "log", "-5"],
        ["git", "log", "-p", "--follow", "omnidesk_agent/config.py"],
        ["git", "log", "-Sneedle"],
        ["git", "log", "origin/main..HEAD"],
        ["git", "branch", "-a"],
        ["git", "ls-tree", "-r", "HEAD"],
        ["git", "add", "safe.py"],
        ["git", "commit", "-m", "fix: tighten shell policy"],
        ["git", "push", "origin", "HEAD:codex/source-fix"],
        ["git", "pull"],
        ["git", "pull", "--rebase", "origin", "main"],
        ["pip", "install", "-e", "."],
        ["python3", "-m", "pip", "install", "-e", "."],
        ["pytest"],
        ["pytest", "-q", "tests/test_shell_allowlist.py"],
        ["pytest", "-x", "-k", "not slow", "-rA", "--tb=short"],
        ["pytest", "-p", "no:cacheprovider"],
        ["pytest", "--co"],
        ["pytest", "--junitxml=reports/junit.xml"],
        ["ruff", "check", "."],
        ["ruff", "check", "--output-format=json", "omnidesk_agent"],
        ["python", "-m", "compileall", "."],
        ["python3", "-m", "compileall", "-q", "omnidesk_agent"],
    ],
)
def test_command_policy_keeps_legitimate_commands(argv, tmp_path: Path):
    assert argv_allowed(argv, ALL_PREFIXES, workspace_root=tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        # git: arbitrary file writes, reads outside the repo, command spawning, config overrides
        ["git", "diff", "--output=pwned.txt"],
        ["git", "diff", "--output", "pwned.txt"],
        ["git", "log", "-p", "--output=pwned.txt"],
        ["git", "diff", "--no-index", "/etc/passwd", "/dev/null"],
        ["git", "diff", "/etc/passwd", "/dev/null"],
        ["git", "diff", "--", "../outside.txt"],
        ["git", "pull", "--upload-pack=sh -c id", "origin"],
        ["git", "pull", "--upload-pa=sh -c id", "origin"],
        ["git", "pull", "--receive-pack=x"],
        ["git", "pull", "--exec=x"],
        ["git", "log", "-c"],
        ["git", "commit", "-c", "HEAD"],
        ["git", "commit", "-nc", "HEAD"],
        ["git", "commit", "-acHEAD"],
        ["git", "status", "--git-dir=/tmp/other"],
        ["git", "status", "--work-tree=other"],
        ["git", "log", "--exec-path=/tmp"],
        ["git", "log", "--config-env=core.pager=EVIL"],
        ["git", "log", "-O/etc/passwd"],
        # pytest: basetemp deletion, plugin loading, config redirection
        ["pytest", "--basetemp=/home/user"],
        ["pytest", "--basetemp", "victim"],
        ["pytest", "-p", "evil_plugin"],
        ["pytest", "-pevil_plugin"],
        ["pytest", "-xp", "evil_plugin"],
        ["pytest", "--rootdir=elsewhere"],
        ["pytest", "-c", "evil.ini"],
        ["pytest", "--config-file=evil.ini"],
        ["pytest", "--confcutdir", "elsewhere"],
        ["pytest", "-o", "addopts=-p evil"],
        ["pytest", "--override-ini=addopts=-p evil"],
        ["pytest", "--pyargs", "os"],
        ["pytest", "--junitxml=/tmp/out.xml"],
        ["pytest", "/abs/tests"],
        # ruff: output files and config/cache redirection
        ["ruff", "check", "--output-file=pwned.txt"],
        ["ruff", "check", "-o", "pwned.txt"],
        ["ruff", "check", "--config", "evil.toml"],
        ["ruff", "check", "--cache-dir=/tmp/cache"],
        # editable install must stay exactly `pip install -e .`
        ["pip", "install", "-e", ".", "evilpkg", "."],
        ["pip", "install", "-e", "--index-url=https://evil.example", "."],
        ["python3", "-m", "pip", "install", "-e", "-r", "requirements.txt", "."],
        ["python", "-m", "compileall", "/etc"],
    ],
)
def test_command_policy_denies_dangerous_extra_args(argv, tmp_path: Path):
    assert not argv_allowed(argv, ALL_PREFIXES, workspace_root=tmp_path)


def test_command_policy_generic_rules_cover_custom_prefixes(tmp_path: Path):
    custom = [["make", "test"]]
    assert argv_allowed(["make", "test", "unit"], custom, workspace_root=tmp_path)
    assert not argv_allowed(["make", "test", "--output=x"], custom, workspace_root=tmp_path)
    assert not argv_allowed(["make", "test", "-o", "x"], custom, workspace_root=tmp_path)
    assert not argv_allowed(["make", "test", "/etc/passwd"], custom, workspace_root=tmp_path)


def test_harden_exec_argv_only_prefixes_git():
    assert harden_exec_argv(["git", "status"]) == ["git", *GIT_EXEC_HARDENING_ARGS, "status"]
    assert harden_exec_argv(["pytest", "-q"]) == ["pytest", "-q"]
    assert "core.fsmonitor=" in GIT_EXEC_HARDENING_ARGS
    assert "core.hooksPath=/dev/null" in GIT_EXEC_HARDENING_ARGS
    # The injected flags must never pass the user-facing policy themselves.
    assert not argv_allowed(harden_exec_argv(["git", "status"]), [["git"]])


class _RecordingPermissions:
    def __init__(self):
        self.proposals = []

    def verify(self, proposal):
        self.proposals.append(proposal)
        return ApprovalDecision(True, "allow", "test")


class _Proc:
    returncode = 0

    async def communicate(self):
        return b"ok", b""

    def kill(self):
        pass


def test_shell_argv_backend_spawns_hardened_git_but_approves_plain_argv(monkeypatch, tmp_path: Path):
    created = []

    async def fake_exec(*argv, cwd, stdout, stderr):
        created.append(list(argv))
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    tool = ShellTool(tmp_path, PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"), SandboxConfig(backend="argv"))
    perms = _RecordingPermissions()

    result = asyncio.run(tool.call("run", {"argv": ["git", "status"]}, ToolContext(permissions=perms)))

    assert result.ok is True
    assert created == [["git", *GIT_EXEC_HARDENING_ARGS, "status"]]
    assert result.data["argv"] == ["git", "status"]
    assert perms.proposals[0].args["argv"] == ["git", "status"]

    blocked = asyncio.run(tool.call("run", {"argv": ["git", "diff", "--output=pwned"]}, ToolContext(permissions=perms)))
    assert blocked.ok is False
    assert "allowlist" in blocked.error
    assert len(created) == 1


def test_shell_docker_backend_hardens_git_and_keeps_readonly_mount(tmp_path: Path):
    tool = ShellTool(tmp_path, PermissionConfig(), SandboxConfig(backend="docker"))
    argv = tool._docker_argv(["git", "status"])
    assert argv[-len(GIT_EXEC_HARDENING_ARGS) - 2:] == ["git", *GIT_EXEC_HARDENING_ARGS, "status"]
    assert any("dst=/workspace" in part and part.endswith("readonly") for part in argv)


def test_sandbox_runner_applies_shared_arg_policy(tmp_path: Path):
    runner = SandboxRunner(tmp_path)
    assert runner.allowed(["pytest", "tests/test_x.py"])
    assert not runner.allowed(["pytest", "--basetemp=/home/user"])
    assert not runner.allowed(["git", "diff", "--output=pwned"])
    result = asyncio.run(runner.run(["pytest", "-p", "evil_plugin"]))
    assert result.ok is False and result.exit_code == 126
    cmd = runner._docker_command(["git", "diff"])
    assert cmd[-len(GIT_EXEC_HARDENING_ARGS) - 2:] == ["git", *GIT_EXEC_HARDENING_ARGS, "diff"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")
def test_git_hardening_flags_disable_repo_fsmonitor_hook(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    git = shutil.which("git")
    subprocess.run([git, "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run([git, "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True, capture_output=True)
    (repo / "file.txt").write_text("x\n", encoding="utf-8")

    subprocess.run([git, "status", "--short"], cwd=repo, capture_output=True, check=False)
    if not marker.exists():
        pytest.skip("this git build does not invoke core.fsmonitor hooks")
    marker.unlink()

    hardened = harden_exec_argv(["git", "status", "--short"])
    result = subprocess.run([git, *hardened[1:]], cwd=repo, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
