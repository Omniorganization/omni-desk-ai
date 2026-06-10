from pathlib import Path
from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.tools.shell import ShellTool


def test_shell_blocks_non_allowlisted(tmp_path: Path):
    tool = ShellTool(tmp_path, PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"))
    assert not tool._allowed(["bash", "-c", "echo x"])
    assert tool._allowed(["git", "status"])
