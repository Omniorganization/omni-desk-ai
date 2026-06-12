from __future__ import annotations

from typing import Any


class PromotionPolicy:
    statuses = {"candidate", "validated", "trusted", "deprecated", "blocked", "needs_review"}

    def decide(self, experience: dict[str, Any], *, confidence: float, contradiction: bool, stale: bool) -> tuple[str, str]:
        if experience.get("failure_reason") == "security_violation" or experience.get("risk_level") == "critical":
            return "blocked", "critical or security-sensitive experience cannot be promoted"
        if contradiction:
            return "needs_review", "contradicts another memory for the same goal"
        if stale and confidence < 0.65:
            return "deprecated", "memory is stale and confidence is low"
        if confidence >= 0.85 and experience.get("success") and experience.get("reusable_skill"):
            return "trusted", "high-confidence reusable successful experience"
        if confidence >= 0.65:
            return "validated", "confidence passed validation threshold"
        return "candidate", "needs more evidence before promotion"
