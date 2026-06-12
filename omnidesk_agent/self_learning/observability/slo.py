from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class SLOTarget:
    metric: str
    operator: str
    threshold: float
    severity: str = "error"
    description: str = ""

    def evaluate(self, value: Optional[float]) -> dict[str, Any]:
        if value is None:
            return {
                "metric": self.metric,
                "ok": False,
                "status": "missing_data",
                "severity": self.severity,
                "value": value,
                "operator": self.operator,
                "threshold": self.threshold,
                "description": self.description,
            }
        if self.operator == ">=":
            ok = value >= self.threshold
        elif self.operator == "<=":
            ok = value <= self.threshold
        elif self.operator == "==":
            ok = value == self.threshold
        else:
            raise ValueError(f"unsupported SLO operator: {self.operator}")
        return {
            "metric": self.metric,
            "ok": ok,
            "status": "ok" if ok else "violated",
            "severity": self.severity,
            "value": value,
            "operator": self.operator,
            "threshold": self.threshold,
            "description": self.description,
        }


class IndustrialSLOEvaluator:
    """Industrial learning SLOs.

    These defaults are intentionally strict. A missing metric is not treated as
    success because industrial systems need evidence, not assumptions.
    """

    def __init__(self, targets: Optional[list[SLOTarget]] = None):
        self.targets = targets or [
            SLOTarget("task_success_rate", ">=", 0.85, "warning", "Task success rate should be at least 85%"),
            SLOTarget("high_risk_misexecution_rate", "<=", 0.0, "critical", "High-risk erroneous execution must be zero"),
            SLOTarget("permission_bypass_rate", "<=", 0.0, "critical", "Permission bypass must be zero"),
            SLOTarget("bad_memory_rate", "<=", 0.01, "error", "Trusted memory pollution must stay below 1%"),
            SLOTarget("rollback_success_rate", ">=", 1.0, "warning", "Rollback success rate should be 100% when rollback data exists"),
            SLOTarget("test_coverage", ">=", 0.80, "warning", "Core test coverage should be at least 80%"),
            SLOTarget("industrial_readiness_score", ">=", 80.0, "error", "Industrial readiness score should be at least 80"),
        ]


    @staticmethod
    def runtime_targets() -> list[SLOTarget]:
        return [
            SLOTarget("webhook_enqueue_success_rate", ">=", 0.999, "error", "Webhook enqueue success rate should be at least 99.9%"),
            SLOTarget("job_dead_letter_rate", "<=", 0.001, "error", "Job dead-letter rate should stay below 0.1%"),
            SLOTarget("approval_resume_success_rate", ">=", 0.99, "error", "Approval resume success rate should be at least 99%"),
            SLOTarget("planner_fallback_rate", "<=", 0.05, "warning", "Planner fallback rate should stay below 5%"),
            SLOTarget("tool_error_rate", "<=", 0.02, "warning", "Tool error rate should stay below 2%"),
            SLOTarget("outbound_duplicate_rate", "==", 0.0, "critical", "Outbound send duplicate rate must be zero"),
            SLOTarget("plugin_timeout_rate", "<=", 0.01, "warning", "Plugin timeout rate should stay below 1%"),
            SLOTarget("daily_model_cost_usd", "<=", 500.0, "warning", "Daily model spend should stay within the default operating budget"),
            SLOTarget("cost_per_successful_task", "<=", 5.0, "warning", "Model cost per completed job should stay within default bounds"),
        ]

    def evaluate(self, metrics: dict[str, Any]) -> dict[str, Any]:
        checks = [target.evaluate(metrics.get(target.metric)) for target in self.targets]
        violations = [c for c in checks if not c["ok"]]
        critical = [c for c in violations if c["severity"] == "critical"]
        return {
            "ok": not critical and not [c for c in violations if c["severity"] == "error"],
            "checks": checks,
            "violations": violations,
            "critical_violations": critical,
        }
