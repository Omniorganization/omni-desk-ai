"""Agent steps that wait for approval, in the clients' signed approval plane.

The orchestrator pauses a run on ``ApprovalRequired`` and records the approval
in security's ``ApprovalStore``, which stays the ledger: single-use,
scope-bound, dual approval for critical steps. Before this module, those
approvals could be decided only through ``/approvals/*`` and ``/agent/resume``,
which no client calls, while the clients' AppSync approvals gated desktop
tasks only.

Now:
- ``AppSyncApprovalListener`` turns every pause (``on_approval_wait``) into an
  AppSync approval ``agt_<ledger id>``. Web Admin and mobile list it, with the
  stored, redacted proposal. The push says only ``tool.action`` and the risk.
- ``POST /app/approvals/agt_.../decide`` (owner, device-signed) applies the
  ledger's authority before anything moves:
  - the requester never approves;
  - break-glass never approves a critical step;
  - a critical step needs two distinct owners, recorded through
    ``dual_approval_store.approve`` before ``decide("approved")``;
  - the approver must be in the run's organization.
  Only then does AppSync mirror the decision. ``resume_by_approval`` then
  resumes the run, or ends it on a rejection.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, cast

from omnidesk_agent.appsync.blocking import run_blocking
from omnidesk_agent.appsync.store import AGENT_APPROVAL_PREFIX

logger = logging.getLogger(__name__)

_REQUESTER_FIELDS = ("created_by", "proposer", "actor")
_RUN_SUMMARY_FIELDS = ("ok", "status", "run_id", "approval_id", "already_handled")


def is_agent_approval(approval_id: str) -> bool:
    return approval_id.startswith(AGENT_APPROVAL_PREFIX)


def _requesters(approval: dict[str, Any]) -> set[str]:
    proposal = (approval.get("action_detail") or {}).get("proposal") or {}
    named = {str(proposal.get(field) or "").strip() for field in _REQUESTER_FIELDS}
    return (named | {str(approval.get("requested_by") or "").strip()}) - {""}


def refuse_break_glass_for_critical(approval: dict[str, Any], *, break_glass: bool) -> None:
    """A break-glass elevation never approves a critical action (security's rule)."""
    if break_glass and int(approval.get("required_approvals") or 1) > 1:
        raise PermissionError("a break-glass elevation cannot approve a critical action")


class AppSyncApprovalListener:
    """The orchestrator's ``approval_listener``: shows each pause to the clients."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def on_approval_wait(self, event: Any) -> None:
        data = event.to_dict() if callable(getattr(event, "to_dict", None)) else dict(event)
        approval_id = str(data.get("approval_id") or "")
        organization = str(data.get("organization") or "").strip()
        if not organization:
            # Without the run's organization nobody can be told safely; the
            # approval stays decidable through the ledger's own routes.
            logger.warning("agent approval %s has no organization; not shown to any client", approval_id)
            return
        raw = data.get("proposal")
        proposal: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        requester = str(data.get("requester") or "").strip() or next(iter(sorted(_requesters({"action_detail": {"proposal": proposal}}))), "")
        if not requester:
            logger.warning("agent approval %s has no requester; not shown to any client", approval_id)
            return
        expires_at = data.get("expires_at")
        self.store.record_agent_approval(
            source_approval_id=approval_id,
            run_id=str(data.get("run_id") or ""),
            organization_id=organization,
            requester=requester,
            risk=str(data.get("risk") or proposal.get("risk") or "high"),
            proposal=proposal,
            requires_dual_approval=bool(data.get("requires_dual_approval")),
            expires_at=float(expires_at) if isinstance(expires_at, (int, float)) else None,
            channel=str(data.get("channel") or ""),
        )


def _ledger_error(exc: PermissionError) -> Exception:
    # The ledger refuses an expired or already decided approval with
    # PermissionError; to the client that is a conflict, not a lack of rights.
    message = str(exc)
    return ValueError(message) if ("expired" in message or "already decided" in message) else exc


async def decide_agent_approval(
    *,
    store: Any,
    approval_store: Any,
    orchestrator: Any,
    approval_id: str,
    actor: str,
    decision: str,
    reason: Optional[str],
    break_glass: bool,
    idempotency_key: Optional[str],
    idempotency_payload: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Records an owner's decision in the ledger, mirrors it, then resumes or ends the run."""
    if approval_store is None:
        raise PermissionError("agent approvals need the approval store")
    approval = await run_blocking(store.get_approval, approval_id, actor=actor)
    source_id = str((approval.get("action_detail") or {}).get("source_approval_id") or "")
    if not source_id:
        raise KeyError("agent approval has no ledger record")
    if approval.get("status") == "pending":
        try:
            if decision == "approved":
                refuse_break_glass_for_critical(approval, break_glass=break_glass)
                if actor in _requesters(approval):
                    raise PermissionError("the requester cannot approve their own request")
                approved_by = list(approval.get("approved_by") or [])
                if actor in approved_by:
                    raise PermissionError("this approver has already approved; a different approver must decide")
                # Two approvals for a critical step, counted here and, when the ledger
                # opened a dual approval, recorded there first (it decides readiness).
                ready = len(approved_by) + 1 >= int(approval.get("required_approvals") or 1)
                if (approval.get("action_detail") or {}).get("ledger_dual"):
                    dual = getattr(approval_store, "dual_approval_store", None)
                    if dual is None:
                        raise PermissionError("a critical step needs the dual-approval store")
                    ready = bool((await run_blocking(dual.approve, source_id, actor)).ready) and ready
                if ready:
                    await run_blocking(approval_store.decide, source_id, "approved", {"approved_by": [*approved_by, actor], "plane": "appsync"})
            else:
                await run_blocking(approval_store.decide, source_id, "denied", {"denied_by": actor, "reason": reason, "plane": "appsync"})
        except PermissionError as exc:
            raise _ledger_error(exc) from exc
    # The ledger accepted the decision; AppSync mirrors it (and replays a retry).
    record = await run_blocking(
        store.decide_approval, approval_id=approval_id, actor=actor, decision=decision,
        reason=reason, idempotency_key=idempotency_key, idempotency_payload=idempotency_payload,
    )
    run: Optional[dict[str, Any]] = None
    resume = cast(Optional[Callable[[str], Awaitable[Any]]], getattr(orchestrator, "resume_by_approval", None))
    if record.get("status") in {"approved", "rejected"} and callable(resume):
        result = await resume(source_id)
        if isinstance(result, dict):
            run = {key: result[key] for key in _RUN_SUMMARY_FIELDS if key in result}
            termination = result.get("termination")
            if isinstance(termination, dict) and termination.get("reason"):
                run["termination_reason"] = termination["reason"]
    return {"approval": record, "run": run}


def attach_approval_plane(rt: Any) -> None:
    """Connects the orchestrator's pauses to AppSync (agent-core #280's hooks)."""
    orchestrator = getattr(rt, "orchestrator", None)
    store = getattr(rt, "app_sync", None)
    if orchestrator is None or store is None or not callable(getattr(store, "record_agent_approval", None)):
        return
    orchestrator.approval_listener = AppSyncApprovalListener(store)
    orchestrator.organization_resolver = store.organization_for_actor
