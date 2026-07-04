from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def stable_id(prefix: str, payload: dict[str, Any]) -> str:
    seed = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


@dataclass
class LearningSourceRecord:
    source: str
    payload: dict[str, Any]
    record_id: str = ""
    occurred_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.record_id:
            self.record_id = stable_id("sl_evt", {"source": self.source, "payload": self.payload, "occurred_at": self.occurred_at})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LearningFinding:
    finding_type: str
    title: str
    severity: str
    evidence: dict[str, Any]
    recommended_action: str
    source_record_ids: list[str] = field(default_factory=list)
    finding_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.finding_id:
            self.finding_id = stable_id("sl_find", {
                "finding_type": self.finding_type,
                "title": self.title,
                "evidence": self.evidence,
                "source_record_ids": self.source_record_ids,
            })

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LearningDraftArtifact:
    artifact_type: str
    title: str
    body: str
    target: str
    source_finding_id: str
    requires_approval: bool = True
    status: str = "pending_approval"
    artifact_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_id:
            self.artifact_id = stable_id("sl_art", {
                "artifact_type": self.artifact_type,
                "title": self.title,
                "target": self.target,
                "source_finding_id": self.source_finding_id,
            })

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LearningProposal:
    stage: str
    proposal_type: str
    title: str
    problem: str
    proposed_change: str
    expected_benefit: str
    risk_level: str = "medium"
    affected_modules: list[str] = field(default_factory=list)
    test_plan: list[str] = field(default_factory=list)
    rollback_plan: str = ""
    requires_human_approval: bool = True
    source_finding_id: Optional[str] = None
    approval_id: Optional[str] = None
    validation_id: Optional[str] = None
    branch_name: Optional[str] = None
    pr_draft: Optional[dict[str, Any]] = None
    status: str = "pending_review"
    proposal_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.proposal_id:
            self.proposal_id = stable_id("sl_prop", {
                "stage": self.stage,
                "proposal_type": self.proposal_type,
                "title": self.title,
                "source_finding_id": self.source_finding_id,
                "created_at": self.created_at,
            })

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SandboxValidationResult:
    proposal_id: str
    ok: bool
    validation_type: str
    command_results: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    validation_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.validation_id:
            self.validation_id = stable_id("sl_val", {
                "proposal_id": self.proposal_id,
                "validation_type": self.validation_type,
                "ok": self.ok,
                "created_at": self.created_at,
            })

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApprovalRecord:
    proposal_id: str
    approval_type: str
    status: str = "pending"
    reviewer: Optional[str] = None
    reason: str = ""
    approval_id: str = ""
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.approval_id:
            self.approval_id = stable_id("sl_appr", {
                "proposal_id": self.proposal_id,
                "approval_type": self.approval_type,
                "created_at": self.created_at,
            })

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PromotionRecord:
    proposal_id: str
    environment: str
    status: str
    approval_id: str
    validation_id: str
    promotion_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.promotion_id:
            self.promotion_id = stable_id("sl_prom", {
                "proposal_id": self.proposal_id,
                "environment": self.environment,
                "status": self.status,
                "created_at": self.created_at,
            })

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RollbackRecord:
    proposal_id: str
    target: str
    plan: str
    status: str = "planned"
    rollback_id: str = ""
    created_at: float = field(default_factory=time.time)
    executed_at: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.rollback_id:
            self.rollback_id = stable_id("sl_rb", {
                "proposal_id": self.proposal_id,
                "target": self.target,
                "plan": self.plan,
                "created_at": self.created_at,
            })

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ControlledLearningReport:
    phase_1: dict[str, Any]
    phase_2: dict[str, Any]
    phase_3: dict[str, Any]
    generated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
