from __future__ import annotations

import base64
import io
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal


class ComputerTool:
    name = "computer"

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

    async def screenshot(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        decision = ctx.permissions.verify(proposal(
            "computer", "screenshot", args, "medium", "读取当前屏幕内容以进行视觉理解", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary="dry-run: screenshot skipped")
        import pyautogui
        img = pyautogui.screenshot()
        max_width = int(args.get("max_width", 1280))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return ToolResult(True, data={"png_base64": b64, "width": img.width, "height": img.height}, summary="screenshot captured")

    async def click(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        x, y = int(args["x"]), int(args["y"])
        button = args.get("button", "left")
        clicks = int(args.get("clicks", 1))
        decision = ctx.permissions.verify(proposal(
            "computer", "click", {"x": x, "y": y, "button": button, "clicks": clicks}, "high",
            "即将移动鼠标并点击屏幕坐标", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: click({x},{y})")
        import pyautogui
        pyautogui.click(x=x, y=y, clicks=clicks, button=button)
        return ToolResult(True, summary=f"clicked {x},{y}")

    async def move(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        x, y = int(args["x"]), int(args["y"])
        decision = ctx.permissions.verify(proposal(
            "computer", "move", {"x": x, "y": y}, "medium", "即将移动鼠标", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: move({x},{y})")
        import pyautogui
        pyautogui.moveTo(x, y, duration=float(args.get("duration", 0.1)))
        return ToolResult(True, summary=f"moved {x},{y}")

    async def type_text(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        text = str(args.get("text", ""))
        preview = text[:200]
        decision = ctx.permissions.verify(proposal(
            "computer", "type_text", {"text_preview": preview, "length": len(text)}, "high",
            "即将向当前焦点窗口输入文本", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: type {len(text)} chars")
        import pyautogui
        pyautogui.write(text, interval=float(args.get("interval", 0.01)))
        return ToolResult(True, summary=f"typed {len(text)} chars")

    async def hotkey(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        keys = list(args.get("keys", []))
        if not keys:
            raise ValueError("hotkey requires keys list")
        decision = ctx.permissions.verify(proposal(
            "computer", "hotkey", {"keys": keys}, "high", "即将触发系统/应用快捷键", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: hotkey {keys}")
        import pyautogui
        pyautogui.hotkey(*keys)
        return ToolResult(True, summary=f"hotkey {keys}")
