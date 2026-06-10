from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from uuid import uuid4
import time

RiskLevel = Literal["low", "medium", "high", "critical"]


class _NoPublicDict:
    """Hide `__dict__` while preserving Python 3.9 dataclass compatibility.

    Python 3.9 does not support `@dataclass(slots=True)`. Using explicit
    `__slots__` with dataclass defaults can break collection. This shim keeps
    `dataclasses.asdict()` working and prevents runtime code from relying on
    `obj.__dict__`.
    """

    def __getattribute__(self, name):
        if name == "__dict__":
            raise AttributeError(name)
        return object.__getattribute__(self, name)


@dataclass
class ChannelMessage(_NoPublicDict):
    channel: str
    sender_id: str
    text: str
    thread_id: Optional[str] = None
    message_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)


@dataclass(init=False)
class PlanStep(_NoPublicDict):
    description: str
    tool: str
    action: str
    args: dict[str, Any]
    risk: RiskLevel
    requires_approval: bool

    def __init__(
        self,
        description: str,
        tool: str,
        action: str,
        args: Optional[dict[str, Any]] = None,
        risk: RiskLevel = "medium",
        requires_approval: bool = True,
        requires_confirmation: Optional[bool] = None,
    ):
        self.description = description
        self.tool = tool
        self.action = action
        self.args = args or {}
        self.risk = risk
        self.requires_approval = requires_approval if requires_confirmation is None else bool(requires_confirmation)

    @property
    def requires_confirmation(self) -> bool:
        return self.requires_approval

    @requires_confirmation.setter
    def requires_confirmation(self, value: bool) -> None:
        self.requires_approval = value


@dataclass
class Plan(_NoPublicDict):
    goal: str
    steps: list[PlanStep]
    rationale: str = ""
    plan_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class ToolResult(_NoPublicDict):
    ok: bool
    data: Any = None
    error: Optional[str] = None
    summary: Optional[str] = None


@dataclass
class ActionProposal(_NoPublicDict):
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
class ApprovalDecision(_NoPublicDict):
    allowed: bool
    mode: Literal["allow", "deny", "dry_run"]
    reason: str = ""
