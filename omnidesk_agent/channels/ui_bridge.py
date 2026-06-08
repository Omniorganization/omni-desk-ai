from __future__ import annotations

from omnidesk_agent.config import UIBridgeConfig
from omnidesk_agent.core.models import ChannelMessage, ToolResult
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.tools.base import ToolContext


class VisibleUIBridge:
    """Visible, permissioned bridge for already-open desktop apps.

    It deliberately does not implement hidden login, cookie extraction, message scraping,
    password storage, anti-detection, or API bypass. The operator sees the target app and
    approves every click/type/send action through PermissionManager.
    """

    name = "ui_bridge"

    def __init__(self, cfg: UIBridgeConfig, tools: ToolRegistry):
        self.cfg = cfg
        self.tools = tools

    async def observe_screen(self, ctx: ToolContext) -> ToolResult:
        return await self.tools.call("computer", "screenshot", {"max_width": 1280}, ctx)

    async def type_visible_reply(self, text: str, ctx: ToolContext) -> ToolResult:
        return await self.tools.call("computer", "type_text", {"text": text}, ctx)

    async def press_send(self, ctx: ToolContext) -> ToolResult:
        return await self.tools.call("computer", "hotkey", {"keys": ["enter"]}, ctx)
