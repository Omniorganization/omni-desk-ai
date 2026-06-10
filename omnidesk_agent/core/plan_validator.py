from __future__ import annotations
from typing import Optional, Any

from dataclasses import dataclass

from omnidesk_agent.core.models import Plan, PlanStep
from omnidesk_agent.core.plan_schema import StructuredPlan
from omnidesk_agent.tools.spec import ActionSpec


@dataclass
class PlanValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    plan: Optional[StructuredPlan] = None


class PlanValidator:
    def __init__(self, tool_registry):
        self.tool_registry = tool_registry

    def _action_spec(self, tool: str, action: str) -> Optional[ActionSpec]:
        if hasattr(self.tool_registry, "action_spec"):
            spec = self.tool_registry.action_spec(tool, action)
            if spec is not None:
                return spec

        if hasattr(self.tool_registry, "describe"):
            desc = self.tool_registry.describe() or {}
            tool_desc = desc.get(tool, {})
            action_desc = (tool_desc.get("actions") or {}).get(action)
            if action_desc is None:
                action_desc = (tool_desc.get("actions") or {}).get("*")
            if action_desc is not None:
                return ActionSpec(
                    name=action,
                    description=str(action_desc.get("description", action)),
                    input_schema=action_desc.get("input_schema") or {},
                    output_schema=action_desc.get("output_schema") or {},
                    risk=action_desc.get("risk", "medium"),
                    side_effect=bool(action_desc.get("side_effect", False)),
                    requires_approval=bool(action_desc.get("requires_approval", True)),
                )
        return None

    def validate(self, plan: StructuredPlan) -> PlanValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        available = set(self.tool_registry.names())

        for i, step in enumerate(plan.steps):
            if step.tool not in available:
                errors.append(f"step[{i}] unknown tool: {step.tool}")
                continue

            action_spec = self._action_spec(step.tool, step.action)
            if action_spec is None:
                errors.append(f"step[{i}] unsupported action for {step.tool}: {step.action}")
            else:
                normalized_args = dict(step.args)
                normalized_args.setdefault("expected_result", step.expected_result)
                schema_errors = action_spec.validate_args(normalized_args)
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
