from __future__ import annotations
from typing import Any

class UpgradeScorer:
    def score(self, proposal: Any) -> float:
        return round(
            float(getattr(proposal, "impact", 0.5)) * 0.30
            + float(getattr(proposal, "frequency", 0.5)) * 0.25
            + float(getattr(proposal, "strategic_value", 0.5)) * 0.20
            + float(getattr(proposal, "testability", 0.5)) * 0.15
            - float(getattr(proposal, "risk", 0.5)) * 0.20
            - float(getattr(proposal, "effort", 0.5)) * 0.10,
            4,
        )
    def sort(self, proposals: list[Any]) -> list[Any]:
        for proposal in proposals:
            proposal.score = self.score(proposal)
        return sorted(proposals, key=lambda p: p.score, reverse=True)
