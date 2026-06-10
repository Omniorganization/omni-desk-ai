from __future__ import annotations

import base64
import hashlib
import io
import time
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal


class ComputerTool:
    name = "computer"

    def __init__(self):
        self._last_screenshot_hash: str | None = None
        self._last_screenshot_at: float = 0.0

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action == "screenshot":
            return await self.screenshot(args, ctx)
        if action == "click":
            return await self.click(args, ctx)
        if action == "type_text":
            return await self.type_text(args, ctx)
        if action == "hotkey":
            return await self.hotkey(args, ctx)
        if action == "move":
            return await self.move(args, ctx)
        raise ValueError(f"Unsupported computer action: {action}")

    def _require_expected_result(self, args: dict[str, Any], action: str) -> str:
        expected = str(args.get("expected_result") or args.get("reason") or "").strip()
        if not expected:
            raise ValueError(
                f"computer.{action} requires expected_result/reason before execution "
                "to avoid wasteful screen/action loops"
            )
        return expected

    async def screenshot(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        expected = self._require_expected_result(args, "screenshot")
        min_interval = float(args.get("min_interval_seconds", 1.0))

        decision = ctx.permissions.verify(proposal(
            "computer",
            "screenshot",
            {"expected_result": expected, "max_width": args.get("max_width", 960)},
            "medium",
            "读取当前屏幕前先声明结果目标，避免无目的截图和视觉 token 浪费",
            ctx,
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary="dry-run: screenshot skipped")

        import pyautogui

        img = pyautogui.screenshot()
        max_width = int(args.get("max_width", 960))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        now = time.time()

        if args.get("skip_if_unchanged", True) and digest == self._last_screenshot_hash:
            return ToolResult(
                True,
                data={"skipped_analysis": True, "reason": "unchanged_screenshot", "hash": digest},
                summary="screenshot unchanged; skip visual analysis",
            )

        if args.get("skip_if_too_soon", True) and now - self._last_screenshot_at < min_interval:
            return ToolResult(
                True,
                data={"skipped_analysis": True, "reason": "too_frequent", "hash": digest},
                summary="screenshot too frequent; wait for UI change before analysis",
            )

        self._last_screenshot_hash = digest
        self._last_screenshot_at = now
        b64 = base64.b64encode(raw).decode("ascii")
        return ToolResult(
            True,
            data={
                "png_base64": b64,
                "width": img.width,
                "height": img.height,
                "hash": digest,
                "expected_result": expected,
            },
            summary=f"screenshot captured for: {expected}",
        )

    async def click(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        expected = self._require_expected_result(args, "click")
        x, y = int(args["x"]), int(args["y"])
        button = args.get("button", "left")
        clicks = int(args.get("clicks", 1))
        decision = ctx.permissions.verify(proposal(
            "computer", "click", {"x": x, "y": y, "button": button, "clicks": clicks, "expected_result": expected}, "high",
            "即将点击屏幕坐标；执行前必须明确点击后的目标结果", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: click({x},{y})")
        import pyautogui
        pyautogui.click(x=x, y=y, clicks=clicks, button=button)
        return ToolResult(True, summary=f"clicked {x},{y}; expected: {expected}")

    async def move(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        expected = self._require_expected_result(args, "move")
        x, y = int(args["x"]), int(args["y"])
        decision = ctx.permissions.verify(proposal(
            "computer", "move", {"x": x, "y": y, "expected_result": expected}, "medium", "即将移动鼠标；执行前必须明确目标结果", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: move({x},{y})")
        import pyautogui
        pyautogui.moveTo(x, y, duration=float(args.get("duration", 0.1)))
        return ToolResult(True, summary=f"moved {x},{y}; expected: {expected}")

    async def type_text(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        expected = self._require_expected_result(args, "type_text")
        text = str(args.get("text", ""))
        preview = text[:200]
        decision = ctx.permissions.verify(proposal(
            "computer", "type_text", {"text_preview": preview, "length": len(text), "expected_result": expected}, "high",
            "即将向当前焦点窗口输入文本；执行前必须明确输入后的目标结果", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: type {len(text)} chars")
        import pyautogui
        pyautogui.write(text, interval=float(args.get("interval", 0.01)))
        return ToolResult(True, summary=f"typed {len(text)} chars; expected: {expected}")

    async def hotkey(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        expected = self._require_expected_result(args, "hotkey")
        keys = list(args.get("keys", []))
        if not keys:
            raise ValueError("hotkey requires keys list")
        decision = ctx.permissions.verify(proposal(
            "computer", "hotkey", {"keys": keys, "expected_result": expected}, "high", "即将触发系统/应用快捷键；执行前必须明确目标结果", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: hotkey {keys}")
        import pyautogui
        pyautogui.hotkey(*keys)
        return ToolResult(True, summary=f"hotkey {keys}; expected: {expected}")
