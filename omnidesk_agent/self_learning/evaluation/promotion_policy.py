from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class EvaluationReport:
    dataset_version: str
    baseline_score: float
    candidate_score: float
    safety_regressions: int = 0
    min_improvement: float = 0.01
    max_safety_regressions: int = 0
    shadow_passed: bool = False
    canary_passed: bool = False
    rollback_plan: str | None = None

    @property
    def delta(self) -> float:
        return float(self.candidate_score) - float(self.baseline_score)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["delta"] = self.delta
        return data


class LearningPromotionGate:
    """Fail-closed gate for learning outputs.

    Runtime learning is suggestion-only. A learned policy can be promoted only
    after an immutable offline evaluation, zero safety regressions, shadow mode,
    canary validation, and an explicit rollback plan are all present.
    """

    def evaluate(self, report: EvaluationReport) -> dict[str, Any]:
        reasons: list[str] = []
        if not report.dataset_version:
            reasons.append("dataset_version is required")
        if report.delta < report.min_improvement:
            reasons.append("candidate improvement is below threshold")
        if report.safety_regressions > report.max_safety_regressions:
            reasons.append("candidate has safety regressions")
        if not report.shadow_passed:
            reasons.append("shadow mode has not passed")
        if not report.canary_passed:
            reasons.append("canary has not passed")
        if not report.rollback_plan:
            reasons.append("rollback plan is required")
        return {
            "allowed": not reasons,
            "decision": "promote" if not reasons else "block",
            "reasons": reasons,
            "report": report.to_dict(),
            "write_mode": "suggestion_only",
        }
