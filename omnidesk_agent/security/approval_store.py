from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from omnidesk_agent.security.dual_approval import ERASED_ACTOR, anonymized_request, proposal_requesters
from omnidesk_agent.storage.sqlite import connect_sqlite
from omnidesk_agent.storage.migrations import Migration, apply_migrations
from typing import Any, Optional


def _deciders(result: dict[str, Any]) -> set[str]:
    names = {str(result.get(key) or "") for key in ("decided_by", "denied_by")}
    approved = result.get("approved_by")
    names |= {str(a) for a in approved} if isinstance(approved, list) else {str(approved or "")}
    return names - {""}


def _anonymized_decision(result: dict[str, Any], actor: str) -> dict[str, Any]:
    out = {k: (ERASED_ACTOR if v == actor else v) for k, v in result.items() if k != "reason"}
    if isinstance(out.get("approved_by"), list):
        out["approved_by"] = [ERASED_ACTOR if a == actor else a for a in out["approved_by"]]
    if "reason" in result and result.get("decided_by") != actor and result.get("denied_by") != actor:
        out["reason"] = result["reason"]  # another person's words stay
    return out


class ApprovalStore:
    def __init__(self, db_path: Path, ttl_seconds: int = 600, dual_approval_store: Any | None = None):
        self.db_path = db_path.expanduser()
        self.ttl_seconds = ttl_seconds
        self.dual_approval_store = dual_approval_store
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def attach_dual_approval_store(self, dual_approval_store: Any | None) -> None:
        self.dual_approval_store = dual_approval_store

    def _init(self) -> None:
        with connect_sqlite(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    proposal TEXT NOT NULL,
                    result TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    decided_at REAL,
                    consumed_at REAL,
                    consumed_by_run_id TEXT,
                    consumed_scope_hash TEXT
                )
                """
            )
            self._migrate(con)
            apply_migrations(con, [Migration(1, "approval_store_schema_baseline", lambda _con: None)])

    def _migrate(self, con: sqlite3.Connection) -> None:
        cols = {row[1] for row in con.execute("PRAGMA table_info(approvals)").fetchall()}
        for name, ddl in {
            "expires_at": "ALTER TABLE approvals ADD COLUMN expires_at REAL",
            "consumed_at": "ALTER TABLE approvals ADD COLUMN consumed_at REAL",
            "consumed_by_run_id": "ALTER TABLE approvals ADD COLUMN consumed_by_run_id TEXT",
            "consumed_scope_hash": "ALTER TABLE approvals ADD COLUMN consumed_scope_hash TEXT",
        }.items():
            if name not in cols:
                con.execute(ddl)

    def create(self, proposal: dict[str, Any], ttl_seconds: Optional[int] = None) -> str:
        self._check_dual_approval_prerequisites(proposal)
        aid = str(uuid.uuid4())
        now = time.time()
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        expires_at = now + ttl if ttl and ttl > 0 else None
        with connect_sqlite(self.db_path) as con:
            con.execute(
                "INSERT INTO approvals (id, status, proposal, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (aid, "pending", json.dumps(proposal, ensure_ascii=False), now, expires_at),
            )
        self._open_dual_approval_if_required(aid, proposal)
        return aid

    def get(self, approval_id: str) -> Optional[dict[str, Any]]:
        with connect_sqlite(self.db_path) as con:
            row = con.execute(
                "SELECT id, status, proposal, result, created_at, expires_at, decided_at, consumed_at, consumed_by_run_id, consumed_scope_hash FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT id, status, proposal, result, created_at, expires_at, decided_at, consumed_at, consumed_by_run_id, consumed_scope_hash FROM approvals"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC"
        with connect_sqlite(self.db_path) as con:
            rows = con.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def decide(self, approval_id: str, status: str, result: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if status not in {"approved", "denied"}:
            raise ValueError("status must be approved or denied")
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id, status, proposal, result, created_at, expires_at, decided_at, consumed_at, consumed_by_run_id, consumed_scope_hash FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if not row:
                raise KeyError(approval_id)
            current = self._row(row)
            if current.get("expires_at") and time.time() > current["expires_at"]:
                raise PermissionError(f"approval expired: {approval_id}")
            if current["status"] != "pending":
                raise PermissionError(f"approval already decided: {approval_id}")
            if status == "approved":
                self._require_dual_approval_ready(approval_id, current.get("proposal") or {})
            con.execute(
                "UPDATE approvals SET status = ?, result = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                (status, json.dumps(result or {}, ensure_ascii=False), time.time(), approval_id),
            )
            row = con.execute(
                "SELECT id, status, proposal, result, created_at, expires_at, decided_at, consumed_at, consumed_by_run_id, consumed_scope_hash FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        return self._row(row)

    def require_approved(self, approval_id: str, expected_proposal: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        approval = self.get(approval_id)
        if not approval:
            raise PermissionError(f"approval not found: {approval_id}")
        if approval.get("expires_at") and time.time() > approval["expires_at"]:
            raise PermissionError(f"approval expired: {approval_id}")
        if approval["status"] != "approved":
            raise PermissionError(f"approval is not approved: {approval_id}")

        if approval["status"] == "consumed":
            raise PermissionError(f"approval already consumed: {approval_id}")
        self._require_dual_approval_ready(approval_id, approval.get("proposal") or {})
        self._validate_expected_proposal(approval, expected_proposal)
        return approval

    def consume_approved(
        self,
        approval_id: str,
        expected_proposal: Optional[dict[str, Any]] = None,
        *,
        consumed_by_run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id, status, proposal, result, created_at, expires_at, decided_at, consumed_at, consumed_by_run_id, consumed_scope_hash FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if not row:
                raise PermissionError(f"approval not found: {approval_id}")
            approval = self._row(row)
            if approval.get("expires_at") and now > approval["expires_at"]:
                raise PermissionError(f"approval expired: {approval_id}")
            if approval["status"] != "approved":
                if approval["status"] == "consumed":
                    raise PermissionError(f"approval already consumed: {approval_id}")
                raise PermissionError(f"approval is not approved: {approval_id}")
            self._require_dual_approval_ready(approval_id, approval.get("proposal") or {})
            self._validate_expected_proposal(approval, expected_proposal)
            scope_hash = str((expected_proposal or approval.get("proposal") or {}).get("scope_hash") or "") or None
            cur = con.execute(
                """
                UPDATE approvals
                SET status='consumed', consumed_at=?, consumed_by_run_id=?, consumed_scope_hash=?
                WHERE id=? AND status='approved'
                """,
                (now, consumed_by_run_id, scope_hash, approval_id),
            )
            if cur.rowcount != 1:
                raise PermissionError(f"approval already consumed: {approval_id}")
            row = con.execute(
                "SELECT id, status, proposal, result, created_at, expires_at, decided_at, consumed_at, consumed_by_run_id, consumed_scope_hash FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        return self._row(row)


    # --- data-subject requests (privacy/data_subject.py) --------------------------------

    def export_subject(self, actor: str) -> list[dict[str, Any]]:
        """The approvals the actor requested, in full, and the actor's decisions on others' requests."""
        out: list[dict[str, Any]] = []
        dual: Any = getattr(self, "dual_approval_store", None)
        dual_approved = set(dual.approvals_by(actor)) if dual is not None and hasattr(dual, "approvals_by") else set()
        for row in self.list():
            result = row.get("result") or {}
            if actor in proposal_requesters(row.get("proposal") or {}):
                out.append(row)
            elif actor in _deciders(result) or row["id"] in dual_approved:
                role = "decider" if actor in _deciders(result) else "dual_approver"
                out.append({"id": row["id"], "role": role, "status": row.get("status"), "decided_at": row.get("decided_at"), "result": result})
        return out

    def erase_subject(self, actor: str) -> int:
        """Data-subject erasure: anonymize the actor in every approval, withdraw what they left pending.

        - A request by the actor: requester fields become [erased] and argument
          content is withheld; a pending one is withdrawn (denied).
        - A decision by the actor on someone else's request: the decider becomes
          [erased] and the reason they wrote is cleared. A pending dual approval
          loses the actor's lone first approval (DualApprovalStore.erase_subject).
        The security audit chain keeps the real identities: it is the governance
        record of who requested and approved what (SECURITY.md, Data-Subject Requests).
        Returns the records changed.
        """
        changed = 0
        now = time.time()
        with connect_sqlite(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            for approval_id, status, proposal_json, result_json in con.execute("SELECT id, status, proposal, result FROM approvals").fetchall():
                proposal = json.loads(proposal_json or "{}")
                result = json.loads(result_json) if result_json else {}
                new_status, new_proposal, new_result = status, proposal, result
                if actor in proposal_requesters(proposal):
                    new_proposal = anonymized_request(proposal, actor)
                    if status == "pending":
                        new_status, new_result = "denied", {**result, "withdrawn": "requester erased"}
                if actor in _deciders(new_result):
                    new_result = _anonymized_decision(new_result, actor)
                if (new_status, new_proposal, new_result) != (status, proposal, result):
                    con.execute(
                        "UPDATE approvals SET status = ?, proposal = ?, result = ?, decided_at = COALESCE(decided_at, ?) WHERE id = ?",
                        (new_status, json.dumps(new_proposal, ensure_ascii=False), json.dumps(new_result, ensure_ascii=False), now, approval_id),
                    )
                    changed += 1
        dual: Any = getattr(self, "dual_approval_store", None)
        if dual is not None and hasattr(dual, "erase_subject"):
            changed += int(dual.erase_subject(actor))
        return changed

    @staticmethod
    def _requires_dual_approval(proposal: dict[str, Any]) -> bool:
        return bool(proposal.get("requires_dual_approval"))

    def _check_dual_approval_prerequisites(self, proposal: dict[str, Any]) -> None:
        # Checked before the approval row is written so a refused proposal leaves nothing behind.
        if not self._requires_dual_approval(proposal):
            return
        if getattr(self, "dual_approval_store", None) is None:
            raise PermissionError("dual approval store is required for critical approval proposals")
        if not proposal_requesters(proposal):
            raise PermissionError("dual approval proposal must record its requester")

    def _open_dual_approval_if_required(self, approval_id: str, proposal: dict[str, Any]) -> None:
        if not self._requires_dual_approval(proposal):
            return
        store = getattr(self, "dual_approval_store", None)
        if store is None:
            raise PermissionError("dual approval store is required for critical approval proposals")
        store.open(approval_id, proposal)

    def _require_dual_approval_ready(self, approval_id: str, proposal: dict[str, Any]) -> None:
        if not self._requires_dual_approval(proposal):
            return
        store = getattr(self, "dual_approval_store", None)
        if store is None:
            raise PermissionError("dual approval store is required for critical approval proposals")
        decision = store.status(approval_id)
        if not decision.ready:
            raise PermissionError(f"dual approval is not satisfied: {decision.reason}")

    @staticmethod
    def _validate_expected_proposal(approval: dict[str, Any], expected_proposal: Optional[dict[str, Any]]) -> None:
        if not expected_proposal:
            return
        proposal = approval.get("proposal") or {}
        # Strict matching for execution-scope fields. Missing expected fields are ignored for compatibility.
        for key in ("tool", "action", "source", "actor", "run_id", "plan_id", "step_index", "scope_hash"):
            if expected_proposal.get(key) is not None and proposal.get(key) != expected_proposal.get(key):
                raise PermissionError(f"approval proposal mismatch on {key}")

    @staticmethod
    def _row(row) -> dict[str, Any]:
        return {
            "id": row[0],
            "status": row[1],
            "proposal": json.loads(row[2]),
            "result": json.loads(row[3]) if row[3] else None,
            "created_at": row[4],
            "expires_at": row[5],
            "decided_at": row[6],
            "consumed_at": row[7] if len(row) > 7 else None,
            "consumed_by_run_id": row[8] if len(row) > 8 else None,
            "consumed_scope_hash": row[9] if len(row) > 9 else None,
        }
