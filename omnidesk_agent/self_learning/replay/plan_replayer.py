from __future__ import annotations

from typing import Any, Callable, Optional

from omnidesk_agent.self_learning.replay.replay_dataset import ReplayTrace


class PlanReplayer:
    """Replans historical traces with a deterministic planner adapter."""

    def __init__(self, plan_factory: Optional[Callable[[ReplayTrace], dict[str, Any]]] = None):
        self.plan_factory = plan_factory or self._default_plan

    def replay(self, trace: ReplayTrace) -> dict[str, Any]:
        plan = self.plan_factory(trace)
        steps = plan.get("steps", []) if isinstance(plan, dict) else []
        verify_count = sum(1 for step in steps if step.get("verify"))
        rollback_count = sum(1 for step in steps if step.get("rollback"))
        risk_penalty = sum(1 for step in steps if step.get("risk") in {"high", "critical"}) * 0.1
        score = min(1.0, 0.45 + verify_count * 0.15 + rollback_count * 0.1 - risk_penalty)
        if trace.failure_reason and any(trace.failure_reason in str(step.get("handles", "")) for step in steps):
            score += 0.15
        return {"trace_id": trace.trace_id, "plan": plan, "new_score": round(max(0.0, min(1.0, score)), 4)}

    def _default_plan(self, trace: ReplayTrace) -> dict[str, Any]:
        return {
            "goal": trace.goal,
            "steps": [
                {"action": "classify_failure", "risk": "low", "verify": "failure_reason_detected", "rollback": None, "handles": trace.failure_reason},
                {"action": "select_recovery_policy", "risk": "medium", "verify": "policy_has_success_criteria", "rollback": "return_to_previous_policy", "handles": trace.failure_reason},
            ],
        }
