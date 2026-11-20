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

    `proposal` is the approval record as the ApprovalStore holds it: known
    secrets redacted (security's approval-record rules) and the requester
    stamped where ``security.dual_approval.proposal_requesters`` reads it.
    It still holds the action's content (a message body, a file's text):
    never put it in a push notification, since APNs and FCM are third
    parties. Push ids, tool/action and risk; devices fetch the rest over the
    authenticated API.

    `organization` is the requester's organization as the server resolves it
    (``Orchestrator.organization_resolver``), or None when no resolver is
    attached: then the listener must not guess one from the requester or
    the channel.
    """

    run_id: Optional[str]
    approval_id: str
    proposal: dict[str, Any]
    requester: str
    organization: Optional[str]
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

        Errors are caught and counted, never raised into the run. See
        ApprovalWaitEvent for what may go into a push notification.
        """
        ...
