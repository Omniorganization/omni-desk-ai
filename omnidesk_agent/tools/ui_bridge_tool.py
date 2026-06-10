from __future__ import annotations
from typing import Any
from omnidesk_agent.config import UIBridgeConfig
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal
from omnidesk_agent.tools.registry import ToolRegistry

class UIBridgeTool:
    name = "ui_bridge"
    def __init__(self, cfg: UIBridgeConfig, tools: ToolRegistry):
        self.cfg = cfg
        self.tools = tools
    def _check_app(self, app: str) -> None:
        if self.cfg.allowed_apps and app not in self.cfg.allowed_apps:
            raise ValueError(f"App is not allowed for UI bridge: {app}")


    def spec(self):
        from omnidesk_agent.tools.spec import ActionSpec, ToolSpec
        return ToolSpec(
            name=self.name,
            description="Visible UI bridge for GUI-only applications.",
            permissions=["ui_bridge.observe", "ui_bridge.input"],
            actions={
                "observe": ActionSpec("observe", "Observe visible app screen", {"app": "string", "expected_result": "string"}, risk="medium", side_effect=False, requires_approval=True),
                "click": ActionSpec("click", "Click visible UI coordinate", {"app": "string", "x": "integer", "y": "integer"}, risk="high", side_effect=True, requires_approval=True),
                "type_visible_reply": ActionSpec("type_visible_reply", "Type into visible UI", {"app": "string", "text": "string"}, risk="high", side_effect=True, requires_approval=True),
                "press_send": ActionSpec("press_send", "Press send in visible UI", {"app": "string"}, risk="high", side_effect=True, requires_approval=True),
            },
        )


    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        app = str(args.get("app", ""))
        if app:
            self._check_app(app)
        if action == "observe":
            expected = str(args.get("expected_result") or f"Observe visible {app or 'desktop'} UI before any action")
            ctx.permissions.verify(proposal("ui_bridge", "observe", {"app": app, "expected_result": expected}, "medium", "通过可见 UI Bridge 观察屏幕", ctx))
            return await self.tools.call("computer", "screenshot", {"max_width": int(args.get("max_width", 960)), "expected_result": expected, "skip_if_unchanged": True, "skip_if_too_soon": True}, ctx)
        if action == "type_visible_reply":
            text = str(args["text"])
            expected = str(args.get("expected_result") or f"Type visible reply in {app}")
            ctx.permissions.verify(proposal("ui_bridge", "type_visible_reply", {"app": app, "text_preview": text[:200], "length": len(text), "expected_result": expected}, "high", "通过可见 UI Bridge 输入文字", ctx))
            return await self.tools.call("computer", "type_text", {"text": text, "expected_result": expected}, ctx)
        if action == "press_send":
            expected = str(args.get("expected_result") or f"Send currently visible composed message in {app}")
            ctx.permissions.verify(proposal("ui_bridge", "press_send", {"app": app, "expected_result": expected}, "high", "通过可见 UI Bridge 触发发送动作", ctx))
            return await self.tools.call("computer", "hotkey", {"keys": ["enter"], "expected_result": expected}, ctx)
        if action == "click":
            expected = str(args.get("expected_result") or f"Click visible UI element in {app}")
            ctx.permissions.verify(proposal("ui_bridge", "click", {"app": app, "x": args.get("x"), "y": args.get("y"), "expected_result": expected}, "high", "通过可见 UI Bridge 点击", ctx))
            return await self.tools.call("computer", "click", {"x": int(args["x"]), "y": int(args["y"]), "expected_result": expected}, ctx)
        raise ValueError(f"Unsupported ui_bridge action: {action}")
