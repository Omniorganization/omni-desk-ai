from __future__ import annotations

import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_agent.storage.sqlite import connect_sqlite


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
            proposer = str(proposal.get("created_by") or proposal.get("proposer") or "")
            if proposer and hmac.compare_digest(proposer, approver):
                raise PermissionError("proposal creator cannot approve their own critical proposal")
            if first and hmac.compare_digest(first, approver):
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
