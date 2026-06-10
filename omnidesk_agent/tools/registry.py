from __future__ import annotations

from typing import Any
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.spec import ToolSpecRegistry
from omnidesk_agent.tools.base import Tool, ToolContext


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    async def call(self, tool_name: str, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(False, error=f"Unknown tool: {tool_name}")
        try:
            return await tool.call(action, args, ctx)
        except Exception as exc:
            return ToolResult(False, error=f"{type(exc).__name__}: {exc}")
