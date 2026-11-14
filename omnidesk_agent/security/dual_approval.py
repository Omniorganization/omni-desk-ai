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

    def status(self, approval_id: str) -> DualApprovalDecision:
        with connect_sqlite(self.db_path) as con:
            row = con.execute("SELECT first_approver, second_approver FROM dual_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not row:
            raise KeyError(f"dual approval not found: {approval_id}")
        first, second = row[0], row[1]
        return DualApprovalDecision(bool(first and second), approval_id, first, second, "ready" if first and second else "waiting_for_second_approver")
