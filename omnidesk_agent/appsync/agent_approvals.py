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
- A ledger already decided elsewhere (``/approvals/*``, the CLI) is mirrored
  as the ledger records it, naming its deciders and never the caller; a
  caller whose decision it was not gets a conflict (409).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, cast

from omnidesk_agent.appsync.blocking import run_blocking
from omnidesk_agent.appsync.store import AGENT_APPROVAL_PREFIX

logger = logging.getLogger(__name__)

_REQUESTER_FIELDS = ("created_by", "proposer", "actor")
_RUN_SUMMARY_FIELDS = ("ok", "status", "run_id", "approval_id", "already_handled")
# Named as the decider of a ledger decision that records nobody; never the caller.
LEDGER_ACTOR = "ledger"


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


def ledger_deciders(ledger: dict[str, Any], *, dual_status: Any = None) -> list[str]:
    """Who decided a ledger approval, as the ledger records it; ``ledger`` when it names nobody.

    The plane records ``approved_by`` or ``denied_by``; the ledger's own routes
    (``/approvals/*``) and the CLI record ``decided_by``; a dual approval's two
    approvers are in the dual-approval store. Every recorded name is kept, in
    that order.
    """
    raw = ledger.get("result")
    result: dict[str, Any] = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    if ledger.get("status") == "denied":
        names: list[Any] = [result.get("denied_by") or result.get("decided_by")]
    else:
        listed = result.get("approved_by")
        names = [getattr(dual_status, "first_approver", None), getattr(dual_status, "second_approver", None)]
        names += list(cast(list[Any], listed)) if isinstance(listed, list) else []
        names.append(result.get("decided_by"))
    deciders = list(dict.fromkeys(str(name).strip() for name in names if str(name or "").strip()))
    return deciders or [LEDGER_ACTOR]


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
    conflict: Optional[str] = None
    if approval.get("status") == "pending":
        record, conflict = await _decide_pending(
            store=store, approval_store=approval_store, approval=approval, source_id=source_id, actor=actor,
            decision=decision, reason=reason, break_glass=break_glass,
            idempotency_key=idempotency_key, idempotency_payload=idempotency_payload,
        )
    elif actor == approval.get("decided_by") or actor in (approval.get("approved_by") or []):
        # A retry of a decision this actor already made and AppSync already holds
        # (with or without the idempotency key): nothing to record; resume again,
        # which agent-core makes idempotent, in case the first resume was lost.
        record = approval
    else:
        # Another actor on a decided approval: the mirror refuses it (409).
        record = await run_blocking(
            store.decide_approval, approval_id=approval_id, actor=actor, decision=decision,
            reason=reason, idempotency_key=idempotency_key, idempotency_payload=idempotency_payload,
            break_glass=break_glass,
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
    if conflict:
        # Mirrored and resumed, but this caller's decision was not the one recorded.
        raise ValueError(conflict)
    return {"approval": record, "run": run}


async def _decide_pending(
    *,
    store: Any,
    approval_store: Any,
    approval: dict[str, Any],
    source_id: str,
    actor: str,
    decision: str,
    reason: Optional[str],
    break_glass: bool,
    idempotency_key: Optional[str],
    idempotency_payload: Optional[dict[str, Any]],
) -> tuple[dict[str, Any], Optional[str]]:
    """Writes the ledger, then mirrors it. Returns the record, and a conflict for the caller.

    The ledger is read first. Already decided (outside this plane through
    ``/approvals/*`` or the CLI, or by an attempt whose mirror failed), it is
    mirrored as it stands, naming the deciders it records and never the caller;
    the conflict then tells a caller whose decision was not the one recorded.
    """
    approval_id = str(approval["approval_id"])
    mirrored = list(approval.get("approved_by") or [])
    ledger: dict[str, Any] = dict(await run_blocking(approval_store.get, source_id) or {})
    if str(ledger.get("status") or "") in {"approved", "denied"}:
        return await _mirror_decided_ledger(store=store, approval_store=approval_store, approval=approval, source_id=source_id, ledger=ledger, actor=actor, decision=decision)
    try:
        if decision == "approved":
            refuse_break_glass_for_critical(approval, break_glass=break_glass)
            if actor in _requesters(approval):
                raise PermissionError("the requester cannot approve their own request")
            if actor in mirrored:
                raise PermissionError("this approver has already approved; a different approver must decide")
            # Two approvals for a critical step, counted here and, when the ledger
            # opened a dual approval, recorded there first (it decides readiness).
            ready = len(mirrored) + 1 >= int(approval.get("required_approvals") or 1)
            if (approval.get("action_detail") or {}).get("ledger_dual"):
                dual = getattr(approval_store, "dual_approval_store", None)
                if dual is None:
                    raise PermissionError("a critical step needs the dual-approval store")
                status = await run_blocking(dual.status, source_id)
                if actor in {status.first_approver, status.second_approver}:
                    dual_ready = bool(status.ready)  # recorded by an attempt whose mirror failed
                else:
                    dual_ready = bool((await run_blocking(dual.approve, source_id, actor)).ready)
                ready = dual_ready and ready
            if not ready:
                partial = await run_blocking(
                    store.decide_approval, approval_id=approval_id, actor=actor, decision="approved",
                    reason=reason, idempotency_key=idempotency_key, idempotency_payload=idempotency_payload,
                    break_glass=break_glass,
                )
                return partial, None
            await run_blocking(approval_store.decide, source_id, "approved", {"approved_by": [*mirrored, actor], "plane": "appsync"})
        else:
            await run_blocking(approval_store.decide, source_id, "denied", {"denied_by": actor, "reason": reason, "plane": "appsync"})
    except PermissionError as exc:
        raise _ledger_error(exc) from exc
    record = await run_blocking(
        store.decide_approval, approval_id=approval_id, actor=actor, decision="approved" if decision == "approved" else "rejected",
        reason=reason, idempotency_key=idempotency_key, idempotency_payload=idempotency_payload,
    )
    return record, None


async def _mirror_decided_ledger(
    *, store: Any, approval_store: Any, approval: dict[str, Any], source_id: str, ledger: dict[str, Any], actor: str, decision: str,
) -> tuple[dict[str, Any], Optional[str]]:
    """Mirrors a ledger decided before this call; a conflict unless it is the caller's own decision."""
    outcome = "approved" if ledger.get("status") == "approved" else "rejected"
    dual_status: Any = None
    dual = getattr(approval_store, "dual_approval_store", None)
    if outcome == "approved" and dual is not None and (approval.get("action_detail") or {}).get("ledger_dual"):
        try:
            dual_status = await run_blocking(dual.status, source_id)
        except KeyError:
            dual_status = None
    deciders = ledger_deciders(ledger, dual_status=dual_status)
    raw = ledger.get("result")
    reason = cast(dict[str, Any], raw).get("reason") if isinstance(raw, dict) else None
    record = await run_blocking(
        store.mirror_agent_decision, approval_id=str(approval["approval_id"]), outcome=outcome, deciders=deciders,
        reason=str(reason) if reason is not None else None,
    )
    # A retry completing the caller's own decision (its mirror was lost) succeeds.
    if actor in deciders and decision == outcome:
        return record, None
    return record, f"already decided: {outcome}"


def attach_approval_plane(rt: Any) -> None:
    """Connects the orchestrator's pauses to AppSync (agent-core #280's hooks)."""
    orchestrator = getattr(rt, "orchestrator", None)
    store = getattr(rt, "app_sync", None)
    if orchestrator is None or store is None or not callable(getattr(store, "record_agent_approval", None)):
        return
    orchestrator.approval_listener = AppSyncApprovalListener(store)
    orchestrator.organization_resolver = store.organization_for_actor
