from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentTraceRecord:
    input: str
    planner_decision: dict[str, Any]
    selected_tool: str
    approval_path: str
    execution_result: str
    risk_score: float
    retrieved_memory: tuple[str, ...] = ()
    selected_skill: str = ""
    failure_reason: str = ""
    human_correction: str = ""
    final_outcome: str = ""
    cost: float = 0.0
    latency_ms: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def append_trace(path: Path, record: AgentTraceRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
