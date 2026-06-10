from __future__ import annotations
import pytest

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.core.vision_executor import VisionActionExecutor
from omnidesk_agent.security.approval_required import ApprovalRequired


class ApprovalPermissions:
    def verify(self, proposal):
        raise ApprovalRequired("approval-1", proposal.__dict__ if hasattr(proposal, "__dict__") else proposal)


class Ctx:
    permissions = ApprovalPermissions()
    source = "test"
    actor = "u"
    run_id = "r"
    plan_id = "p"
    step_index = 0


class DummyTools:
    async def call(self, tool, action, args, ctx):
        return ToolResult(True, data=args, summary="clicked")


@pytest.mark.asyncio
async def test_low_confidence_grounded_click_raises_approval():
    executor = VisionActionExecutor(DummyTools(), min_click_confidence=0.9)
    ground = ToolResult(True, data={"grounding": {"target": {"x": 10, "y": 20, "width": 10, "height": 10, "confidence": 0.4}}})
    with pytest.raises(ApprovalRequired):
        await executor.maybe_click_target(ground, "click", Ctx(), screenshot_metadata={"scale_ratio": 1.0})
