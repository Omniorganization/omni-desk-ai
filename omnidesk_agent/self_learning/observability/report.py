from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from omnidesk_agent.self_learning.observability.metrics import LearningMetricsCalculator
from omnidesk_agent.self_learning.observability.schema import LearningEvent
from omnidesk_agent.self_learning.observability.slo import IndustrialSLOEvaluator


class LearningReportBuilder:
    def __init__(self, calculator: Optional[LearningMetricsCalculator] = None, slo: Optional[IndustrialSLOEvaluator] = None):
        self.calculator = calculator or LearningMetricsCalculator()
        self.slo = slo or IndustrialSLOEvaluator()

    def build(self, events: Iterable[LearningEvent], *, period: str = "latest") -> dict[str, Any]:
        snapshot = self.calculator.compute(events).to_dict()
        slo_result = self.slo.evaluate(snapshot)
        return {
            "ok": slo_result["ok"],
            "period": period,
            "generated_at": time.time(),
            "metrics": snapshot,
            "slo": slo_result,
            "recommendations": self.recommendations(snapshot, slo_result),
        }

    def recommendations(self, metrics: dict[str, Any], slo_result: dict[str, Any]) -> list[str]:
        recs: list[str] = []
        for violation in slo_result.get("violations", []):
            metric = violation["metric"]
            if metric == "bad_memory_rate":
                recs.append("Freeze promotion to trusted memory and run memory curator review.")
            elif metric == "stale_memory_rate":
                recs.append("Run memory decay and re-validation job for stale experiences.")
            elif metric == "task_success_rate":
                recs.append("Replay recent failed traces and compare current planner against historical plan.")
            elif metric == "test_coverage":
                recs.append("Add regression tests before promoting new skills or self-upgrade artifacts.")
            elif metric in {"permission_bypass_rate", "high_risk_misexecution_rate"}:
                recs.append("Block all automatic promotion and require security review.")
            elif metric == "industrial_readiness_score":
                recs.append("Keep system in controlled beta until learning SLOs are satisfied.")
        if not recs:
            recs.append("Learning SLOs are currently satisfied; continue monitoring drift and bad-memory rate.")
        return recs
