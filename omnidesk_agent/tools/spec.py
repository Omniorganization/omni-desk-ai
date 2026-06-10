from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union

RiskLevel = Literal["low", "medium", "high", "critical"]


def obj_schema(properties: dict[str, Any], required: Optional[list[str]] = None, additional: bool = False) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": additional,
    }


def normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if not schema:
        return obj_schema({}, additional=True)
    if schema.get("type") == "object" and "properties" in schema:
        return schema

    # Backward-compatible shorthand: {"url": "string"} or {"count": "integer"}.
    properties: dict[str, Any] = {}
    required: list[str] = []
    for key, value in schema.items():
        if isinstance(value, str):
            if value.startswith("list[") or value.startswith("list"):
                properties[key] = {"type": "array"}
            else:
                properties[key] = {"type": _json_type(value)}
            required.append(key)
        elif isinstance(value, dict):
            properties[key] = value
            required.append(key)
    return obj_schema(properties, required=required, additional=True)


@dataclass
class ActionSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    risk: RiskLevel = "medium"
    side_effect: bool = False
    requires_approval: bool = True

    def __post_init__(self) -> None:
        self.input_schema = normalize_schema(self.input_schema)
        if self.output_schema:
            self.output_schema = normalize_schema(self.output_schema)

    def validate_args(self, args: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        schema = self.input_schema or {}
        required = schema.get("required", [])
        props = schema.get("properties", {})
        for key in required:
            if key not in args:
                errors.append(f"missing required arg: {key}")
        runtime_keys = {"expected_result", "retry_policy", "rollback_action", "verification"}
        if schema.get("additionalProperties") is False:
            for key in args:
                if key not in props and key not in runtime_keys:
                    errors.append(f"unknown arg: {key}")
        for key, prop in props.items():
            if key not in args:
                continue
            expected = prop.get("type")
            if expected and not _type_ok(args[key], expected):
                errors.append(f"arg {key} expected {expected}, got {type(args[key]).__name__}")
        return errors


@dataclass
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


def _json_type(value: str) -> str:
    value = value.lower()
    if value in {"str", "string"}:
        return "string"
    if value in {"int", "integer"}:
        return "integer"
    if value in {"float", "number"}:
        return "number"
    if value in {"bool", "boolean"}:
        return "boolean"
    if value.startswith("list") or value.startswith("array"):
        return "array"
    if value in {"dict", "object"}:
        return "object"
    return value


def _type_ok(value: Any, expected: Union[str, list]) -> bool:
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
