"""The orchestrator's side of the single approval plane.

When a step pauses for an approval, the orchestrator tells an approval
listener (gateway-clients' AppSync listener writes the approval record that
approvers' devices show). After the plane has recorded a decision in the
ApprovalStore, it calls ``Orchestrator.resume_by_approval(approval_id)``.
The ApprovalStore stays the ledger: the plane decides there, and the
orchestrator only reads it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class ApprovalWaitEvent:
    """A run waiting for an approval, as an approver is shown it.

    `proposal` is the approval record as the ApprovalStore holds it: secrets
    redacted (security's approval-record rules) and the requester stamped
    where ``security.dual_approval.proposal_requesters`` reads it.
    """

    run_id: Optional[str]
    approval_id: str
    proposal: dict[str, Any]
    requester: str
    channel: str
    risk: str
    requires_dual_approval: bool
    created_at: Optional[float]
    expires_at: Optional[float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalListener(Protocol):
    def on_approval_wait(self, event: ApprovalWaitEvent) -> None:
        """Called in a worker thread once the approval is recorded and the run is saved.

        Errors are caught and counted, never raised into the run.
        """
        ...
