from __future__ import annotations

import json
import sqlite3
import time
import weakref
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.storage.sqlite import connect_sqlite


class SelfLearningStore:
    """SQLite audit store for controlled self-learning.

    The tables intentionally mirror the industrial control-plane stages:
    events, findings, proposals, validations, approvals, promotions and
    rollbacks. Payloads are JSON so schema evolution does not require a new
    migration for every proposal shape.
    """

    TABLES = {
        "self_learning_events",
        "self_learning_findings",
        "self_learning_proposals",
        "self_learning_validations",
        "self_learning_approvals",
        "self_learning_promotions",
        "self_learning_rollbacks",
    }

    def __init__(self, db_path: Path):
        self.db_path = db_path.expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect_sqlite(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._closed = False
        self._finalizer = weakref.finalize(self, self.conn.close)
        self._init()

    def close(self) -> None:
        if not self._closed:
            if self._finalizer.alive:
                self._finalizer()
            else:
                self.conn.close()
            self._closed = True

    def __enter__(self) -> "SelfLearningStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _init(self) -> None:
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS self_learning_events (
          id TEXT PRIMARY KEY,
          created_at REAL NOT NULL,
          source TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          metadata_json TEXT NOT NULL
        )
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS self_learning_findings (
          id TEXT PRIMARY KEY,
          created_at REAL NOT NULL,
          finding_type TEXT NOT NULL,
          severity TEXT NOT NULL,
          payload_json TEXT NOT NULL
        )
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS self_learning_proposals (
          id TEXT PRIMARY KEY,
          created_at REAL NOT NULL,
          stage TEXT NOT NULL,
          proposal_type TEXT NOT NULL,
          status TEXT NOT NULL,
          requires_human_approval INTEGER NOT NULL,
          payload_json TEXT NOT NULL
        )
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS self_learning_validations (
          id TEXT PRIMARY KEY,
          created_at REAL NOT NULL,
          proposal_id TEXT NOT NULL,
          ok INTEGER NOT NULL,
          validation_type TEXT NOT NULL,
          payload_json TEXT NOT NULL
        )
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS self_learning_approvals (
          id TEXT PRIMARY KEY,
          created_at REAL NOT NULL,
          proposal_id TEXT NOT NULL,
          approval_type TEXT NOT NULL,
          status TEXT NOT NULL,
          reviewer TEXT,
          decided_at REAL,
          payload_json TEXT NOT NULL
        )
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS self_learning_promotions (
          id TEXT PRIMARY KEY,
          created_at REAL NOT NULL,
          proposal_id TEXT NOT NULL,
          environment TEXT NOT NULL,
          status TEXT NOT NULL,
          payload_json TEXT NOT NULL
        )
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS self_learning_rollbacks (
          id TEXT PRIMARY KEY,
          created_at REAL NOT NULL,
          proposal_id TEXT NOT NULL,
          target TEXT NOT NULL,
          status TEXT NOT NULL,
          payload_json TEXT NOT NULL
        )
        """)
        self.conn.commit()

    def record_event(self, record: Any) -> None:
        payload = self._payload(record)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO self_learning_events
              (id, created_at, source, payload_json, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload["record_id"],
                float(payload.get("occurred_at") or payload.get("created_at") or time.time()),
                str(payload.get("source", "unknown")),
                self._json(payload),
                self._json(payload.get("metadata") or {}),
            ),
        )
        self.conn.commit()

    def save_finding(self, finding: Any) -> None:
        payload = self._payload(finding)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO self_learning_findings
              (id, created_at, finding_type, severity, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (payload["finding_id"], float(payload["created_at"]), payload["finding_type"], payload["severity"], self._json(payload)),
        )
        self.conn.commit()

    def save_proposal(self, proposal: Any) -> None:
        payload = self._payload(proposal)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO self_learning_proposals
              (id, created_at, stage, proposal_type, status, requires_human_approval, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["proposal_id"],
                float(payload["created_at"]),
                payload["stage"],
                payload["proposal_type"],
                payload["status"],
                1 if payload.get("requires_human_approval") else 0,
                self._json(payload),
            ),
        )
        self.conn.commit()

    def save_validation(self, validation: Any) -> None:
        payload = self._payload(validation)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO self_learning_validations
              (id, created_at, proposal_id, ok, validation_type, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["validation_id"],
                float(payload["created_at"]),
                payload["proposal_id"],
                1 if payload.get("ok") else 0,
                payload["validation_type"],
                self._json(payload),
            ),
        )
        self.conn.commit()

    def save_approval(self, approval: Any) -> None:
        payload = self._payload(approval)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO self_learning_approvals
              (id, created_at, proposal_id, approval_type, status, reviewer, decided_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["approval_id"],
                float(payload["created_at"]),
                payload["proposal_id"],
                payload["approval_type"],
                payload["status"],
                payload.get("reviewer"),
                payload.get("decided_at"),
                self._json(payload),
            ),
        )
        self.conn.commit()

    def save_promotion(self, promotion: Any) -> None:
        payload = self._payload(promotion)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO self_learning_promotions
              (id, created_at, proposal_id, environment, status, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["promotion_id"],
                float(payload["created_at"]),
                payload["proposal_id"],
                payload["environment"],
                payload["status"],
                self._json(payload),
            ),
        )
        self.conn.commit()

    def save_rollback(self, rollback: Any) -> None:
        payload = self._payload(rollback)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO self_learning_rollbacks
              (id, created_at, proposal_id, target, status, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["rollback_id"],
                float(payload["created_at"]),
                payload["proposal_id"],
                payload["target"],
                payload["status"],
                self._json(payload),
            ),
        )
        self.conn.commit()

    def get_approval(self, approval_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT payload_json FROM self_learning_approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def list_records(self, table: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if table not in self.TABLES:
            raise ValueError(f"unsupported self-learning table: {table}")
        rows = self.conn.execute(
            f"SELECT payload_json FROM {table} ORDER BY created_at DESC LIMIT ?",  # nosec B608
            (max(0, int(limit)),),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    @staticmethod
    def _payload(record: Any) -> dict[str, Any]:
        if hasattr(record, "to_dict"):
            return record.to_dict()
        if isinstance(record, dict):
            return dict(record)
        raise TypeError(f"unsupported self-learning record: {type(record)!r}")

    @staticmethod
    def _json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
