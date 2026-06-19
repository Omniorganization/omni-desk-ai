from __future__ import annotations

from collections import defaultdict
from typing import Any


class ContradictionChecker:
    """Detects conflicting memories for the same task goal."""

    def find_contradictions(self, experiences: list[dict[str, Any]]) -> set[int]:
        by_goal: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in experiences:
            by_goal[(str(item.get("task_type", "")), str(item.get("goal", "")).lower())].append(item)

        contradictory: set[int] = set()
        for items in by_goal.values():
            successes = [i for i in items if i.get("success")]
            failures = [i for i in items if not i.get("success")]
            actions = {str(i.get("recommended_next_action") or "").strip().lower() for i in items}
            actions.discard("")
            if successes and failures and len(actions) > 1:
                contradictory.update(int(i["id"]) for i in items if "id" in i)
        return contradictory
