from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from omnidesk_agent.self_learning.skill_versions.lineage import SkillLineageStore


@dataclass(frozen=True)
class SkillVersionComparison:
    skill_name: str
    baseline_version: str
    candidate_version: str
    metric: str
    baseline_value: float
    candidate_value: float
    delta: float
    improved: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SkillPerformanceHistory:
    def __init__(self, store: SkillLineageStore):
        self.store = store

    def compare(self, skill_name: str, baseline_version: str, candidate_version: str, *, metric: str = "success_rate") -> SkillVersionComparison:
        baseline = self.store.latest_metric(skill_name, baseline_version, metric)
        candidate = self.store.latest_metric(skill_name, candidate_version, metric)
        if baseline is None or candidate is None:
            raise ValueError("both versions need benchmark data")
        delta = candidate - baseline
        return SkillVersionComparison(skill_name, baseline_version, candidate_version, metric, baseline, candidate, round(delta, 6), delta > 0)
