from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator

RiskLevel = Literal["low", "medium", "high", "critical"]
# Upper bound on the steps one plan or turn may list; the loop runs at most
# agent.max_steps_per_turn of them per turn.
MAX_PLAN_STEPS = 50
TaskType = Literal["chat", "file", "browser", "desktop", "channel", "code", "self_upgrade", "gmail", "unknown"]
PlanStatus = Literal["planned", "waiting_approval", "running", "verifying", "retrying", "completed", "failed", "rolled_back"]


class RetryPolicy(BaseModel):
    max_retries: int = 0
    backoff_seconds: float = 1.0


class VerificationSpec(BaseModel):
    tool: Optional[str] = None
    action: Optional[str] = None
    args: dict[str, Any] = Field(default_factory=dict)
    expected: Optional[str] = None


class PlanStepSchema(BaseModel):
    description: str
    tool: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = "medium"
    requires_approval: bool = True
    expected_result: str
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    rollback_action: Optional[dict[str, Any]] = None
    verification: Optional[VerificationSpec] = None

    @field_validator("expected_result")
    @classmethod
    def expected_result_required(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("expected_result is required for every step")
        return value.strip()


class StructuredPlan(BaseModel):
    goal: str
    task_type: TaskType = "unknown"
    risk: RiskLevel = "medium"
    steps: list[PlanStepSchema] = Field(max_length=MAX_PLAN_STEPS)
    success_criteria: str
    rollback_plan: Optional[str] = None

    @field_validator("steps")
    @classmethod
    def at_least_one_step(cls, value: list[PlanStepSchema]) -> list[PlanStepSchema]:
        if not value:
            raise ValueError("plan must contain at least one step")
        return value


TurnStatus = Literal["continue", "done", "give_up"]


class PlannerTurnSchema(BaseModel):
    """One planner turn of the agent loop.

    `continue` lists the next steps to run (at least one); `done` and `give_up`
    end the run and carry the answer for the requester. A plain StructuredPlan
    (no status) parses as a `continue` turn.
    """

    status: TurnStatus = "continue"
    goal: str = ""
    task_type: TaskType = "unknown"
    risk: RiskLevel = "medium"
    steps: list[PlanStepSchema] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)
    success_criteria: str = ""
    final_answer: str = ""
    rollback_plan: Optional[str] = None

    def as_structured_plan(self) -> StructuredPlan:
        return StructuredPlan(
            goal=self.goal or "(goal not stated)",
            task_type=self.task_type,
            risk=self.risk,
            steps=self.steps,
            success_criteria=self.success_criteria or "(not stated)",
            rollback_plan=self.rollback_plan,
        )
