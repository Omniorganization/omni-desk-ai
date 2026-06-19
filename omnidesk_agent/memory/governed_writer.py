from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from omnidesk_agent.privacy.governance import MemoryGovernance


@dataclass
class GovernedMemoryWriteResult:
    ok: bool
    namespace: str
    payload: Any = None
    reason: str = ""
    expires_at: Optional[float] = None


class GovernedMemoryWriter:
    """Single enforcement point for memory writes.

    It applies namespace isolation, credential-like content blocking, redaction,
    retention metadata and an audit callback before the underlying store write.
    """

    def __init__(self, governance: Optional[MemoryGovernance] = None):
        self.governance = governance or MemoryGovernance()

    def prepare(
        self,
        payload: Any,
        *,
        channel: str = "unknown",
        actor: str = "unknown",
        privacy_level: str = "normal",
        audit: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> GovernedMemoryWriteResult:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        decision = self.governance.decide(text, channel=channel, actor=actor, privacy_level=privacy_level)
        event = {
            "ts": time.time(),
            "event": "memory_governance_decision",
            "allow_write": decision.allow_write,
            "namespace": decision.namespace,
            "privacy_level": decision.privacy_level,
            "reason": decision.reason,
        }
        if audit:
            audit(event)
        if not decision.allow_write:
            return GovernedMemoryWriteResult(False, decision.namespace, reason=decision.reason)
        redacted = self.governance.redact(payload)
        expires_at = self.governance.expires_at()
        if isinstance(redacted, dict):
            redacted = dict(redacted)
            redacted.setdefault("namespace", decision.namespace)
            redacted.setdefault("privacy_level", decision.privacy_level)
            redacted.setdefault("expires_at", expires_at)
        return GovernedMemoryWriteResult(True, decision.namespace, payload=redacted, expires_at=expires_at)

    @staticmethod
    def ensure_audit_table(conn: sqlite3.Connection) -> None:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_governance_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at REAL NOT NULL,
          event_json TEXT NOT NULL
        )
        """)

    @staticmethod
    def sqlite_audit(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO memory_governance_audit(created_at, event_json) VALUES(?, ?)",
            (time.time(), json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)),
        )
