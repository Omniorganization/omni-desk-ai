from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class JudgeResult:
    score: float
    verdict: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StaticRiskJudge:
    """Deterministic placeholder for offline eval gates.

    Production deployments can swap this with an LLM judge, but CI keeps a
    deterministic implementation so promotion gates are reproducible.
    """

    def judge(self, *, expected_outcome: str, actual_outcome: str, risk_score: float) -> JudgeResult:
        if expected_outcome == actual_outcome and risk_score <= 0.85:
            return JudgeResult(1.0, "pass", "expected outcome matched and risk stayed within threshold")
        return JudgeResult(0.0, "fail", "outcome mismatch or risk threshold exceeded")
