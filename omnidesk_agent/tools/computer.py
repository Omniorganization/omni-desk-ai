from __future__ import annotations

import base64
import hashlib
import io
import time
from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal


class ComputerTool:
    name = "computer"

    def __init__(self, screenshot_dir: Path | None = None):
        self._last_screenshot_hash: str | None = None
        self._last_screenshot_at: float = 0.0
        self.screenshot_dir = (screenshot_dir or Path("~/.omnidesk/screenshots")).expanduser()
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)


    def spec(self):
        from omnidesk_agent.tools.spec import ActionSpec, ToolSpec, obj_schema
        return ToolSpec(
            name=self.name,
            description="Computer-use tool for screenshots, clicking, typing, moving mouse, and hotkeys.",
            permissions=["computer.screenshot", "computer.input"],
            actions={
                "screenshot": ActionSpec("screenshot", "Capture screen to file; base64 opt-in", obj_schema({
                    "expected_result": {"type": "string"},
                    "max_width": {"type": "integer"},
                    "return_base64": {"type": "boolean"},
                    "skip_if_unchanged": {"type": "boolean"},
                    "skip_if_too_soon": {"type": "boolean"},
                    "auto_ground": {"type": "boolean"},
                    "auto_click_grounded": {"type": "boolean"}
                }, required=["expected_result"], additional=True), risk="medium", side_effect=False, requires_approval=True),
                "click": ActionSpec("click", "Click screen coordinates", obj_schema({
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "expected_result": {"type": "string"}
                }, required=["x", "y", "expected_result"], additional=True), risk="high", side_effect=True, requires_approval=True),
                "type_text": ActionSpec("type_text", "Type text into focused UI", obj_schema({
                    "text": {"type": "string"},
                    "expected_result": {"type": "string"}
                }, required=["text", "expected_result"], additional=True), risk="high", side_effect=True, requires_approval=True),
                "hotkey": ActionSpec("hotkey", "Press keyboard shortcut", obj_schema({
                    "keys": {"type": "array"},
                    "expected_result": {"type": "string"}
                }, required=["keys", "expected_result"], additional=True), risk="high", side_effect=True, requires_approval=True),
                "move": ActionSpec("move", "Move mouse pointer", obj_schema({
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "expected_result": {"type": "string"}
                }, required=["x", "y", "expected_result"], additional=True), risk="medium", side_effect=True, requires_approval=True),
            },
        )

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
            raise ValueError(f"computer.{action} requires expected_result/reason before execution")
        return expected

    async def screenshot(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        expected = self._require_expected_result(args, "screenshot")
        min_interval = float(args.get("min_interval_seconds", 1.0))
        return_base64 = bool(args.get("return_base64", False))

        decision = ctx.permissions.verify(proposal(
            "computer", "screenshot",
            {"expected_result": expected, "max_width": args.get("max_width", 960), "return_base64": return_base64},
            "medium", "读取当前屏幕前先声明结果目标，默认保存文件而非返回 base64", ctx
        ))
        if decision.mode == "dry_run":
            return ToolResult(False, summary="dry-run: screenshot skipped")

        import pyautogui

        img = pyautogui.screenshot()
        original_width, original_height = img.width, img.height
        max_width = int(args.get("max_width", 960))
        scale_ratio = 1.0
        if img.width > max_width:
            scale_ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * scale_ratio)))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        now = time.time()

        if args.get("skip_if_unchanged", True) and digest == self._last_screenshot_hash:
            return ToolResult(True, data={"skipped_analysis": True, "reason": "unchanged_screenshot", "hash": digest}, summary="screenshot unchanged; skip visual analysis")

        if args.get("skip_if_too_soon", True) and now - self._last_screenshot_at < min_interval:
            return ToolResult(True, data={"skipped_analysis": True, "reason": "too_frequent", "hash": digest}, summary="screenshot too frequent; wait for UI change before analysis")

        self._last_screenshot_hash = digest
        self._last_screenshot_at = now

        out_path = self.screenshot_dir / f"{digest}.png"
        out_path.write_bytes(raw)

        data: dict[str, Any] = {
            "image_path": str(out_path),
            "width": img.width,
            "height": img.height,
            "original_width": original_width,
            "original_height": original_height,
            "scaled_width": img.width,
            "scaled_height": img.height,
            "scale_ratio": scale_ratio,
            "hash": digest,
            "expected_result": expected,
            "base64_returned": False,
        }
        if return_base64:
            data["png_base64"] = base64.b64encode(raw).decode("ascii")
            data["base64_returned"] = True

        return ToolResult(True, data=data, summary=f"screenshot saved to {out_path}")

    async def click(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        expected = self._require_expected_result(args, "click")
        x, y = int(args["x"]), int(args["y"])
        button = args.get("button", "left")
        clicks = int(args.get("clicks", 1))
        decision = ctx.permissions.verify(proposal("computer", "click", {"x": x, "y": y, "button": button, "clicks": clicks, "expected_result": expected}, "high", "即将点击屏幕坐标", ctx))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: click({x},{y})")
        import pyautogui
        pyautogui.click(x=x, y=y, clicks=clicks, button=button)
        return ToolResult(True, summary=f"clicked {x},{y}; expected: {expected}")

    async def move(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        expected = self._require_expected_result(args, "move")
        x, y = int(args["x"]), int(args["y"])
        decision = ctx.permissions.verify(proposal("computer", "move", {"x": x, "y": y, "expected_result": expected}, "medium", "即将移动鼠标", ctx))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: move({x},{y})")
        import pyautogui
        pyautogui.moveTo(x, y, duration=float(args.get("duration", 0.1)))
        return ToolResult(True, summary=f"moved {x},{y}; expected: {expected}")

    async def type_text(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        expected = self._require_expected_result(args, "type_text")
        text = str(args.get("text", ""))
        decision = ctx.permissions.verify(proposal("computer", "type_text", {"text_preview": text[:200], "length": len(text), "expected_result": expected}, "high", "即将向当前焦点窗口输入文本", ctx))
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
        decision = ctx.permissions.verify(proposal("computer", "hotkey", {"keys": keys, "expected_result": expected}, "high", "即将触发系统/应用快捷键", ctx))
        if decision.mode == "dry_run":
            return ToolResult(False, summary=f"dry-run: hotkey {keys}")
        import pyautogui
        pyautogui.hotkey(*keys)
        return ToolResult(True, summary=f"hotkey {keys}; expected: {expected}")
