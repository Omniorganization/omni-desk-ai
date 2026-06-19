from __future__ import annotations
from typing import Any, Optional
class UpgradeRiskClassifier:
    TYPE_RISK = {"prompt": "low", "skill": "low", "workflow": "medium", "test": "low", "code": "high", "permission": "critical", "deployment": "critical"}
    def classify(self, proposal: Any, permission_diff: Optional[dict] = None) -> dict:
        upgrade_type = getattr(proposal, "upgrade_type", "workflow")
        risk = self.TYPE_RISK.get(upgrade_type, "medium")
        if permission_diff and permission_diff.get("requires_human_approval"):
            risk = "critical" if permission_diff.get("risk") == "critical" else "high"
        return {"upgrade_type": upgrade_type, "risk": risk, "requires_human_approval": risk in {"high", "critical"} or upgrade_type in {"code", "permission", "deployment"}, "can_auto_canary": upgrade_type in {"prompt", "skill"} and risk == "low", "can_auto_merge": False}
