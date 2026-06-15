from __future__ import annotations

from typing import Optional


class SkillRetirementPolicy:
    def should_retire(self, *, bad_memory_rate: Optional[float] = None, replay_regression: Optional[float] = None) -> bool:
        return (bad_memory_rate is not None and bad_memory_rate >= 0.4) or (replay_regression is not None and replay_regression >= 0.3)
