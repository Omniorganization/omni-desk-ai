from __future__ import annotations

from pathlib import Path

from omnidesk_agent.config import PermissionConfig, SandboxConfig
from omnidesk_agent.tools.shell import ShellTool


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
    assert tool._allowed(["git", "push", "origin", "HEAD:codex/source-fix"])
    assert not tool._allowed(["git", "push", "upstream", "main"])
    assert not tool._allowed(["git", "push", "origin", "main"])
