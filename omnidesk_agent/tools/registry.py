from __future__ import annotations

from typing import Any, Optional

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.spec import ActionSpec, ToolSpec, ToolSpecRegistry


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

    def spec_for(self, name: str) -> ToolSpec:
        return ToolSpecRegistry.infer(self.get(name))

    def action_spec(self, tool_name: str, action: str) -> Optional[ActionSpec]:
        spec = self.spec_for(tool_name)
        return spec.actions.get(action) or spec.actions.get("*")

    def describe(self) -> dict[str, dict]:
        return {
            name: self.spec_for(name).to_prompt_dict()
            for name in self._tools
        }

    async def call(self, tool_name: str, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self.get(tool_name)
        try:
            return await tool.call(action, args, ctx)
        except Exception as exc:
            return ToolResult(False, error=f"{type(exc).__name__}: {exc}", summary=f"{tool_name}.{action} failed")
