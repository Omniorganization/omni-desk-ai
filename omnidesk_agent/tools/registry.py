from __future__ import annotations

from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.spec import ToolSpecRegistry


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Any] = {}

    def register(self, tool: Any) -> None:
        if not getattr(tool, "name", None):
            raise ValueError("Tool must have a name")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def get(self, name: str) -> Any:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def describe(self) -> dict[str, dict]:
        return {
            name: ToolSpecRegistry.infer(tool).to_prompt_dict()
            for name, tool in self._tools.items()
        }

    async def call(self, tool_name: str, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self.get(tool_name)
        try:
            return await tool.call(action, args, ctx)
        except Exception as exc:
            return ToolResult(False, error=f"{type(exc).__name__}: {exc}", summary=f"{tool_name}.{action} failed")
