from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class LearningPromotionDecision:
    decision: str
    reason: str
    confidence: float
    treatment_success_rate: float
    control_success_rate: float
    treatment_sample_count: int
    control_sample_count: int
    safety_violation_rate: float
    requires_human_approval: bool = True
    rollback_plan_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LearningPromotionPolicy:
    """Fail-closed promotion policy for online self-learning candidates.

    Treatment candidates never self-promote directly. They can only become a
    human-reviewable candidate when sample size, confidence, safety, cost and
    rollback gates pass.
    """

    def __init__(
        self,
        *,
        min_sample_size_per_arm: int = 100,
        min_success_delta: float = 0.03,
        min_confidence: float = 0.95,
        max_safety_violation_rate: float = 0.0,
        max_cost_increase: float = 0.20,
    ):
        self.min_sample_size_per_arm = int(min_sample_size_per_arm)
        self.min_success_delta = float(min_success_delta)
        self.min_confidence = float(min_confidence)
        self.max_safety_violation_rate = float(max_safety_violation_rate)
        self.max_cost_increase = float(max_cost_increase)

    def evaluate(self, summary: dict[str, dict[str, float]]) -> LearningPromotionDecision:
        control = summary.get("control", {})
        treatment = summary.get("treatment", {})
        c_n = int(control.get("sample_count", 0))
        t_n = int(treatment.get("sample_count", 0))
        c_rate = float(control.get("success_rate", 0.0))
        t_rate = float(treatment.get("success_rate", 0.0))
        t_safety = float(treatment.get("safety_violation_rate", 0.0))
        c_cost = float(control.get("average_cost", 0.0))
        t_cost = float(treatment.get("average_cost", 0.0))

        if c_n < self.min_sample_size_per_arm or t_n < self.min_sample_size_per_arm:
            return self._reject("minimum sample size not reached", c_rate, t_rate, c_n, t_n, t_safety, 0.0)
        if t_safety > self.max_safety_violation_rate:
            return self._reject("safety violation hard block", c_rate, t_rate, c_n, t_n, t_safety, 1.0)
        success_delta = t_rate - c_rate
        if success_delta < self.min_success_delta:
            return self._reject("success delta below promotion threshold", c_rate, t_rate, c_n, t_n, t_safety, max(0.0, success_delta))
        cost_increase = (t_cost - c_cost) / max(c_cost, 1e-9)
        if cost_increase > self.max_cost_increase:
            return self._reject("cost increase exceeds promotion gate", c_rate, t_rate, c_n, t_n, t_safety, 0.0)
        confidence = self._two_proportion_confidence(c_rate, t_rate, c_n, t_n)
        if confidence < self.min_confidence:
            return self._reject("confidence below promotion gate", c_rate, t_rate, c_n, t_n, t_safety, confidence)
        return LearningPromotionDecision(
            decision="candidate_for_human_review",
            reason="treatment passed sample, confidence, cost, safety and rollback gates",
            confidence=round(confidence, 6),
            treatment_success_rate=round(t_rate, 6),
            control_success_rate=round(c_rate, 6),
            treatment_sample_count=t_n,
            control_sample_count=c_n,
            safety_violation_rate=round(t_safety, 6),
            requires_human_approval=True,
            rollback_plan_required=True,
        )

    def _reject(self, reason: str, c_rate: float, t_rate: float, c_n: int, t_n: int, safety: float, confidence: float) -> LearningPromotionDecision:
        return LearningPromotionDecision(
            decision="reject",
            reason=reason,
            confidence=round(float(confidence), 6),
            treatment_success_rate=round(t_rate, 6),
            control_success_rate=round(c_rate, 6),
            treatment_sample_count=t_n,
            control_sample_count=c_n,
            safety_violation_rate=round(safety, 6),
            requires_human_approval=True,
            rollback_plan_required=True,
        )

    @staticmethod
    def _two_proportion_confidence(control_rate: float, treatment_rate: float, control_n: int, treatment_n: int) -> float:
        pooled = ((control_rate * control_n) + (treatment_rate * treatment_n)) / max(control_n + treatment_n, 1)
        se = math.sqrt(max(pooled * (1.0 - pooled), 1e-9) * ((1.0 / max(control_n, 1)) + (1.0 / max(treatment_n, 1))))
        if se <= 0:
            return 0.0
        z = (treatment_rate - control_rate) / se
        # Normal CDF without scipy.
        return max(0.0, min(0.999999, 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))))
