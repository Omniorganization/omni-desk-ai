from __future__ import annotations
from typing import Any
from omnidesk_agent.channels.gmail import GmailChannel
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal

class GmailTool:
    name = "gmail"
    def __init__(self, adapter: GmailChannel):
        self.adapter = adapter
    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action == "build_raw_email":
            to = str(args["to"]); subject = str(args.get("subject", "")); body = str(args.get("body", ""))
            ctx.permissions.verify(proposal("gmail", "build_raw_email", {"to": to, "subject": subject, "body_preview": body[:200], "length": len(body)}, "medium", "生成 Gmail API raw email 载荷", ctx))
            return ToolResult(True, data=self.adapter.build_raw_email(to, subject, body), summary=f"built raw email to {to}")
        if action == "configured":
            return ToolResult(True, data={"configured": self.adapter.configured()}, summary="checked gmail configuration")
        raise ValueError(f"Unsupported gmail action: {action}")
