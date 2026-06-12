from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class WinnerDecision:
    winner: str
    confidence: float
    reason: str
    promote: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WinnerSelector:
    """Selects an experiment winner with simple safety and sample gates."""

    def select(
        self,
        summary: dict[str, dict[str, float]],
        *,
        min_samples_per_arm: int = 30,
        min_success_delta: float = 0.03,
        max_cost_increase: float = 0.25,
        max_safety_violation_rate: float = 0.0,
    ) -> WinnerDecision:
        control = summary.get("control", {})
        treatment = summary.get("treatment", {})
        if control.get("sample_count", 0) < min_samples_per_arm or treatment.get("sample_count", 0) < min_samples_per_arm:
            return WinnerDecision("inconclusive", 0.0, "insufficient samples", False)
        if treatment.get("safety_violation_rate", 0) > max_safety_violation_rate:
            return WinnerDecision("control", 1.0, "treatment safety violation rate exceeds gate", False)

        success_delta = treatment.get("success_rate", 0) - control.get("success_rate", 0)
        reward_delta = treatment.get("average_reward", 0) - control.get("average_reward", 0)
        control_cost = max(control.get("average_cost", 0), 1e-9)
        cost_increase = (treatment.get("average_cost", 0) - control.get("average_cost", 0)) / control_cost
        if success_delta >= min_success_delta and cost_increase <= max_cost_increase:
            confidence = min(0.99, 0.5 + success_delta + max(reward_delta, 0) * 0.1)
            return WinnerDecision("treatment", round(confidence, 4), "treatment improves success within cost and safety gates", True)
        if success_delta <= -min_success_delta:
            return WinnerDecision("control", round(min(0.99, 0.5 + abs(success_delta)), 4), "treatment regresses success", False)
        return WinnerDecision("inconclusive", round(max(success_delta, 0), 4), "delta below promotion threshold", False)
