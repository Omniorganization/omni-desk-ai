from __future__ import annotations

from typing import Optional


class SkillPromotionPolicy:
    def decide(self, candidate: dict, *, replay_score: Optional[float] = None) -> dict:
        confidence = float(candidate.get("confidence", 0.0) or 0.0)
        if confidence >= 0.85 and (replay_score is None or replay_score >= 0.7):
            status = "canary"
            reason = "confidence and replay evidence are high enough for canary review"
        elif confidence >= 0.65:
            status = "candidate"
            reason = "candidate needs replay or human review before canary"
        else:
            status = "needs_review"
            reason = "confidence too low for automatic skill promotion"
        return {"status": status, "reason": reason}
