from __future__ import annotations

import time
from typing import Optional

from omnidesk_agent.self_learning.schemas import ApprovalRecord, LearningProposal
from omnidesk_agent.self_learning.store import SelfLearningStore


class HumanApprovalGate:
    """Approval gate for knowledge, prompt, workflow and code PR proposals."""

    def __init__(self, store: Optional[SelfLearningStore] = None):
        self.store = store

    def submit(self, proposal: LearningProposal, *, approval_type: Optional[str] = None, reason: str = "") -> ApprovalRecord:
        approval = ApprovalRecord(
            proposal_id=proposal.proposal_id,
            approval_type=approval_type or proposal.proposal_type,
            reason=reason or "human review required before controlled self-learning promotion",
            metadata={
                "stage": proposal.stage,
                "risk_level": proposal.risk_level,
                "requires_pr": proposal.proposal_type in {"code_fix", "test_improvement"},
            },
        )
        proposal.approval_id = approval.approval_id
        proposal.status = "pending_approval"
        self._save(approval)
        return approval

    def decide(self, approval_id: str, decision: str, *, reviewer: str, reason: str = "") -> ApprovalRecord:
        payload = self.store.get_approval(approval_id) if self.store is not None else None
        if payload is None:
            raise KeyError(approval_id)
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        approval = ApprovalRecord(**payload)
        approval.status = decision
        approval.reviewer = reviewer
        approval.reason = reason or approval.reason
        approval.decided_at = time.time()
        self._save(approval)
        return approval

    def require_approved(self, approval: ApprovalRecord) -> None:
        if approval.status != "approved":
            raise PermissionError(f"self-learning proposal requires approval; status={approval.status}")
        if not approval.reviewer:
            raise PermissionError("self-learning approval requires a reviewer identity")

    def _save(self, approval: ApprovalRecord) -> None:
        if self.store is not None:
            self.store.save_approval(approval)
