from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator

RiskLevel = Literal["low", "medium", "high", "critical"]
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
    steps: list[PlanStepSchema]
    success_criteria: str
    rollback_plan: Optional[str] = None

    @field_validator("steps")
    @classmethod
    def at_least_one_step(cls, value: list[PlanStepSchema]) -> list[PlanStepSchema]:
        if not value:
            raise ValueError("plan must contain at least one step")
        return value
