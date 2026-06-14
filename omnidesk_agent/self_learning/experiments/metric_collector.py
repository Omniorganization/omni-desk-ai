from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class ExperimentObservation:
    experiment_id: str
    unit_id: str
    arm: str
    success: bool
    reward: float = 0.0
    cost: float = 0.0
    latency_ms: float = 0.0
    safety_violation: bool = False
    metadata: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metadata"] = self.metadata or {}
        return payload


class ExperimentMetricCollector:
    def summarize(self, observations: Iterable[ExperimentObservation]) -> dict[str, dict[str, float]]:
        rows = list(observations)
        summary: dict[str, dict[str, float]] = {}
        for arm in sorted({row.arm for row in rows} | {"control", "treatment"}):
            arm_rows = [row for row in rows if row.arm == arm]
            n = len(arm_rows)
            success_count = sum(1 for row in arm_rows if row.success)
            safety_count = sum(1 for row in arm_rows if row.safety_violation)
            reward = sum(float(row.reward) for row in arm_rows)
            cost = sum(float(row.cost) for row in arm_rows)
            latency = sum(float(row.latency_ms) for row in arm_rows)
            summary[arm] = {
                "sample_count": float(n),
                "success_rate": round(success_count / n, 6) if n else 0.0,
                "average_reward": round(reward / n, 6) if n else 0.0,
                "average_cost": round(cost / n, 6) if n else 0.0,
                "average_latency_ms": round(latency / n, 6) if n else 0.0,
                "safety_violation_rate": round(safety_count / n, 6) if n else 0.0,
            }
        return summary
