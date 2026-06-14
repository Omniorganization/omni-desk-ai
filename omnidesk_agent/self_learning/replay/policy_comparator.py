from __future__ import annotations

from typing import Any

from omnidesk_agent.self_learning.replay.replay_dataset import ReplayTrace


class PolicyComparator:
    def compare(self, trace: ReplayTrace, replay_result: dict[str, Any]) -> dict[str, Any]:
        new_score = float(replay_result.get("new_score", 0.0) or 0.0)
        delta = new_score - trace.old_score
        return {
            "trace_id": trace.trace_id,
            "old_score": trace.old_score,
            "new_score": new_score,
            "improvement_delta": round(delta, 4),
            "improved": delta > 0.05,
        }
