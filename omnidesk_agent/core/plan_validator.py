from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
            if "*" not in actions and step.action not in actions:
                errors.append(f"step[{i}] unsupported action for {step.tool}: {step.action}")

            if not step.expected_result:
                errors.append(f"step[{i}] missing expected_result")

            if step.tool in {"computer", "browser", "gmail", "channels", "ui_bridge", "shell"} and not step.requires_approval:
                warnings.append(f"step[{i}] side-effect tool should normally require approval: {step.tool}")

            # Ensure expected_result is present inside args so older tools still enforce it.
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
                    args=s.args,
                    risk=s.risk,
                    requires_approval=s.requires_approval,
                )
                for s in plan.steps
            ],
            rationale=rationale,
        )
