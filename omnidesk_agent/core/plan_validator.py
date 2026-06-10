from __future__ import annotations
from typing import Optional

from dataclasses import dataclass

from omnidesk_agent.core.models import Plan, PlanStep
from omnidesk_agent.core.plan_schema import StructuredPlan


@dataclass
class PlanValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    plan: Optional[StructuredPlan] = None


class PlanValidator:
    def __init__(self, tool_registry):
        self.tool_registry = tool_registry

    def validate(self, plan: StructuredPlan) -> PlanValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        available = set(self.tool_registry.names())

        for i, step in enumerate(plan.steps):
            if step.tool not in available:
                errors.append(f"step[{i}] unknown tool: {step.tool}")
                continue

            action_spec = None
            if hasattr(self.tool_registry, "action_spec"):
                action_spec = self.tool_registry.action_spec(step.tool, step.action)

            if action_spec is None:
                errors.append(f"step[{i}] unsupported action for {step.tool}: {step.action}")
            else:
                # Reuse ActionSpec's single source of truth for argument validation.
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
