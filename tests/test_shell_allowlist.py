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
