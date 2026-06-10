from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from uuid import uuid4
import time

RiskLevel = Literal["low", "medium", "high", "critical"]


@dataclass
class ChannelMessage:
    channel: str
    sender_id: str
    text: str
    thread_id: Optional[str] = None
    message_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)


@dataclass
class PlanStep:
    description: str
    tool: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    risk: RiskLevel = "medium"
    requires_approval: bool = True

    # Backward-compatible alias for older code/config.
    @property
    def requires_confirmation(self) -> bool:
        return self.requires_approval

    @requires_confirmation.setter
    def requires_confirmation(self, value: bool) -> None:
        self.requires_approval = value


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep]
    rationale: str = ""
    plan_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None
    summary: Optional[str] = None


@dataclass
class ActionProposal:
    tool: str
    action: str
    args: dict[str, Any]
    risk: RiskLevel
    reason: str
    source: str
    actor: str
    action_id: str = field(default_factory=lambda: str(uuid4()))
    run_id: Optional[str] = None
    plan_id: Optional[str] = None
    step_index: Optional[int] = None
    scope_hash: Optional[str] = None


@dataclass
class ApprovalDecision:
    allowed: bool
    mode: Literal["allow", "deny", "dry_run"]
    reason: str = ""
