from __future__ import annotations

import math
import time
from typing import Any


class ConfidenceManager:
    """Scores how safe an experience is to reuse as long-term memory."""

    def score(self, experience: dict[str, Any]) -> float:
        score = 0.45
        score += float(experience.get("success_score", 0.0) or 0.0) * 0.25
        if experience.get("success"):
            score += 0.15
        if experience.get("reusable_skill"):
            score += 0.08
        if experience.get("human_feedback") in {"approved", "useful", "positive"}:
            score += 0.12
        if experience.get("human_feedback") in {"rejected", "bad", "negative"}:
            score -= 0.25
        if experience.get("failure_reason") in {"security_violation", "permission_denied"}:
            score -= 0.25
        score -= int(experience.get("negative_example_count", 0) or 0) * 0.08
        return max(0.0, min(1.0, self.apply_decay(score, experience)))

    def apply_decay(self, confidence: float, experience: dict[str, Any]) -> float:
        updated_at = float(experience.get("updated_at") or experience.get("created_at") or time.time())
        age_days = max((time.time() - updated_at) / 86400, 0.0)
        decay = math.exp(-age_days / 180)
        return confidence * decay
