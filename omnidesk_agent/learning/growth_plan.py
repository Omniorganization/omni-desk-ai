from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.learning.failure_analyzer import FailureAnalyzer


@dataclass
class GrowthPlan:
    long_term_goal: str = "become an autonomous ecommerce operations assistant"
    focus_areas: list[str] = field(default_factory=lambda: [
        "browser automation",
        "TikTok ads analysis",
        "Gmail workflow",
        "spreadsheet analysis",
        "Xiaohongshu content generation",
    ])
    weekly_targets: list[str] = field(default_factory=lambda: [
        "reduce browser automation failure rate",
        "increase reusable skill count",
        "improve task planning quality",
    ])

    @classmethod
    def load(cls, path: Path) -> "GrowthPlan":
        if not path.exists():
            plan = cls()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(plan), ensure_ascii=False, indent=2), encoding="utf-8")
            return plan
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


class GrowthPlanner:
    def __init__(self, failure_analyzer: Optional[FailureAnalyzer] = None):
        self.failure_analyzer = failure_analyzer or FailureAnalyzer()

    def propose(self, *, growth_plan: GrowthPlan, failure_summary: list[dict[str, Any]], metrics: dict[str, Any]) -> list[dict[str, Any]]:
        proposals: list[dict[str, Any]] = []
        if metrics.get("manual_intervention_rate", 0) > 0.3:
            proposals.append({
                "title": "Reduce manual intervention rate",
                "priority": "high",
                "upgrade_type": "skill",
                "reason": "manual_intervention_rate is above 0.30",
                "recommended_action": "Create skills for repeated approval-heavy workflows.",
            })
        if metrics.get("tool_error_rate", 0) > 0.2:
            proposals.append({
                "title": "Add tool regression tests",
                "priority": "high",
                "upgrade_type": "test",
                "reason": "tool_error_rate is above 0.20",
                "recommended_action": "Generate regression tests around failing tool/action pairs.",
            })
        for item in failure_summary[:5]:
            proposals.append({
                "title": f"Improve failure handling: {item.get('failure_reason')}",
                "priority": item.get("priority", "medium"),
                "upgrade_type": "skill" if item.get("failure_reason") in {"captcha_required", "login_required", "selector_changed"} else "test",
                "reason": f"Repeated failure count: {item.get('count')}",
                "recommended_action": item.get("recommended_upgrade"),
            })
        if not proposals:
            proposals.append({
                "title": "Maintain current learning baseline",
                "priority": "low",
                "upgrade_type": "report",
                "reason": "No dominant failure trend detected.",
                "recommended_action": "Continue collecting structured experiences.",
            })
        return proposals
