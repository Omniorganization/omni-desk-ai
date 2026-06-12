from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
import hashlib
import json
import time

@dataclass
class UpgradeProposal:
    title: str
    source: str
    problem: str
    proposed_change: str
    expected_benefit: str
    risk_level: str = "medium"
    affected_modules: list[str] = field(default_factory=list)
    test_plan: list[str] = field(default_factory=list)
    rollback_plan: str = ""
    upgrade_type: str = "workflow"
    impact: float = 0.5
    frequency: float = 0.5
    effort: float = 0.5
    risk: float = 0.5
    testability: float = 0.5
    strategic_value: float = 0.5
    score: float = 0.0
    status: str = "pending"
    artifact_hash: Optional[str] = None
    test_report_path: Optional[str] = None
    pr_url: Optional[str] = None
    merge_sha: Optional[str] = None
    proposal_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.proposal_id:
            seed = json.dumps({"title": self.title, "source": self.source, "problem": self.problem, "created_at": self.created_at}, sort_keys=True, ensure_ascii=False)
            self.proposal_id = "upg_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UpgradeProposal":
        return cls(**data)
