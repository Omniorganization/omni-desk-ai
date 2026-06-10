from __future__ import annotations

import asyncio
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext


class VisionActionExecutor:
    def __init__(self, tools, min_click_confidence: float = 0.75):
        self.tools = tools
        self.min_click_confidence = min_click_confidence

    async def maybe_click_target(
        self,
        grounding_result: ToolResult,
        instruction: str,
        ctx: ToolContext,
        screenshot_metadata: dict[str, Any] | None = None,
    ) -> ToolResult | None:
        if not grounding_result.ok or not isinstance(grounding_result.data, dict):
            return None
        target = self._target(grounding_result)
        if not target:
            return None

        confidence = float(target.get("confidence") or 0)
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

        cx = float(x) + float(width or 0) / 2
        cy = float(y) + float(height or 0) / 2

        scale_ratio = 1.0
        if screenshot_metadata:
            scale_ratio = float(screenshot_metadata.get("scale_ratio") or 1.0)
        if scale_ratio <= 0:
            scale_ratio = 1.0

        screen_x = int(cx / scale_ratio)
        screen_y = int(cy / scale_ratio)

        return await self.tools.call("computer", "click", {
            "x": screen_x,
            "y": screen_y,
            "expected_result": f"Click grounded target for: {instruction}",
        }, ctx)

    async def verify_with_retry(
        self,
        *,
        ctx: ToolContext,
        instruction: str,
        verification: dict[str, Any],
        retry_policy: dict[str, Any] | None = None,
    ) -> ToolResult:
        retry_policy = retry_policy or {}
        max_retries = int(retry_policy.get("max_retries", 0))
        backoff = float(retry_policy.get("backoff_seconds", 1.0))
        expected = str(verification.get("expected") or verification.get("expected_result") or instruction)

        last_result: ToolResult | None = None
        for attempt in range(max_retries + 1):
            shot = await self.tools.call("computer", "screenshot", {
                "expected_result": f"Verify result: {expected}",
                "skip_if_unchanged": False,
                "auto_ground": False,
            }, ctx)
            if not shot.ok or not isinstance(shot.data, dict) or not shot.data.get("image_path"):
                last_result = shot
            else:
                ground = await self.tools.call("vision", "ground", {
                    "image_path": shot.data["image_path"],
                    "instruction": f"Verify whether this condition is satisfied: {expected}. Return target.confidence as satisfaction confidence.",
                    "expected_result": expected,
                }, ctx)
                last_result = ground
                if self._verified(ground, float(verification.get("min_confidence", 0.70))):
                    return ToolResult(True, data={"attempt": attempt, "verification": ground.data}, summary=f"vision verification passed: {expected}")

            if attempt < max_retries:
                await asyncio.sleep(backoff * (attempt + 1))

        return ToolResult(False, data={"last_result": self._safe_result(last_result), "expected": expected}, summary=f"vision verification failed: {expected}")

    @staticmethod
    def _target(result: ToolResult) -> dict[str, Any] | None:
        data = result.data if isinstance(result.data, dict) else {}
        grounding = data.get("grounding", {})
        if not isinstance(grounding, dict):
            return None
        target = grounding.get("target")
        if isinstance(target, dict):
            if "confidence" not in target and "confidence" in grounding:
                target["confidence"] = grounding.get("confidence")
            return target
        return None

    @classmethod
    def _verified(cls, result: ToolResult, min_confidence: float) -> bool:
        if not result.ok:
            return False
        target = cls._target(result)
        if target:
            return float(target.get("confidence") or 0) >= min_confidence
        data = result.data if isinstance(result.data, dict) else {}
        grounding = data.get("grounding", {})
        if isinstance(grounding, dict):
            return bool(grounding.get("verified")) or float(grounding.get("confidence") or 0) >= min_confidence
        return False

    @staticmethod
    def _safe_result(result: ToolResult | None) -> dict[str, Any] | None:
        if result is None:
            return None
        return {"ok": result.ok, "summary": result.summary, "error": result.error, "data": result.data}
