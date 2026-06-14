from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any



ALLOWED_PLUGIN_PERMISSIONS = {
    "files.read", "files.write",
    "browser.read", "browser.write",
    "network.read", "network.write",
    "gmail.read", "gmail.compose",
    "skills.read",
    "memory.read", "memory.write",
    "plugin.call",
}


def validate_plugin_permissions(permissions: list[str]) -> None:
    invalid = [p for p in permissions if p not in ALLOWED_PLUGIN_PERMISSIONS]
    if invalid:
        raise PermissionError(f"unsupported plugin permissions: {invalid}")


class SubprocessPluginTool:
    """Run plugin entrypoints in a child Python process via JSON stdin/stdout.

    The plugin entrypoint may expose:
      def call(action: str, args: dict) -> dict

    It must not import the main runtime directly.
    """

    def __init__(self, name: str, entrypoint: Path, permissions: list[str], timeout_seconds: int = 30, max_output_bytes: int = 200000):
        self.name = name
        self.entrypoint = entrypoint
        validate_plugin_permissions(permissions)
        self.permissions = permissions
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

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
            "granted_permissions": self.permissions,
        }
        proc = await asyncio.create_subprocess_exec(
            "python3",
            "-I",
            str(self.entrypoint),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.entrypoint.parent),
            env={"PYTHONNOUSERSITE": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode("utf-8")), timeout=self.timeout_seconds)
        if len(stdout) > self.max_output_bytes or len(stderr) > self.max_output_bytes:
            return ToolResult(False, error="plugin output exceeded limit", summary=f"plugin {self.name} output limit exceeded")
        if proc.returncode != 0:
            return ToolResult(False, error=stderr.decode("utf-8", errors="replace")[:4000], summary=f"plugin {self.name} failed")
        try:
            data = json.loads(stdout.decode("utf-8"))
        except Exception:
            data = {"stdout": stdout.decode("utf-8", errors="replace")}
        return ToolResult(True, data=data, summary=f"plugin {self.name} completed")
