from __future__ import annotations

from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.self_upgrade.tester import UpgradeTester
from omnidesk_agent.tools.base import ToolContext, proposal


class TestTool:
    name = "test"

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.tester = UpgradeTester(self.repo_root)

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action != "run":
            return ToolResult(False, error=f"Unsupported test action: {action}")
        command = str(args.get("command") or "python -m compileall omnidesk_agent")
        if any(bad in command for bad in ["--force", " rm ", "sudo", "shutdown", "reboot"]):
            return ToolResult(False, error="Refusing unsafe test command")
        decision = ctx.permissions.verify(proposal("test", "run", {"command": command}, "medium", "运行升级测试命令", ctx))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run test: {command}")
        result = await self.tester.run(command)
        return ToolResult(result.ok, data=result, summary=result.output[-2000:], error=None if result.ok else result.output[-2000:])
