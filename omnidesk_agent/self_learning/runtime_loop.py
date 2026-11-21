from __future__ import annotations

import time
from typing import Any, Optional

from omnidesk_agent.self_learning.policy_hook import (
    EMPTY_POLICY,
    PolicyAssignment,
    RunOutcome,
    classify_safety_events,
)


class RuntimeLearningLoop:
    """The orchestrator's experiment-policy hook.

    ``assign(msg)`` picks the planner policy for one run: the active learned
    policy, or the candidate when a rollout (A/B or canary) puts the run in its
    treatment arm. ``observe(assignment, outcome)`` feeds the rollout gate,
    which rolls back automatically. ``after_plan`` hands sampled runs to the
    shadow runner. Without a rollout controller this hook changes nothing and
    records nothing: no experiment runs unless an operator starts one.

    ``assign_policy`` / ``observe_result`` keep the old call shape for the
    current orchestrator; they return and accept JSON-safe dicts.

    The methods are deliberately not named ``assign``/``observe``/``after_plan``:
    the agent-core orchestrator switches to its PlannerExperimentHook protocol
    (core.planner_policy types) when it sees those names. The wiring change adds
    them with the core types; until then the orchestrator uses the legacy calls.
    """

    MAX_LIVE_ASSIGNMENTS = 10_000

    def __init__(
        self,
        experiments: Any = None,
        world_model: Any = None,
        roi: Any = None,
        *,
        rollout: Any = None,
        shadow: Any = None,
        registry: Any = None,
    ):
        self.experiments = experiments
        self.rollout = rollout
        self.shadow = shadow
        self.registry = registry if registry is not None else getattr(rollout, "registry", None)
        self.metrics: Any = None
        self._live: dict[str, PolicyAssignment] = {}

    # ------------------------------------------------------------- new hook
    def assign_run(self, msg: Any) -> PolicyAssignment:
        try:
            if self.rollout is not None:
                assignment = self.rollout.assign(msg)
            elif self.registry is not None:
                assignment = PolicyAssignment(policy=self.registry.active_policy())
            else:
                assignment = PolicyAssignment(policy=EMPTY_POLICY)
        except Exception:
            # A broken registry (e.g. a tampered row) must not change planning.
            self._metric("omnidesk_learning_policy_errors_total")
            assignment = PolicyAssignment(policy=EMPTY_POLICY)
        if assignment.arm != "none":
            self._metric("omnidesk_learning_experiment_assignments_total", arm=assignment.arm)
        return assignment

    def observe_run(self, assignment: Optional[PolicyAssignment], outcome: RunOutcome) -> Optional[dict[str, Any]]:
        if assignment is None or self.rollout is None or assignment.arm == "none":
            return None
        decision = self.rollout.observe(assignment, outcome)
        self._metric("omnidesk_learning_experiment_observations_total", arm=assignment.arm, status=outcome.status)
        if decision and decision.get("status") == "rolled_back":
            self._metric("omnidesk_learning_rollout_rollbacks_total")
        return decision

    def shadow_after_plan(self, msg: Any, assignment: Optional[PolicyAssignment], plan: Any, *, run_key: str) -> None:
        if self.shadow is not None and (assignment is None or assignment.arm != "treatment"):
            self.shadow.after_plan(msg, plan, run_key=run_key)

    # ----------------------------------------------------- legacy call shape
    def assign_policy(self, msg: Any) -> dict[str, Any]:
        assignment = self.assign_run(msg)
        payload = assignment.to_dict()
        if assignment.arm != "none":
            token = f"{assignment.rollout_id}:{assignment.unit_hash}:{time.time_ns()}"
            self._live[token] = assignment
            while len(self._live) > self.MAX_LIVE_ASSIGNMENTS:  # runs that never reported back
                self._live.pop(next(iter(self._live)))
            payload["assignment_token"] = token
        return payload

    def observe_result(
        self,
        assignment: Optional[dict[str, Any]],
        *,
        status: str,
        result_count: int = 0,
        cost: Optional[float] = None,
        latency_ms: Optional[float] = None,
        safety_violation: bool = False,
        results: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[dict[str, Any]]:
        """Old orchestrator path. ``safety_violation`` (a substring guess) is ignored;
        safety comes from structured result prefixes."""
        if not assignment or "assignment_token" not in assignment:
            return None
        live = self._live.pop(str(assignment["assignment_token"]), None)
        if live is None:
            return None
        outcome = RunOutcome(
            status=status,
            latency_ms=latency_ms,
            model_cost_usd=cost,
            safety_events=classify_safety_events(results or []),
            steps_executed=int(result_count or 0),
        )
        return self.observe_run(live, outcome)

    def _metric(self, name: str, **labels: Any) -> None:
        inc = getattr(getattr(self, "metrics", None), "inc", None)
        if callable(inc):
            inc(name, **labels)
