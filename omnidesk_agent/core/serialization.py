from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from omnidesk_agent.core.models import ChannelMessage, Plan, PlanStep


def dataclass_to_dict(obj: Any) -> dict[str, Any]:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    raise TypeError(f"Expected dataclass or dict, got {type(obj).__name__}")


def plan_from_dict(data: dict[str, Any]) -> Plan:
    steps = []
    for raw in data.get("steps", []):
        raw = dict(raw)
        if "requires_confirmation" in raw and "requires_approval" not in raw:
            raw["requires_approval"] = raw.pop("requires_confirmation")
        steps.append(PlanStep(**raw))
    plan_id = data.get("plan_id") or data.get("id")
    if plan_id is not None:
        return Plan(goal=data["goal"], steps=steps, rationale=data.get("rationale", ""), plan_id=str(plan_id))
    return Plan(goal=data["goal"], steps=steps, rationale=data.get("rationale", ""))


def message_from_dict(data: dict[str, Any]) -> ChannelMessage:
    return ChannelMessage(**data)
