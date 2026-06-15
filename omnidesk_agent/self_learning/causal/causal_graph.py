from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class CausalEdge:
    cause: str
    effect: str
    weight: float = 1.0
    evidence_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CausalGraph:
    """Lightweight directed graph for learned failure chains."""

    def __init__(self):
        self._edges: dict[tuple[str, str], CausalEdge] = {}

    def add_edge(self, cause: str, effect: str, *, weight: float = 1.0) -> CausalEdge:
        cause = self._normalize(cause)
        effect = self._normalize(effect)
        if not cause or not effect:
            raise ValueError("cause and effect are required")
        key = (cause, effect)
        previous = self._edges.get(key)
        if previous:
            merged = CausalEdge(cause, effect, previous.weight + weight, previous.evidence_count + 1)
            self._edges[key] = merged
            return merged
        edge = CausalEdge(cause, effect, weight, 1)
        self._edges[key] = edge
        return edge

    def add_chain(self, chain: Iterable[str], *, weight: float = 1.0) -> list[CausalEdge]:
        nodes = [self._normalize(item) for item in chain if self._normalize(item)]
        edges = []
        for cause, effect in zip(nodes, nodes[1:]):
            edges.append(self.add_edge(cause, effect, weight=weight))
        return edges

    def causes_of(self, effect: str) -> list[CausalEdge]:
        effect = self._normalize(effect)
        return sorted([edge for edge in self._edges.values() if edge.effect == effect], key=lambda e: (-e.weight, e.cause))

    def effects_of(self, cause: str) -> list[CausalEdge]:
        cause = self._normalize(cause)
        return sorted([edge for edge in self._edges.values() if edge.cause == cause], key=lambda e: (-e.weight, e.effect))

    def edges(self) -> list[dict[str, Any]]:
        return [edge.to_dict() for edge in sorted(self._edges.values(), key=lambda e: (e.cause, e.effect))]

    @staticmethod
    def _normalize(value: Any) -> str:
        return str(value or "").strip().lower().replace(" ", "_")
