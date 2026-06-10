from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RiskLevel = Literal["low", "medium", "high", "critical"]


@dataclass(slots=True)
class ActionSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    risk: RiskLevel = "medium"
    side_effect: bool = False
    requires_approval: bool = True


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    actions: dict[str, ActionSpec]
    permissions: list[str] = field(default_factory=list)

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permissions": self.permissions,
            "actions": {
                action_name: {
                    "description": action.description,
                    "input_schema": action.input_schema,
                    "risk": action.risk,
                    "side_effect": action.side_effect,
                    "requires_approval": action.requires_approval,
                }
                for action_name, action in self.actions.items()
            },
        }


class ToolSpecRegistry:
    @staticmethod
    def infer(tool: Any) -> ToolSpec:
        if hasattr(tool, "spec") and callable(getattr(tool, "spec")):
            return tool.spec()
        name = getattr(tool, "name", tool.__class__.__name__)
        return ToolSpec(
            name=name,
            description=f"{name} tool",
            actions={
                "*": ActionSpec(
                    name="*",
                    description="Tool did not provide action-level schema",
                    risk="high",
                    side_effect=True,
                    requires_approval=True,
                )
            },
            permissions=[name],
        )
