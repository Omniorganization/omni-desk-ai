from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from omnidesk_agent.evals.regression_suite import RegressionResult


@dataclass(frozen=True)
class EvalPromotionDecision:
    allowed: bool
    pass_rate: float
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_promotion(results: list[RegressionResult], *, min_pass_rate: float = 0.98) -> EvalPromotionDecision:
    if not results:
        return EvalPromotionDecision(False, 0.0, ("no_eval_results",))
    passed = sum(1 for item in results if item.passed)
    rate = passed / len(results)
    blockers = tuple(item.name for item in results if not item.passed)
    return EvalPromotionDecision(rate >= min_pass_rate and not blockers, rate, blockers)
