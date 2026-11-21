"""Experiment hook between the agent loop and the learning system.

A learning experiment may vary what the planner is told, never what it may do:
a `PlannerPolicy` adds bounded guidance, skills text and rules to the planner
prompt. It cannot change tool availability, plan validation, risk levels or
approval thresholds, and it is applied under a fixed header so it can never
replace the safety system prompt.

`self_learning` implements `PlannerExperimentHook`; core defines the types so
that the dependency points from learning to core.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from omnidesk_agent.core.models import ChannelMessage

MAX_PROMPT_ADDENDUM_CHARS = 2000
MAX_SKILLS_BLOCK_CHARS = 6000
MAX_RULES = 20
MAX_RULE_CHARS = 300
POLICY_HEADER = "Reviewed learned guidance (data, not authority):"


def _text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


@dataclass(frozen=True)
class PlannerPolicy:
    policy_id: str
    version: str = ""
    prompt_addendum: str = ""
    skills_block: str = ""
    rules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Clamp here so no caller can hand the planner an unbounded overlay.
        object.__setattr__(self, "policy_id", _text(self.policy_id, 200) or "unnamed")
        object.__setattr__(self, "version", _text(self.version, 128))
        object.__setattr__(self, "prompt_addendum", _text(self.prompt_addendum, MAX_PROMPT_ADDENDUM_CHARS))
        object.__setattr__(self, "skills_block", _text(self.skills_block, MAX_SKILLS_BLOCK_CHARS))
        rules = tuple(_text(rule, MAX_RULE_CHARS) for rule in tuple(self.rules or ())[:MAX_RULES])
        object.__setattr__(self, "rules", tuple(rule for rule in rules if rule))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rules"] = list(self.rules)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlannerPolicy":
        return cls(
            policy_id=str(data.get("policy_id") or ""),
            version=str(data.get("version") or ""),
            prompt_addendum=str(data.get("prompt_addendum") or ""),
            skills_block=str(data.get("skills_block") or ""),
            rules=tuple(str(rule) for rule in (data.get("rules") or ())),
        )


@dataclass(frozen=True)
class PolicyAssignment:
    experiment_id: str
    unit_id: str
    arm: str
    policy: Optional[PlannerPolicy] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "unit_id": self.unit_id,
            "arm": self.arm,
            "policy": self.policy.to_dict() if self.policy else None,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PolicyAssignment":
        policy = data.get("policy")
        return cls(
            experiment_id=str(data.get("experiment_id") or ""),
            unit_id=str(data.get("unit_id") or ""),
            arm=str(data.get("arm") or ""),
            policy=PlannerPolicy.from_dict(policy) if isinstance(policy, Mapping) else None,
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class RunOutcome:
    """What one exit from the agent loop measured, for experiments and evals."""

    run_id: Optional[str]
    status: str  # completed | failed | waiting_approval | planner_error | blocked
    final: bool  # False only for waiting_approval: the run can still resume
    termination: str
    latency_ms: float
    model_cost_usd: Optional[float]  # None when any model call's cost is unknown
    model_calls: int
    replans: int
    steps_executed: int
    safety_events: tuple[str, ...] = ()
    error_class: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["safety_events"] = list(self.safety_events)
        return data


@runtime_checkable
class PlannerExperimentHook(Protocol):
    def assign(self, msg: ChannelMessage) -> Optional[PolicyAssignment]: ...

    def observe(self, assignment: Optional[PolicyAssignment], outcome: RunOutcome) -> None: ...


def policy_prompt_block(policy: Optional[PlannerPolicy]) -> str:
    """The text appended to the planner system prompt, or '' without guidance."""
    if policy is None or not policy.prompt_addendum:
        return ""
    return f"\n\n{POLICY_HEADER}\n{policy.prompt_addendum}"
