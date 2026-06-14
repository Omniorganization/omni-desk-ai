from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from omnidesk_agent.self_learning.causal.causal_graph import CausalGraph


@dataclass(frozen=True)
class RootCauseReport:
    root_cause: str
    symptom: str
    confidence: float
    chain: list[str]
    evidence: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RootCauseAnalyzer:
    """Separates proximal symptoms from upstream causes."""

    def __init__(self, graph: CausalGraph | None = None):
        self.graph = graph or CausalGraph()

    def learn_from_experience(self, experience: dict[str, Any]) -> None:
        chain = experience.get("causal_chain") or experience.get("failure_chain")
        if chain:
            self.graph.add_chain(chain, weight=float(experience.get("success_score", 1.0) or 1.0))
            return
        raw_trace = experience.get("raw_trace") or {}
        if isinstance(raw_trace, dict):
            events = raw_trace.get("events") or []
            labels = [event.get("event") or event.get("reason") for event in events if isinstance(event, dict)]
            if len(labels) >= 2:
                self.graph.add_chain(labels)
                return
        root = experience.get("root_cause")
        failure = experience.get("failure_reason")
        symptom = experience.get("symptom") or experience.get("goal")
        if root and failure:
            self.graph.add_edge(root, failure)
        if failure and symptom:
            self.graph.add_edge(failure, symptom)

    def analyze(self, symptom: str) -> RootCauseReport:
        symptom_key = self.graph._normalize(symptom)
        chain = [symptom_key]
        evidence: list[dict[str, Any]] = []
        current = symptom_key
        visited = {current}
        confidence = 0.0
        while True:
            causes = [edge for edge in self.graph.causes_of(current) if edge.cause not in visited]
            if not causes:
                break
            best = causes[0]
            evidence.append(best.to_dict())
            chain.insert(0, best.cause)
            visited.add(best.cause)
            current = best.cause
            confidence += min(best.weight, 5.0)
        root = chain[0]
        denom = max(len(evidence) * 5.0, 1.0)
        return RootCauseReport(root, symptom_key, round(min(confidence / denom, 1.0), 4), chain, evidence)

    def analyze_experiences(self, experiences: list[dict[str, Any]], *, symptom: str) -> RootCauseReport:
        for experience in experiences:
            self.learn_from_experience(experience)
        return self.analyze(symptom)
