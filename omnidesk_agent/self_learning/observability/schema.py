from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class LearningEvent:
    """Auditable learning event.

    Event types used by the built-in calculators:
      - task_outcome
      - experience_reused
      - memory_review
      - drift_detected
      - safety_event
      - rollback_event
      - test_coverage
    """

    event_type: str
    task_id: Optional[str] = None
    trace_id: Optional[str] = None
    experience_id: Optional[str] = None
    skill_id: Optional[str] = None
    outcome: Optional[str] = None
    reused: bool = False
    reuse_success_delta: Optional[float] = None
    memory_status: Optional[str] = None
    confidence: Optional[float] = None
    contradiction: bool = False
    stale: bool = False
    drift_type: Optional[str] = None
    manual_intervention: bool = False
    permission_bypass: bool = False
    high_risk_misexecution: bool = False
    rollback_success: Optional[bool] = None
    test_coverage: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LearningEvent":
        allowed = set(cls.__dataclass_fields__.keys())
        payload = {k: v for k, v in data.items() if k in allowed}
        return cls(**payload)
