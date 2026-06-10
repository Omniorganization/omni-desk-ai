from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class SubprocessPluginTool:
    """Run plugin entrypoints in a child Python process via JSON stdin/stdout.

    The plugin entrypoint may expose:
      def call(action: str, args: dict) -> dict

    It must not import the main runtime directly.
    """

    def __init__(self, name: str, entrypoint: Path, permissions: list[str]):
        self.name = name
        self.entrypoint = entrypoint
        self.permissions = permissions

    def spec(self):
        from omnidesk_agent.tools.spec import ActionSpec, ToolSpec, obj_schema
        return ToolSpec(
            name=self.name,
            description=f"Subprocess plugin {self.name}",
            permissions=self.permissions,
            actions={
                "call": ActionSpec(
                    "call",
                    "Call subprocess plugin action",
                    obj_schema({
                        "plugin_action": {"type": "string"},
                        "plugin_args": {"type": "object"},
                    }, required=["plugin_action"], additional=False),
                    risk="high",
                    side_effect=True,
                    requires_approval=True,
                )
            },
        )

    async def call(self, action: str, args: dict[str, Any], ctx) -> Any:
        from omnidesk_agent.core.models import ToolResult
        from omnidesk_agent.tools.base import proposal

        if action != "call":
            raise ValueError("subprocess plugin tool only supports action=call")
        ctx.permissions.verify(proposal(
            self.name,
            "call",
            {"plugin_action": args.get("plugin_action"), "permissions": self.permissions},
            "high",
            "调用隔离子进程插件",
            ctx,
        ))

        payload = {
            "action": args.get("plugin_action"),
            "args": args.get("plugin_args", {}),
        }
        proc = await asyncio.create_subprocess_exec(
            "python3",
            str(self.entrypoint),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode("utf-8")), timeout=30)
        if proc.returncode != 0:
            return ToolResult(False, error=stderr.decode("utf-8", errors="replace")[:4000], summary=f"plugin {self.name} failed")
        try:
            data = json.loads(stdout.decode("utf-8"))
        except Exception:
            data = {"stdout": stdout.decode("utf-8", errors="replace")}
        return ToolResult(True, data=data, summary=f"plugin {self.name} completed")
