from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ApprovalRequired(Exception):
    approval_id: str
    proposal: dict[str, Any]

    def __str__(self) -> str:
        return f"Approval required: {self.approval_id}"
