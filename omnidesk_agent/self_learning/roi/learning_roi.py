from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class LearningROIReport:
    benefit_score: float
    compute_cost: float
    risk_penalty: float
    roi: float
    decision: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LearningROIAnalyzer:
    """Prevents low-value learning jobs from consuming unbounded API/runtime cost."""

    def evaluate(
        self,
        *,
        success_delta: float,
        affected_task_count: int,
        compute_cost: float,
        risk_penalty: float = 0.0,
        min_roi: float = 1.0,
    ) -> LearningROIReport:
        if affected_task_count < 0:
            raise ValueError("affected_task_count cannot be negative")
        if compute_cost < 0:
            raise ValueError("compute_cost cannot be negative")
        benefit_score = max(success_delta, 0.0) * affected_task_count
        adjusted_benefit = max(benefit_score - max(risk_penalty, 0.0), 0.0)
        roi = adjusted_benefit / max(compute_cost, 1e-9)
        if roi >= min_roi and adjusted_benefit > 0:
            return LearningROIReport(round(benefit_score, 6), compute_cost, risk_penalty, round(roi, 6), "approve", "learning benefit exceeds cost gate")
        return LearningROIReport(round(benefit_score, 6), compute_cost, risk_penalty, round(roi, 6), "reject", "learning ROI below promotion gate")
