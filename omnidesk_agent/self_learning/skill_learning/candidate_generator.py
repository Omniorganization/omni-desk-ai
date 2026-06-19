from __future__ import annotations

import re
from typing import Any


class SkillCandidateGenerator:
    def from_experiences(self, experiences: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in experiences:
            if not item.get("reusable_skill") and item.get("memory_status") not in {"validated", "trusted"}:
                continue
            name = self._name(item)
            if name in seen:
                continue
            seen.add(name)
            candidates.append({
                "name": name,
                "source_experience_id": item.get("id"),
                "task_type": item.get("task_type", "unknown"),
                "goal": item.get("goal", ""),
                "recommended_action": item.get("recommended_next_action", "Follow the validated recovery plan."),
                "confidence": float(item.get("confidence", 0.5) or 0.5),
                "status": "candidate",
            })
            if len(candidates) >= limit:
                break
        return candidates

    def _name(self, item: dict[str, Any]) -> str:
        base = f"{item.get('task_type', 'task')}-{item.get('goal', 'workflow')}"
        slug = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "-", base).strip("-").lower()
        return slug[:80] or "learned-skill"
