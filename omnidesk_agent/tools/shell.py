from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal


class ShellTool:
    name = "shell"

    def __init__(self, workspace_root: Path, permissions_config: PermissionConfig):
        self.workspace_root = workspace_root
        self.permissions_config = permissions_config

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action != "run":
            raise ValueError(f"Unsupported shell action: {action}")
        command = str(args.get("command", ""))
        cwd = Path(args.get("cwd") or self.workspace_root).expanduser().resolve()
        if not str(cwd).startswith(str(self.workspace_root.resolve())):
            raise ValueError(f"cwd must stay inside workspace: {self.workspace_root}")
        timeout = min(int(args.get("timeout", self.permissions_config.max_shell_seconds)), self.permissions_config.max_shell_seconds)
        decision = ctx.permissions.verify(proposal(
            "shell", "run", {"command": command, "cwd": str(cwd), "timeout": timeout}, "critical",
            "即将在本机执行 shell 命令", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: {command}")
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return ToolResult(False, error=f"Command timed out after {timeout}s")
        return ToolResult(
            proc.returncode == 0,
            data={
                "returncode": proc.returncode,
                "stdout": stdout.decode(errors="replace")[-8000:],
                "stderr": stderr.decode(errors="replace")[-8000:],
            },
            error=None if proc.returncode == 0 else f"exit {proc.returncode}",
            summary=f"shell exited {proc.returncode}",
        )
