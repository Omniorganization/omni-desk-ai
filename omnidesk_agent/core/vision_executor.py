from __future__ import annotations

from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext


class VisionActionExecutor:
    def __init__(self, tools, min_click_confidence: float = 0.75):
        self.tools = tools
        self.min_click_confidence = min_click_confidence

    async def maybe_click_target(self, grounding_result: ToolResult, instruction: str, ctx: ToolContext) -> ToolResult | None:
        if not grounding_result.ok or not isinstance(grounding_result.data, dict):
            return None
        grounding = grounding_result.data.get("grounding", {})
        target = grounding.get("target") if isinstance(grounding, dict) else None
        if not isinstance(target, dict):
            return None

        confidence = float(target.get("confidence") or grounding.get("confidence") or 0)
        if confidence < self.min_click_confidence:
            return ToolResult(
                False,
                summary="vision target confidence below threshold; human approval required",
                data={"confidence": confidence, "threshold": self.min_click_confidence, "target": target},
            )

        x = target.get("x")
        y = target.get("y")
        width = target.get("width", 0)
        height = target.get("height", 0)
        if x is None or y is None:
            return None

        cx = int(float(x) + float(width or 0) / 2)
        cy = int(float(y) + float(height or 0) / 2)
        return await self.tools.call("computer", "click", {
            "x": cx,
            "y": cy,
            "expected_result": f"Click grounded target for: {instruction}",
        }, ctx)
