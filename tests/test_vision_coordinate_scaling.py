from __future__ import annotations

import asyncio

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.core.vision_executor import VisionActionExecutor


class DummyTools:
    def __init__(self):
        self.calls = []

    async def call(self, tool, action, args, ctx):
        self.calls.append((tool, action, args))
        return ToolResult(True, summary="ok")


def test_vision_click_scales_coordinates():
    async def run_case():
        tools = DummyTools()
        executor = VisionActionExecutor(tools, min_click_confidence=0.5)
        ground = ToolResult(True, data={"grounding": {"target": {"x": 50, "y": 25, "width": 10, "height": 10, "confidence": 0.9}}})
        await executor.maybe_click_target(ground, "click", ctx=None, screenshot_metadata={"scale_ratio": 0.5})
        _, _, args = tools.calls[0]
        assert args["x"] == 110
        assert args["y"] == 60

    asyncio.run(run_case())
