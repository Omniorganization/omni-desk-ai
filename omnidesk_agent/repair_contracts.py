from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RepairEvidenceBundle:
    incident_id: str
    branch: str
    tests: tuple[str, ...]
    gates: tuple[str, ...]
    rollback_plan: str
    artifacts: tuple[str, ...] = ()
    external_evidence_status: str = "blocked_until_attached"
    artifact_hashes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PullRequestDraft:
    title: str
    body: str
    base: str
    head: str
    labels: tuple[str, ...]
    ready_for_review: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
