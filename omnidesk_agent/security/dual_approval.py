from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_agent.storage.sqlite import connect_sqlite

# Proposal fields that name who asked for the action. `created_by` and
# `proposer` are written server-side from the authenticated caller (see
# `stamp_requester`); `actor` is the identity the action runs as, which is the
# requester for orchestrator proposals. None of them may count as an approver.
REQUESTER_FIELDS = ("created_by", "proposer", "actor")


def proposal_requesters(proposal: dict[str, Any]) -> set[str]:
    return {str(proposal.get(field) or "").strip() for field in REQUESTER_FIELDS} - {""}


def stamp_requester(proposal: dict[str, Any], requester: str) -> dict[str, Any]:
    """Record the server-authenticated requester, replacing any caller-supplied value."""
    requester = str(requester or "").strip()
    if not requester:
        raise PermissionError("an authenticated requester is required to create an approval")
    proposal["created_by"] = requester
    proposal["proposer"] = requester
    return proposal


@dataclass(frozen=True)
class DualApprovalDecision:
    ready: bool
    approval_id: str
    first_approver: str | None
    second_approver: str | None
    reason: str


ERASED_ACTOR = "[erased]"
_REQUESTER_KEYS = ("created_by", "proposer", "actor", "requested_by")
# What an erased subject's request keeps: structure, never free text. A
# planner's "reason" or "summary" can describe the person's task.
STRUCTURAL_REQUEST_KEYS = frozenset({
    *_REQUESTER_KEYS,
    "tool", "action", "risk", "source", "channel", "run_id", "plan_id", "step_id",
    "approval_id", "scope_hash", "requires_dual_approval", "provenance", "args",
})


def anonymized_request(proposal: dict[str, Any], actor: str) -> dict[str, Any]:
    """A request made by an erased subject: requester fields become [erased]; only structure stays.

    Argument values are withheld (their digests and lengths stay), and every
    field outside STRUCTURAL_REQUEST_KEYS is dropped; ``fields_withheld`` names them.
    """
    out = {k: v for k, v in proposal.items() if k in STRUCTURAL_REQUEST_KEYS}
    dropped = sorted(k for k in proposal if k not in STRUCTURAL_REQUEST_KEYS and k not in {"args_withheld", "fields_withheld"})
    for key in _REQUESTER_KEYS:
        if out.get(key) == actor:
            out[key] = ERASED_ACTOR
    args = out.get("args")
    if isinstance(args, dict):
        kept = {k: v for k, v in args.items() if str(k).endswith(("_sha256", "_length"))}
        out["args"] = kept
        out["args_withheld"] = len(kept) != len(args)
    if dropped:
        out["fields_withheld"] = dropped
    return out


class DualApprovalStore:
    """Two-person approval gate for critical production actions.

    This store is intentionally independent from the normal ApprovalStore so a
    critical proposal can be tracked as requiring two distinct approvers before
    the ordinary approval is consumed by a run.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS dual_approvals (
                  approval_id TEXT PRIMARY KEY,
                  proposal_json TEXT NOT NULL,
                  first_approver TEXT,
                  second_approver TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                )
                """
            )

    def open(self, approval_id: str, proposal: dict[str, Any]) -> None:
        if not proposal_requesters(proposal):
            raise PermissionError("dual approval proposal must record its requester")
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                INSERT OR IGNORE INTO dual_approvals(approval_id, proposal_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (approval_id, json.dumps(proposal, ensure_ascii=False, sort_keys=True), now, now),
            )

    def approve(self, approval_id: str, approver: str) -> DualApprovalDecision:
        approver = str(approver).strip()
        if not approver:
            raise ValueError("approver is required")
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT first_approver, second_approver, proposal_json FROM dual_approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if not row:
                raise KeyError(f"dual approval not found: {approval_id}")
            first, second = row[0], row[1]
            proposal = json.loads(row[2] or "{}")
            if approver in proposal_requesters(proposal):
                raise PermissionError("proposal creator cannot approve their own critical proposal")
            if first and first == approver:
                raise PermissionError("second approver must be distinct from first approver")
            if not first:
                first = approver
            elif not second:
                second = approver
            con.execute(
                "UPDATE dual_approvals SET first_approver=?, second_approver=?, updated_at=? WHERE approval_id=?",
                (first, second, now, approval_id),
            )
        return self.status(approval_id)

    def approvals_by(self, actor: str) -> list[str]:
        """Ids of the dual approvals in which ``actor`` is the first or second approver."""
        with connect_sqlite(self.db_path) as con:
            rows = con.execute("SELECT approval_id FROM dual_approvals WHERE first_approver = ? OR second_approver = ?", (actor, actor)).fetchall()
        return [str(row[0]) for row in rows]

    def erase_subject(self, actor: str) -> int:
        """Data-subject erasure: withdraw the actor's lone first approval, anonymize them elsewhere.

        A pending dual approval whose only approver is the actor loses that
        approval, so it needs two fresh approvers and nobody can count twice. In
        a ready one the actor becomes ``[erased]``, and so do requester fields
        and argument content of a request they made. Returns the rows changed.
        """
        changed = 0
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("SELECT approval_id, first_approver, second_approver, proposal_json FROM dual_approvals").fetchall()
            for approval_id, first, second, proposal_json in rows:
                proposal = json.loads(proposal_json or "{}")
                if first == actor and not second:
                    new_first, new_second = None, None
                else:
                    new_first = ERASED_ACTOR if first == actor else first
                    new_second = ERASED_ACTOR if second == actor else second
                new_proposal = anonymized_request(proposal, actor) if actor in proposal_requesters(proposal) else proposal
                if (new_first, new_second, new_proposal) != (first, second, proposal):
                    con.execute(
                        "UPDATE dual_approvals SET first_approver=?, second_approver=?, proposal_json=?, updated_at=? WHERE approval_id=?",
                        (new_first, new_second, json.dumps(new_proposal, ensure_ascii=False), time.time(), approval_id),
                    )
                    changed += 1
        return changed

    def status(self, approval_id: str) -> DualApprovalDecision:
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT first_approver, second_approver FROM dual_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not row:
            raise KeyError(f"dual approval not found: {approval_id}")
        first, second = row[0], row[1]
        return DualApprovalDecision(bool(first and second), approval_id, first, second, "ready" if first and second else "waiting_for_second_approver")
