from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any
import time
@dataclass
class ShadowComparison:
    task: str
    stable_steps: int
    shadow_steps: int
    stable_risk: str
    shadow_risk: str
    improvement: str
    recommendation: str
    created_at: float

    def to_dict(self):
        return asdict(self)

class ShadowModeEvaluator:
    RISK_ORDER = {"low":0,"medium":1,"high":2,"critical":3}
    def compare_plans(self, task: str, stable_plan: dict[str, Any], shadow_plan: dict[str, Any]) -> ShadowComparison:
        stable_steps, shadow_steps = len(stable_plan.get("steps",[]) or []), len(shadow_plan.get("steps",[]) or [])
        stable_risk, shadow_risk = self._max_risk(stable_plan), self._max_risk(shadow_plan)
        improvements=[]
        if shadow_steps < stable_steps:
            improvements.append("fewer steps")
        if self.RISK_ORDER.get(shadow_risk,1) < self.RISK_ORDER.get(stable_risk,1):
            improvements.append("lower risk")
        if not improvements:
            improvements.append("no clear improvement")
        rec = "promote_to_canary" if improvements != ["no clear improvement"] and shadow_risk in {"low","medium"} else "keep_shadow"
        return ShadowComparison(task, stable_steps, shadow_steps, stable_risk, shadow_risk, ", ".join(improvements), rec, time.time())
    def _max_risk(self, plan):
        max_risk="low"
        for step in plan.get("steps",[]) or []:
            r=step.get("risk","medium")
            if self.RISK_ORDER.get(r,1)>self.RISK_ORDER.get(max_risk,0):
                max_risk=r
        return max_risk
