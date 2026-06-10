from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional
from uuid import uuid4
import time

RiskLevel = Literal["low", "medium", "high", "critical"]


@dataclass(init=False)
class ChannelMessage:
    __slots__ = ("channel", "sender_id", "text", "thread_id", "message_id", "raw", "received_at")

    channel: str
    sender_id: str
    text: str
    thread_id: Optional[str]
    message_id: Optional[str]
    raw: dict[str, Any]
    received_at: float

    def __init__(
        self,
        channel: str,
        sender_id: str,
        text: str,
        thread_id: Optional[str] = None,
        message_id: Optional[str] = None,
        raw: Optional[dict[str, Any]] = None,
        received_at: Optional[float] = None,
    ):
        self.channel = channel
        self.sender_id = sender_id
        self.text = text
        self.thread_id = thread_id
        self.message_id = message_id
        self.raw = raw or {}
        self.received_at = time.time() if received_at is None else received_at


@dataclass(init=False)
class PlanStep:
    __slots__ = ("description", "tool", "action", "args", "risk", "requires_approval")

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


@dataclass(init=False)
class Plan:
    __slots__ = ("goal", "steps", "rationale", "plan_id")

    goal: str
    steps: list[PlanStep]
    rationale: str
    plan_id: str

    def __init__(
        self,
        goal: str,
        steps: list[PlanStep],
        rationale: str = "",
        plan_id: Optional[str] = None,
    ):
        self.goal = goal
        self.steps = steps
        self.rationale = rationale
        self.plan_id = plan_id or str(uuid4())


@dataclass(init=False)
class ToolResult:
    __slots__ = ("ok", "data", "error", "summary")

    ok: bool
    data: Any
    error: Optional[str]
    summary: Optional[str]

    def __init__(
        self,
        ok: bool,
        data: Any = None,
        error: Optional[str] = None,
        summary: Optional[str] = None,
    ):
        self.ok = ok
        self.data = data
        self.error = error
        self.summary = summary


@dataclass(init=False)
class ActionProposal:
    __slots__ = (
        "tool", "action", "args", "risk", "reason", "source", "actor",
        "action_id", "run_id", "plan_id", "step_index", "scope_hash"
    )

    tool: str
    action: str
    args: dict[str, Any]
    risk: RiskLevel
    reason: str
    source: str
    actor: str
    action_id: str
    run_id: Optional[str]
    plan_id: Optional[str]
    step_index: Optional[int]
    scope_hash: Optional[str]

    def __init__(
        self,
        tool: str,
        action: str,
        args: dict[str, Any],
        risk: RiskLevel,
        reason: str,
        source: str,
        actor: str,
        action_id: Optional[str] = None,
        run_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        step_index: Optional[int] = None,
        scope_hash: Optional[str] = None,
    ):
        self.tool = tool
        self.action = action
        self.args = args
        self.risk = risk
        self.reason = reason
        self.source = source
        self.actor = actor
        self.action_id = action_id or str(uuid4())
        self.run_id = run_id
        self.plan_id = plan_id
        self.step_index = step_index
        self.scope_hash = scope_hash


@dataclass(init=False)
class ApprovalDecision:
    __slots__ = ("allowed", "mode", "reason")

    allowed: bool
    mode: Literal["allow", "deny", "dry_run"]
    reason: str

    def __init__(self, allowed: bool, mode: Literal["allow", "deny", "dry_run"], reason: str = ""):
        self.allowed = allowed
        self.mode = mode
        self.reason = reason
