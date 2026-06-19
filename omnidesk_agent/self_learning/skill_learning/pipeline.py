from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.self_learning.skill_learning.candidate_generator import SkillCandidateGenerator
from omnidesk_agent.self_learning.skill_learning.promotion import SkillPromotionPolicy
from omnidesk_agent.self_learning.skill_learning.registry_bridge import SkillRegistryBridge


class SkillLearningPipeline:
    def __init__(self, candidate_root: Path):
        self.generator = SkillCandidateGenerator()
        self.bridge = SkillRegistryBridge(candidate_root)
        self.promotion = SkillPromotionPolicy()

    def run(self, experiences: list[dict[str, Any]], *, replay_score: Optional[float] = None, limit: int = 10) -> list[dict[str, Any]]:
        output = []
        for candidate in self.generator.from_experiences(experiences, limit=limit):
            decision = self.promotion.decide(candidate, replay_score=replay_score)
            written = self.bridge.write_candidate({**candidate, "status": decision["status"]})
            output.append({**written, "promotion": decision})
        return output
