from __future__ import annotations
from typing import Any, Callable, Optional
from omnidesk_agent.config import UIBridgeConfig
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, bound_content, proposal, permission_guarded
from omnidesk_agent.tools.foreground import ForegroundWindow, looks_like_sign_in, probe_foreground, window_matches_app
from omnidesk_agent.tools.registry import ToolRegistry

_ACTIONS = ("observe", "click", "type_visible_reply", "press_send")


class UIBridgeTool:
    """Acts in an already-open, signed-in app the operator can see, one approved step at a time.

    Every action names its app, which must be on the allowlist. With
    require_foreground_confirmation (the default) that app must also be the
    foreground window, both before the approval request and again right before
    acting, and a window titled as a sign-in screen is refused: the bridge never
    signs in and never types into whatever else has focus. If the foreground
    app cannot be determined, the action is refused.
    """

    name = "ui_bridge"

    def __init__(self, cfg: UIBridgeConfig, tools: ToolRegistry, *, foreground_probe: Callable[[], Optional[ForegroundWindow]] = probe_foreground):
        self.cfg = cfg
        self.tools = tools
        self.foreground_probe = foreground_probe

    def _require_enabled(self) -> None:
        if not self.cfg.enabled:
            raise PermissionError("UI bridge is disabled by configuration")

    def _check_app(self, app: str) -> str:
        app = app.strip()
        if not app:
            raise PermissionError("UI bridge actions must name the target app")
        if self.cfg.allowed_apps and app not in self.cfg.allowed_apps:
            raise PermissionError(f"App is not allowed for UI bridge: {app}")
        return app

    def _confirm_foreground(self, app: str) -> Optional[ForegroundWindow]:
        if not self.cfg.require_foreground_confirmation:
            return None
        window = self.foreground_probe()
        if window is None:
            raise PermissionError(f"cannot confirm that {app} is the foreground window; the UI bridge acts only in the visible target app")
        if not window_matches_app(window, app):
            raise PermissionError(f"{app} is not the foreground window (found: {window.describe()})")
        if looks_like_sign_in(window):
            raise PermissionError(f"{app} shows a sign-in screen; the UI bridge acts only in a signed-in session and never signs in")
        return window

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

    @permission_guarded
    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self._require_enabled()
        app = self._check_app(str(args.get("app", "")))
        if action not in _ACTIONS:
            raise ValueError(f"Unsupported ui_bridge action: {action}")
        window = self._confirm_foreground(app)
        # The approver sees which window will receive the action (not bound: titles change).
        seen = f" (foreground: {window.describe()})" if window else ""
        if action == "observe":
            expected = str(args.get("expected_result") or f"Observe visible {app} UI before any action")
            ctx.permissions.verify(proposal("ui_bridge", "observe", {"app": app, "expected_result": expected}, "medium", "通过可见 UI Bridge 观察屏幕" + seen, ctx))
            self._confirm_foreground(app)  # still the target after the approval wait
            return await self.tools.call("computer", "screenshot", {"max_width": int(args.get("max_width", 960)), "expected_result": expected, "skip_if_unchanged": True, "skip_if_too_soon": True}, ctx)
        if action == "type_visible_reply":
            text = str(args["text"])
            expected = str(args.get("expected_result") or f"Type visible reply in {app}")
            approved: dict[str, Any] = {"app": app, **bound_content("text", text), "expected_result": expected}
            ctx.permissions.verify(proposal("ui_bridge", "type_visible_reply", approved, "high", "通过可见 UI Bridge 输入文字" + seen, ctx))
            self._confirm_foreground(app)
            return await self.tools.call("computer", "type_text", {"text": text, "expected_result": expected}, ctx)
        if action == "press_send":
            expected = str(args.get("expected_result") or f"Send currently visible composed message in {app}")
            ctx.permissions.verify(proposal("ui_bridge", "press_send", {"app": app, "expected_result": expected}, "high", "通过可见 UI Bridge 触发发送动作" + seen, ctx))
            self._confirm_foreground(app)
            return await self.tools.call("computer", "hotkey", {"keys": ["enter"], "expected_result": expected}, ctx)
        expected = str(args.get("expected_result") or f"Click visible UI element in {app}")
        ctx.permissions.verify(proposal("ui_bridge", "click", {"app": app, "x": args.get("x"), "y": args.get("y"), "expected_result": expected}, "high", "通过可见 UI Bridge 点击" + seen, ctx))
        self._confirm_foreground(app)
        return await self.tools.call("computer", "click", {"x": int(args["x"]), "y": int(args["y"]), "expected_result": expected}, ctx)
