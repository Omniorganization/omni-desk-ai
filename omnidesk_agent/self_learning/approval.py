from __future__ import annotations

import time
from typing import Optional

from omnidesk_agent.self_learning.schemas import ApprovalRecord, LearningProposal
from omnidesk_agent.self_learning.store import SelfLearningStore

# Identities the learning pipeline itself uses. A decision recorded under one of
# them would be the pipeline approving its own output.
SYSTEM_IDENTITIES = frozenset({
    "",
    "system",
    "omnidesk",
    "omnidesk-self-learning",
    "omnidesk-rollout-guard",
    "daily-self-learning",
    "ai",
    "agent",
    "automation",
})


def is_system_identity(reviewer: Optional[str]) -> bool:
    """True for empty or pipeline identities, also when decorated as "name (os-user:x)"."""
    name = str(reviewer or "").split(" (", 1)[0].strip().lower()
    return name in SYSTEM_IDENTITIES


class HumanApprovalGate:
    """Approval gate for knowledge, prompt, workflow and code PR proposals.

    Decisions live in the store. ``require_approved`` re-reads the stored
    decision instead of trusting the record object a caller hands in, so an
    ApprovalRecord that was never stored (or was edited in memory) cannot
    authorize anything.
    """

    def __init__(self, store: Optional[SelfLearningStore] = None):
        self.store = store

    def submit(
        self,
        proposal: LearningProposal,
        *,
        approval_type: Optional[str] = None,
        reason: str = "",
        content_sha256: Optional[str] = None,
    ) -> ApprovalRecord:
        metadata = {
            "stage": proposal.stage,
            "risk_level": proposal.risk_level,
            "requires_pr": proposal.proposal_type in {"code_fix", "test_improvement"},
        }
        if content_sha256:
            metadata["content_sha256"] = content_sha256
        approval = ApprovalRecord(
            proposal_id=proposal.proposal_id,
            approval_type=approval_type or proposal.proposal_type,
            reason=reason or "human review required before controlled self-learning promotion",
            metadata=metadata,
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
        reviewer = str(reviewer or "").strip()
        if is_system_identity(reviewer):
            raise PermissionError("self-learning approval requires a human reviewer identity")
        approval = ApprovalRecord(**payload)
        if approval.status != "pending":
            # A decided approval is final; a new decision needs a new request.
            raise PermissionError(f"approval {approval_id} is already {approval.status}")
        approval.status = decision
        approval.reviewer = reviewer
        approval.reason = reason or approval.reason
        approval.decided_at = time.time()
        self._save(approval)
        return approval

    def require_approved(self, approval: ApprovalRecord, *, content_sha256: Optional[str] = None) -> ApprovalRecord:
        """Return the stored approval if it authorizes ``approval.proposal_id``.

        Raises PermissionError unless the stored decision is ``approved`` by a
        human reviewer for the same proposal and, when given, the same content.
        """
        if self.store is None:
            raise PermissionError("self-learning approval requires a stored decision; no approval store is configured")
        payload = self.store.get_approval(approval.approval_id)
        if payload is None:
            raise PermissionError(f"self-learning approval {approval.approval_id} was never stored")
        stored = ApprovalRecord(**payload)
        if stored.proposal_id != approval.proposal_id:
            raise PermissionError("self-learning approval is not bound to this proposal")
        if stored.status != "approved":
            raise PermissionError(f"self-learning proposal requires approval; status={stored.status}")
        if is_system_identity(stored.reviewer):
            raise PermissionError("self-learning approval requires a reviewer identity")
        bound = (stored.metadata or {}).get("content_sha256")
        if content_sha256 is not None and bound != content_sha256:
            raise PermissionError("self-learning approval was given for different content")
        return stored

    def _save(self, approval: ApprovalRecord) -> None:
        if self.store is not None:
            self.store.save_approval(approval)
