from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Protocol


@dataclass(frozen=True)
class AgentVote:
    agent: str
    verdict: str
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MultiAgentReviewDecision:
    verdict: str
    confidence: float
    reason: str
    votes: list[AgentVote]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["votes"] = [vote.to_dict() for vote in self.votes]
        return payload


class MemoryReviewAgent(Protocol):
    name: str

    def review(self, candidate: dict[str, Any]) -> AgentVote:
        ...


class EvidenceReviewerAgent:
    name = "reviewer_agent"

    def review(self, candidate: dict[str, Any]) -> AgentVote:
        confidence = float(candidate.get("confidence", 0.5) or 0.5)
        evidence_fields = ["raw_trace", "recommended_next_action", "solution_attempted", "human_feedback"]
        evidence_count = sum(1 for field in evidence_fields if candidate.get(field))
        if confidence >= 0.75 and evidence_count >= 2:
            return AgentVote(self.name, "approve", min(0.95, confidence), "sufficient confidence and task evidence")
        if confidence < 0.45:
            return AgentVote(self.name, "reject", 1 - confidence, "confidence below evidence gate")
        return AgentVote(self.name, "abstain", 0.5, "evidence is partial")


class CriticAgent:
    name = "critic_agent"

    def review(self, candidate: dict[str, Any]) -> AgentVote:
        negative = int(candidate.get("negative_example_count", 0) or 0)
        validations = int(candidate.get("validation_count", 0) or 0)
        if negative > validations:
            return AgentVote(self.name, "reject", 0.9, "negative examples exceed validations")
        if candidate.get("contradiction"):
            return AgentVote(self.name, "reject", 0.85, "candidate contradicts existing memory")
        if validations >= 2:
            return AgentVote(self.name, "approve", 0.75, "candidate has repeated validation")
        return AgentVote(self.name, "abstain", 0.5, "not enough longitudinal validation")


class SafetyAgent:
    name = "safety_agent"
    high_risk = {"critical", "high"}
    unsafe_failures = {"security_violation", "permission_bypass", "credential_leak", "unsafe_shell"}

    def review(self, candidate: dict[str, Any]) -> AgentVote:
        risk = str(candidate.get("risk_level", "medium") or "medium").lower()
        failure = str(candidate.get("failure_reason", "") or "").lower()
        if risk in self.high_risk or failure in self.unsafe_failures:
            return AgentVote(self.name, "reject", 0.99, "safety gate rejected high-risk learning")
        if candidate.get("requires_human_approval"):
            return AgentVote(self.name, "abstain", 0.65, "requires explicit human approval before trusted memory")
        return AgentVote(self.name, "approve", 0.8, "no safety blocker detected")


class MultiAgentMemoryReviewer:
    """Executor-independent review quorum for learned memories."""

    def __init__(self, agents: Iterable[MemoryReviewAgent] | None = None, *, min_approvals: int = 2):
        self.agents = list(agents) if agents is not None else [EvidenceReviewerAgent(), CriticAgent(), SafetyAgent()]
        self.min_approvals = min_approvals

    def review(self, candidate: dict[str, Any]) -> MultiAgentReviewDecision:
        votes = [agent.review(candidate) for agent in self.agents]
        rejects = [vote for vote in votes if vote.verdict == "reject"]
        approvals = [vote for vote in votes if vote.verdict == "approve"]
        if rejects:
            confidence = max(vote.confidence for vote in rejects)
            return MultiAgentReviewDecision("reject", round(confidence, 4), rejects[0].reason, votes)
        if len(approvals) >= self.min_approvals:
            confidence = sum(vote.confidence for vote in approvals) / len(approvals)
            return MultiAgentReviewDecision("approve", round(confidence, 4), "review quorum approved trusted memory", votes)
        return MultiAgentReviewDecision("needs_review", 0.5, "review quorum not reached", votes)
