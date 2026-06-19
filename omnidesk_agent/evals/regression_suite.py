from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from omnidesk_agent.evals.deterministic_assertions import assert_controls
from omnidesk_agent.evals.llm_judge import StaticRiskJudge
from omnidesk_agent.evals.scenario_loader import EvalScenario


@dataclass(frozen=True)
class RegressionResult:
    name: str
    passed: bool
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_regression_suite(
    scenarios: list[EvalScenario],
    runner: Callable[[EvalScenario], dict[str, Any]],
) -> list[RegressionResult]:
    judge = StaticRiskJudge()
    results: list[RegressionResult] = []
    for scenario in scenarios:
        actual = runner(scenario)
        controls = tuple(actual.get("controls", ()))
        actual_outcome = str(actual.get("outcome", ""))
        risk_score = float(actual.get("risk_score", 1.0))
        control_assertion = assert_controls(controls, scenario.expected_controls)
        judged = judge.judge(expected_outcome=scenario.expected_outcome, actual_outcome=actual_outcome, risk_score=risk_score)
        passed = control_assertion.passed and judged.verdict == "pass"
        results.append(
            RegressionResult(
                name=scenario.name,
                passed=passed,
                details={
                    "controls": control_assertion.to_dict(),
                    "judge": judged.to_dict(),
                    "actual": actual,
                },
            )
        )
    return results
