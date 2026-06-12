from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from omnidesk_agent.self_learning.observability.schema import LearningEvent


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator


def _average(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


@dataclass
class LearningMetricsSnapshot:
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.metrics)


class LearningMetricsCalculator:
    """Computes L10 learning observability metrics from audit events."""

    bad_memory_statuses = {"bad", "deprecated", "blocked", "needs_review"}

    def compute(self, events: Iterable[LearningEvent]) -> LearningMetricsSnapshot:
        items = list(events)
        task_events = [e for e in items if e.event_type == "task_outcome"]
        memory_events = [e for e in items if e.event_type == "memory_review"]
        reuse_events = [e for e in items if e.event_type == "experience_reused"]
        drift_events = [e for e in items if e.event_type == "drift_detected"]
        safety_events = [e for e in items if e.event_type == "safety_event"]
        rollback_events = [e for e in items if e.event_type == "rollback_event"]
        replay_events = [e for e in items if e.event_type == "replay_evaluation"]
        skill_promotion_events = [e for e in items if e.event_type == "skill_promotion"]
        coverage_events = [e for e in items if e.event_type == "test_coverage" and e.test_coverage is not None]

        success_count = sum(1 for e in task_events if e.outcome == "success")
        manual_count = sum(1 for e in task_events if e.manual_intervention)
        bad_memory_count = sum(1 for e in memory_events if (e.memory_status or "") in self.bad_memory_statuses)
        stale_count = sum(1 for e in memory_events if e.stale)
        contradiction_count = sum(1 for e in memory_events if e.contradiction)
        bypass_count = sum(1 for e in safety_events if e.permission_bypass)
        high_risk_misexecution_count = sum(1 for e in safety_events if e.high_risk_misexecution)
        rollback_success_count = sum(1 for e in rollback_events if e.rollback_success is True)

        reuse_deltas = [float(e.reuse_success_delta) for e in reuse_events if e.reuse_success_delta is not None]
        confidence_values = [float(e.confidence) for e in memory_events if e.confidence is not None]
        coverage_values = [float(e.test_coverage) for e in coverage_events if e.test_coverage is not None]
        precision_values = [float(e.memory_precision_score) for e in memory_events if e.memory_precision_score is not None]
        relevance_values = [float(e.retrieval_relevance_score) for e in reuse_events if e.retrieval_relevance_score is not None]
        generalization_values = [float(e.experience_generalization_score) for e in memory_events if e.experience_generalization_score is not None]
        regression_values = [float(e.learning_regression_score) for e in replay_events if e.learning_regression_score is not None]
        policy_values = [float(e.policy_improvement_score) for e in replay_events if e.policy_improvement_score is not None]
        skill_promotion_success_count = sum(1 for e in skill_promotion_events if e.skill_promotion_success is True)

        metrics = {
            "event_count": len(items),
            "task_count": len(task_events),
            "task_success_rate": _rate(success_count, len(task_events)),
            "manual_intervention_rate": _rate(manual_count, len(task_events)),
            "experience_reuse_rate": _rate(len(reuse_events), max(len(task_events), 1)) if task_events or reuse_events else None,
            "reuse_success_delta": _average(reuse_deltas),
            "memory_review_count": len(memory_events),
            "bad_memory_rate": _rate(bad_memory_count, len(memory_events)),
            "stale_memory_rate": _rate(stale_count, len(memory_events)),
            "contradiction_rate": _rate(contradiction_count, len(memory_events)),
            "average_memory_confidence": _average(confidence_values),
            "drift_event_count": len(drift_events),
            "permission_bypass_rate": _rate(bypass_count, len(safety_events)),
            "high_risk_misexecution_rate": _rate(high_risk_misexecution_count, len(safety_events)),
            "rollback_success_rate": _rate(rollback_success_count, len(rollback_events)),
            "test_coverage": _average(coverage_values),
            "memory_precision_score": _average(precision_values),
            "retrieval_relevance_score": _average(relevance_values),
            "experience_generalization_score": _average(generalization_values),
            "learning_regression_score": _average(regression_values),
            "policy_improvement_score": _average(policy_values),
            "skill_promotion_success_rate": _rate(skill_promotion_success_count, len(skill_promotion_events)),
        }
        metrics["learning_quality_score"] = self.learning_quality_score(metrics)
        metrics["industrial_readiness_score"] = self.industrial_readiness_score(metrics)
        return LearningMetricsSnapshot(metrics)

    def learning_quality_score(self, metrics: dict[str, Any]) -> float:
        score = 0.0
        task_success = metrics.get("task_success_rate")
        reuse_rate = metrics.get("experience_reuse_rate")
        reuse_delta = metrics.get("reuse_success_delta")
        bad_memory = metrics.get("bad_memory_rate")
        stale_memory = metrics.get("stale_memory_rate")
        contradiction = metrics.get("contradiction_rate")
        memory_precision = metrics.get("memory_precision_score")
        retrieval_relevance = metrics.get("retrieval_relevance_score")
        generalization = metrics.get("experience_generalization_score")
        regression = metrics.get("learning_regression_score")
        policy_improvement = metrics.get("policy_improvement_score")
        skill_success = metrics.get("skill_promotion_success_rate")

        if task_success is not None:
            score += min(max(task_success, 0.0), 1.0) * 20
        if reuse_rate is not None:
            score += min(max(reuse_rate, 0.0), 1.0) * 10
        if reuse_delta is not None:
            score += max(min((reuse_delta + 0.2) / 0.4, 1.0), 0.0) * 10
        score += (1.0 - min(max(bad_memory if bad_memory is not None else 0.0, 0.0), 1.0)) * 10
        score += (1.0 - min(max(stale_memory if stale_memory is not None else 0.0, 0.0), 1.0)) * 7.5
        score += (1.0 - min(max(contradiction if contradiction is not None else 0.0, 0.0), 1.0)) * 7.5
        for value, weight in (
            (memory_precision, 10),
            (retrieval_relevance, 7.5),
            (generalization, 7.5),
            (policy_improvement, 5),
            (skill_success, 5),
        ):
            if value is not None:
                score += min(max(value, 0.0), 1.0) * weight
        if regression is not None:
            score += (1.0 - min(max(regression, 0.0), 1.0)) * 10
        return round(score, 2)

    def industrial_readiness_score(self, metrics: dict[str, Any]) -> int:
        score = metrics.get("learning_quality_score") or 0.0
        if metrics.get("permission_bypass_rate") not in (None, 0):
            score -= 25
        if metrics.get("high_risk_misexecution_rate") not in (None, 0):
            score -= 35
        coverage = metrics.get("test_coverage")
        if coverage is not None:
            score += min(max(coverage, 0.0), 1.0) * 10
        if metrics.get("rollback_success_rate") == 1.0:
            score += 5
        return int(max(min(score, 100), 0))
