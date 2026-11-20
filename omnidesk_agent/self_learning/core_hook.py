"""Expose the learning loop as agent-core's ``PlannerExperimentHook``.

The orchestrator calls ``assign(msg)`` before the first planner turn and
applies the returned ``core.planner_policy.PolicyAssignment.policy`` to every
turn of the run. It calls ``after_plan(msg, assignment, turn)`` after the first
turn and ``observe(assignment, RunOutcome)`` at every loop exit (``final`` is
False at an approval pause). Assignments come back rebuilt from their dict,
so everything the rollout needs travels in ``metadata``.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from omnidesk_agent.core.planner_policy import PlannerPolicy as CorePolicy
from omnidesk_agent.core.planner_policy import PolicyAssignment as CoreAssignment
from omnidesk_agent.core.planner_policy import RunOutcome as CoreOutcome
from omnidesk_agent.self_learning.policy_hook import (
    SAFETY_EVENT_CODES,
    PlannerPolicy,
    PolicyAssignment,
    RunOutcome,
)
from omnidesk_agent.self_learning.rollout import unit_hash
from omnidesk_agent.self_learning.runtime_loop import RuntimeLearningLoop


def to_core_policy(policy: PlannerPolicy) -> CorePolicy:
    return CorePolicy(
        policy_id=policy.policy_id,
        version=policy.version,
        prompt_addendum=policy.prompt_addendum,
        skills_block=policy.skills_block,
        rules=tuple(policy.rules),
    )


class CoreExperimentHook:
    def __init__(self, loop: RuntimeLearningLoop):
        self.loop = loop

    def assign(self, msg: Any) -> Optional[CoreAssignment]:
        assignment = self.loop.assign_run(msg)
        if assignment.arm == "none" and assignment.policy.is_empty and self.loop.shadow is None:
            return None  # nothing to apply, nothing to compare: leave the run untouched
        unit = assignment.unit_hash or unit_hash(msg)
        return CoreAssignment(
            experiment_id=assignment.experiment_id or "none",
            unit_id=unit,
            arm=assignment.arm,
            policy=None if assignment.policy.is_empty else to_core_policy(assignment.policy),
            metadata={
                "rollout_id": assignment.rollout_id,
                "bucket": assignment.bucket,
                "policy_id": assignment.policy.policy_id,
                "policy_version": assignment.policy.version,
            },
        )

    def observe(self, assignment: Optional[CoreAssignment], outcome: CoreOutcome) -> None:
        if assignment is None:
            return
        mine = self._local(assignment)
        local_outcome = RunOutcome(
            status=outcome.status if outcome.final else "waiting_approval",
            latency_ms=outcome.latency_ms,
            model_cost_usd=outcome.model_cost_usd,
            model_calls=outcome.model_calls,
            safety_events=tuple(code for code in outcome.safety_events if code in SAFETY_EVENT_CODES),
            replans=outcome.replans,
            steps_executed=outcome.steps_executed,
            error_class=outcome.error_class,
        )
        self.loop.observe_run(mine, local_outcome)
        shadow = self.loop.shadow
        if shadow is not None and outcome.final:
            shadow.observe_production(self._run_key(assignment), local_outcome)

    def after_plan(self, msg: Any, assignment: Optional[CoreAssignment], turn: Any) -> None:
        if assignment is None:
            return
        self.loop.shadow_after_plan(msg, self._local(assignment), turn, run_key=self._run_key(assignment))

    @staticmethod
    def _run_key(assignment: CoreAssignment) -> str:
        return hashlib.sha256(f"{assignment.experiment_id}:{assignment.unit_id}".encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _local(assignment: CoreAssignment) -> PolicyAssignment:
        meta = assignment.metadata or {}
        policy = assignment.policy
        local_policy = PlannerPolicy(
            policy_id=str(meta.get("policy_id") or (policy.policy_id if policy else "planner_current")),
            version=str(meta.get("policy_version") or (policy.version if policy else "")),
            prompt_addendum=policy.prompt_addendum if policy else "",
            skills_block=policy.skills_block if policy else "",
            rules=tuple(policy.rules) if policy else (),
        )
        bucket = meta.get("bucket")
        return PolicyAssignment(
            policy=local_policy,
            arm=assignment.arm,
            rollout_id=meta.get("rollout_id"),
            experiment_id=assignment.experiment_id,
            unit_hash=assignment.unit_id,
            bucket=int(bucket) if isinstance(bucket, int) else None,
        )
