from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from omnidesk_agent.privacy.redaction import MemoryPrivacyFilter


@dataclass
class MemoryGovernanceDecision:
    allow_write: bool
    namespace: str
    privacy_level: str
    reason: str = ""


class MemoryGovernance:
    """Runtime memory privacy governance.

    Enforces:
      - PII redaction before write
      - actor/channel namespace
      - deny long-term storage for credential-heavy payloads
      - retention hints through expires_at
    """

    SENSITIVE_MARKERS = {"oauth", "token", "secret", "password", "cookie", "authorization", "private key"}

    def __init__(self, retention_days: int = 30):
        self.retention_days = retention_days
        self.redactor = MemoryPrivacyFilter()

    def namespace(self, channel: str, actor: str) -> str:
        return f"{channel or 'unknown'}:{actor or 'unknown'}"

    def decide(self, text: str, *, channel: str = "unknown", actor: str = "unknown", privacy_level: str = "normal") -> MemoryGovernanceDecision:
        lower = text.lower()
        if any(marker in lower for marker in self.SENSITIVE_MARKERS):
            return MemoryGovernanceDecision(False, self.namespace(channel, actor), "sensitive", "sensitive credential-like content")
        return MemoryGovernanceDecision(True, self.namespace(channel, actor), privacy_level)

    def redact(self, value: Any) -> Any:
        return self.redactor.redact_obj(value)

    def expires_at(self) -> float:
        return time.time() + max(self.retention_days, 0) * 86400
