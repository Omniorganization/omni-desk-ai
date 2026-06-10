from __future__ import annotations
from omnidesk_agent.core.models import ToolResult

class EchoTool:
    name = "echo"
    async def call(self, action, args, ctx):
        if action != "say":
            raise ValueError("Unsupported echo action")
        return ToolResult(True, data=args, summary=str(args.get("text", "")))

def register(tool_registry, app_config=None):
    tool_registry.register(EchoTool())
    return ["echo"]
