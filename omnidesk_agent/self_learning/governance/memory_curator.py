from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from omnidesk_agent.self_learning.governance.confidence_manager import ConfidenceManager
from omnidesk_agent.self_learning.governance.contradiction_checker import ContradictionChecker
from omnidesk_agent.self_learning.governance.promotion_policy import PromotionPolicy
from omnidesk_agent.self_learning.governance.stale_memory_detector import StaleMemoryDetector
from omnidesk_agent.self_learning.governance.multi_agent_review import MultiAgentMemoryReviewer


@dataclass
class MemoryReview:
    experience_id: int
    memory_status: str
    confidence: float
    contradiction: bool
    stale: bool
    reason: str
    review_status: Optional[str] = None
    review_reason: Optional[str] = None
    review_votes: Optional[list[dict[str, Any]]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryCurator:
    """Reviews candidate memories before they become trusted reusable knowledge."""

    def __init__(
        self,
        confidence: Optional[ConfidenceManager] = None,
        contradictions: Optional[ContradictionChecker] = None,
        stale: Optional[StaleMemoryDetector] = None,
        policy: Optional[PromotionPolicy] = None,
        reviewer: Optional[MultiAgentMemoryReviewer] = None,
    ):
        self.confidence = confidence or ConfidenceManager()
        self.contradictions = contradictions or ContradictionChecker()
        self.stale = stale or StaleMemoryDetector()
        self.policy = policy or PromotionPolicy()
        self.reviewer = reviewer

    def review(self, experiences: list[dict[str, Any]]) -> list[MemoryReview]:
        contradictory_ids = self.contradictions.find_contradictions(experiences)
        reviews: list[MemoryReview] = []
        for item in experiences:
            experience_id = int(item.get("id", 0) or 0)
            confidence = self.confidence.score(item)
            contradiction = experience_id in contradictory_ids
            stale = self.stale.is_stale(item)
            status, reason = self.policy.decide(item, confidence=confidence, contradiction=contradiction, stale=stale)
            review_status = None
            review_reason = None
            review_votes = None
            if self.reviewer is not None:
                decision = self.reviewer.review({**item, "confidence": confidence, "contradiction": contradiction, "stale": stale})
                review_status = decision.verdict
                review_reason = decision.reason
                review_votes = [vote.to_dict() for vote in decision.votes]
                if decision.verdict == "reject":
                    status = "blocked" if status == "blocked" or item.get("risk_level") == "critical" else "needs_review"
                    reason = f"multi-agent review rejected memory: {decision.reason}"
                elif decision.verdict == "needs_review" and status in {"trusted", "validated"}:
                    status = "needs_review"
                    reason = "multi-agent review quorum not reached"
            reviews.append(MemoryReview(experience_id, status, round(confidence, 4), contradiction, stale, reason, review_status, review_reason, review_votes))
        return reviews

    def curate_store(self, memory, *, days: int = 30, limit: int = 200) -> list[dict[str, Any]]:
        experiences = memory.list_structured(days=days, limit=limit)
        reviews = self.review(experiences)
        for review in reviews:
            if review.experience_id:
                memory.update_memory_review(
                    review.experience_id,
                    memory_status=review.memory_status,
                    confidence=review.confidence,
                    reason=review.reason,
                    contradiction=review.contradiction,
                    stale=review.stale,
                )
        return [r.to_dict() for r in reviews]
