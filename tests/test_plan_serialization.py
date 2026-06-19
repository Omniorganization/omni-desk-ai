from __future__ import annotations
from dataclasses import asdict

from omnidesk_agent.core.models import ChannelMessage, Plan, PlanStep
from omnidesk_agent.core.serialization import message_from_dict, plan_from_dict


def test_slots_dataclasses_serialize_without_dict():
    msg = ChannelMessage(channel="local", sender_id="u", text="hello")
    assert not hasattr(msg, "__dict__")
    restored = message_from_dict(asdict(msg))
    assert restored.text == "hello"

    plan = Plan(goal="g", steps=[PlanStep(description="d", tool="files", action="list")], rationale="r")
    restored_plan = plan_from_dict(asdict(plan))
    assert restored_plan.steps[0].requires_approval is True
