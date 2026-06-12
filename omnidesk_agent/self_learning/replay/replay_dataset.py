from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class ReplayTrace:
    trace_id: str
    task_type: str
    goal: str
    old_success: bool
    failure_reason: str = ""
    old_score: float = 0.0
    raw_trace: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReplayDataset:
    @staticmethod
    def from_experiences(experiences: list[dict[str, Any]], *, limit: int = 50) -> list[ReplayTrace]:
        traces: list[ReplayTrace] = []
        for item in experiences[:limit]:
            traces.append(ReplayTrace(
                trace_id=str(item.get("id") or item.get("trace_id") or len(traces) + 1),
                task_type=str(item.get("task_type") or "unknown"),
                goal=str(item.get("goal") or ""),
                old_success=bool(item.get("success")),
                failure_reason=str(item.get("failure_reason") or ""),
                old_score=float(item.get("success_score", 1.0 if item.get("success") else 0.0) or 0.0),
                raw_trace=item.get("raw_trace") if isinstance(item.get("raw_trace"), dict) else {},
            ))
        return traces
