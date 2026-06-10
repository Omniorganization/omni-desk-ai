from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RiskLevel = Literal["low", "medium", "high", "critical"]


def obj_schema(properties: dict[str, Any], required: list[str] | None = None, additional: bool = False) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": additional,
    }


@dataclass(slots=True)
class ActionSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    risk: RiskLevel = "medium"
    side_effect: bool = False
    requires_approval: bool = True

    def validate_args(self, args: dict[str, Any]) -> list[str]:
        """Small built-in JSON Schema subset validator.

        Full jsonschema dependency is intentionally optional. This validates the most important
        contract fields: type=object, required, properties basic type names, additionalProperties.
        """
        errors: list[str] = []
        schema = self.input_schema or {}
        required = schema.get("required", [])
        props = schema.get("properties", {})
        for key in required:
            if key not in args:
                errors.append(f"missing required arg: {key}")
        if schema.get("additionalProperties") is False:
            for key in args:
                if key not in props:
                    errors.append(f"unknown arg: {key}")
        for key, prop in props.items():
            if key not in args:
                continue
            expected = prop.get("type")
            if expected and not _type_ok(args[key], expected):
                errors.append(f"arg {key} expected {expected}, got {type(args[key]).__name__}")
        return errors


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
                    "output_schema": action.output_schema,
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
                    input_schema=obj_schema({}, additional=True),
                    risk="high",
                    side_effect=True,
                    requires_approval=True,
                )
            },
            permissions=[name],
        )


def _type_ok(value: Any, expected: str | list[str]) -> bool:
    if isinstance(expected, list):
        return any(_type_ok(value, e) for e in expected)
    mapping = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
        "null": type(None),
    }
    py_type = mapping.get(expected)
    if py_type is None:
        return True
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, py_type)
