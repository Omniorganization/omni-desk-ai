from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.models.base import ModelRequest
from omnidesk_agent.models.router import ModelRouter
from omnidesk_agent.tools.base import ToolContext, proposal


class VisionGroundingTool:
    """Vision grounding tool.

    Input: image_path + instruction.
    Output: JSON containing target descriptions and optional coordinates.
    It uses ModelRouter task="vision"; no image base64 is placed into normal agent state.
    """

    name = "vision"

    def __init__(self, router: ModelRouter):
        self.router = router

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action != "ground":
            raise ValueError(f"Unsupported vision action: {action}")
        image_path = Path(str(args["image_path"])).expanduser()
        instruction = str(args.get("instruction") or args.get("expected_result") or "Analyze UI screenshot")
        if not image_path.exists():
            return ToolResult(False, error=f"image_path does not exist: {image_path}")

        ctx.permissions.verify(proposal(
            "vision", "ground",
            {"image_path": str(image_path), "instruction": instruction[:500]},
            "medium", "使用视觉模型分析截图并定位 UI 元素", ctx
        ))

        system = (
            "You are a UI grounding model. Return strict JSON with keys: "
            "summary, elements, target, confidence. If coordinates are available, use x,y,width,height."
        )
        user = f"Analyze this screenshot for the requested UI task:\n{instruction}\nReturn JSON only."
        resp = await self.router.complete(ModelRequest(
            system=system,
            user=user,
            task="vision",
            images=[str(image_path)],
            json_mode=True,
            verified_required=True,
            task_id=str(args.get("task_id", "vision-ground")),
        ))
        text = resp.text.strip()
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"summary": text, "elements": [], "target": None, "confidence": 0}
        return ToolResult(True, data={"grounding": parsed, "provider": resp.provider, "model": resp.model}, summary="vision grounding completed")
