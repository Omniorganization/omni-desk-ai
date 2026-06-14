from __future__ import annotations

import time
from typing import Any, Optional

from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.self_learning.experiments.experiment_manager import ExperimentManager, ExperimentSpec
from omnidesk_agent.self_learning.experiments.metric_collector import ExperimentObservation
from omnidesk_agent.self_learning.roi.learning_roi import LearningROIAnalyzer
from omnidesk_agent.self_learning.promotion.policy import LearningPromotionPolicy
from omnidesk_agent.self_learning.world_model.model import WorldModel


class RuntimeLearningLoop:
    """Online closed-loop bridge for L9 learning primitives.

    This does not replace the planner. It assigns a stable learning policy arm,
    observes task outcomes, records experiment metrics, maintains a lightweight
    world model, and applies an ROI gate for future promotion jobs.
    """

    DEFAULT_EXPERIMENT_ID = "planner_policy_online_v1"

    def __init__(self, experiments: ExperimentManager, world_model: Optional[WorldModel] = None, roi: Optional[LearningROIAnalyzer] = None):
        self.experiments = experiments
        self.world_model = world_model or WorldModel()
        self.roi = roi or LearningROIAnalyzer()
        self.metrics: Any = None
        self.promotion_policy = LearningPromotionPolicy()
        self._ensure_default_experiment()

    def _ensure_default_experiment(self) -> None:
        if not self.experiments.get(self.DEFAULT_EXPERIMENT_ID):
            self.experiments.create(ExperimentSpec(
                experiment_id=self.DEFAULT_EXPERIMENT_ID,
                name="Online planner learning policy canary",
                control_policy="planner_current",
                treatment_policy="planner_learning_candidate",
                treatment_percent=10.0,
                status="running",
                created_at=time.time(),
            ))

    def assign_policy(self, msg: ChannelMessage) -> dict[str, Any]:
        unit_id = f"{msg.channel}:{msg.sender_id}:{msg.thread_id or msg.message_id or msg.text[:80]}"
        assignment = self.experiments.assign(self.DEFAULT_EXPERIMENT_ID, unit_id)
        self.world_model.observe_entity(msg.channel, "channel")
        self.world_model.observe_entity(msg.sender_id, "actor", channel=msg.channel)
        self.world_model.transition_state(msg.channel, "message_received", trigger="inbound_message")
        self._metric("omnidesk_learning_experiment_assignments_total", arm=assignment.arm)
        return {
            "experiment_id": assignment.experiment_id,
            "unit_id": assignment.unit_id,
            "arm": assignment.arm,
            "bucket": assignment.bucket,
            "policy": "planner_learning_candidate" if assignment.arm == "treatment" else "planner_current",
        }

    def observe_result(self, assignment: Optional[dict[str, Any]], *, status: str, result_count: int, cost: float = 0.0, latency_ms: float = 0.0, safety_violation: bool = False) -> None:
        if not assignment:
            return
        success = status == "completed"
        self.experiments.record(ExperimentObservation(
            experiment_id=str(assignment["experiment_id"]),
            unit_id=str(assignment["unit_id"]),
            arm=str(assignment["arm"]),
            success=success,
            reward=1.0 if success else 0.0,
            cost=float(cost),
            latency_ms=float(latency_ms),
            safety_violation=bool(safety_violation),
            metadata={"status": status, "result_count": result_count, "policy": assignment.get("policy")},
        ))
        self.world_model.transition_state(str(assignment.get("policy", "planner_current")), status, trigger="task_completed") if str(assignment.get("policy", "")) in self.world_model.entities else None
        self._metric("omnidesk_learning_experiment_observations_total", arm=str(assignment["arm"]), status=status)

    def roi_gate(self, *, success_delta: float, affected_task_count: int, compute_cost: float, risk_penalty: float = 0.0) -> dict[str, Any]:
        report = self.roi.evaluate(
            success_delta=success_delta,
            affected_task_count=affected_task_count,
            compute_cost=compute_cost,
            risk_penalty=risk_penalty,
        )
        self._metric("omnidesk_learning_roi_evaluations_total", decision=report.decision)
        return report.to_dict()

    def promotion_gate(self, experiment_id: str | None = None) -> dict[str, Any]:
        experiment = experiment_id or self.DEFAULT_EXPERIMENT_ID
        decision = self.promotion_policy.evaluate(self.experiments.summary(experiment))
        self._metric("omnidesk_learning_promotion_evaluations_total", decision=decision.decision)
        if decision.decision == "candidate_for_human_review":
            self._metric("omnidesk_learning_promotion_candidates_total")
        return decision.to_dict()

    def _metric(self, name: str, **labels: Any) -> None:
        inc = getattr(getattr(self, "metrics", None), "inc", None)
        if callable(inc):
            inc(name, **labels)
