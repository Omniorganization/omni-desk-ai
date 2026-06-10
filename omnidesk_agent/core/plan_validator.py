from __future__ import annotations

from dataclasses import dataclass

from omnidesk_agent.core.models import Plan, PlanStep
from omnidesk_agent.core.plan_schema import StructuredPlan


@dataclass(slots=True)
class PlanValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    plan: StructuredPlan | None = None


class PlanValidator:
    def __init__(self, tool_registry):
        self.tool_registry = tool_registry

    def validate(self, plan: StructuredPlan) -> PlanValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        available = set(self.tool_registry.names())
        tool_specs = self.tool_registry.describe() if hasattr(self.tool_registry, "describe") else {}

        for i, step in enumerate(plan.steps):
            if step.tool not in available:
                errors.append(f"step[{i}] unknown tool: {step.tool}")
                continue

            spec = tool_specs.get(step.tool, {})
            actions = spec.get("actions", {})
            action_spec = actions.get(step.action) or actions.get("*")
            if not action_spec:
                errors.append(f"step[{i}] unsupported action for {step.tool}: {step.action}")
            else:
                schema_errors = _validate_against_schema(step.args, action_spec.get("input_schema", {}))
                for err in schema_errors:
                    errors.append(f"step[{i}] {step.tool}.{step.action}: {err}")

            if not step.expected_result:
                errors.append(f"step[{i}] missing expected_result")

            if step.tool in {"computer", "browser", "gmail", "channels", "ui_bridge", "shell"} and not step.requires_approval:
                warnings.append(f"step[{i}] side-effect tool should normally require approval: {step.tool}")

            step.args.setdefault("expected_result", step.expected_result)

        return PlanValidationResult(ok=not errors, errors=errors, warnings=warnings, plan=plan if not errors else None)

    @staticmethod
    def to_runtime_plan(plan: StructuredPlan, rationale: str = "validated structured plan") -> Plan:
        return Plan(
            goal=plan.goal,
            steps=[
                PlanStep(
                    description=s.description,
                    tool=s.tool,
                    action=s.action,
                    args={
                        **s.args,
                        "retry_policy": s.retry_policy.model_dump(),
                        "rollback_action": s.rollback_action,
                        "verification": s.verification.model_dump() if s.verification else None,
                    },
                    risk=s.risk,
                    requires_approval=s.requires_approval,
                )
                for s in plan.steps
            ],
            rationale=rationale,
        )


def _validate_against_schema(args: dict, schema: dict) -> list[str]:
    errors: list[str] = []
    required = schema.get("required", [])
    props = schema.get("properties", {})
    for key in required:
        if key not in args:
            errors.append(f"missing required arg: {key}")
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in props and key not in {"expected_result", "retry_policy", "rollback_action", "verification"}:
                errors.append(f"unknown arg: {key}")
    for key, prop in props.items():
        if key not in args:
            continue
        expected = prop.get("type")
        if expected and not _type_ok(args[key], expected):
            errors.append(f"arg {key} expected {expected}, got {type(args[key]).__name__}")
    return errors


def _type_ok(value, expected) -> bool:
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
    typ = mapping.get(expected)
    if typ is None:
        return True
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, typ)
