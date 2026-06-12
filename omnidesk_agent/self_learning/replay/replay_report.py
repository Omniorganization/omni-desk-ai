from __future__ import annotations

from typing import Any, Optional

from omnidesk_agent.self_learning.replay.plan_replayer import PlanReplayer
from omnidesk_agent.self_learning.replay.policy_comparator import PolicyComparator
from omnidesk_agent.self_learning.replay.replay_dataset import ReplayDataset


class ReplayReportBuilder:
    def __init__(self, replayer: Optional[PlanReplayer] = None, comparator: Optional[PolicyComparator] = None):
        self.replayer = replayer or PlanReplayer()
        self.comparator = comparator or PolicyComparator()

    def from_experiences(self, experiences: list[dict[str, Any]], *, limit: int = 50) -> dict[str, Any]:
        traces = ReplayDataset.from_experiences(experiences, limit=limit)
        comparisons = []
        for trace in traces:
            replay_result = self.replayer.replay(trace)
            comparisons.append(self.comparator.compare(trace, replay_result))
        improved = sum(1 for item in comparisons if item["improved"])
        avg_delta = sum(item["improvement_delta"] for item in comparisons) / len(comparisons) if comparisons else 0.0
        return {
            "trace_count": len(comparisons),
            "improved_count": improved,
            "policy_improvement_score": round(improved / len(comparisons), 4) if comparisons else None,
            "learning_regression_score": round(sum(1 for item in comparisons if item["improvement_delta"] < -0.05) / len(comparisons), 4) if comparisons else None,
            "average_improvement_delta": round(avg_delta, 4),
            "comparisons": comparisons,
        }
