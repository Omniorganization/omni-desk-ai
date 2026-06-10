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
        if action == "configured":
            return ToolResult(True, data={"configured": self.adapter.configured(), "authenticated": self.adapter.authenticated()}, summary="checked gmail configuration")

        if action == "auth_local":
            ctx.permissions.verify(proposal("gmail", "auth_local", {}, "high", "启动 Gmail OAuth 本地授权流程", ctx))
            token = self.adapter.oauth.run_local_flow(port=int(args.get("port", 0)))
            return ToolResult(True, data={"token_saved": True, "keys": sorted(token.keys())}, summary="gmail oauth token saved")

        if action == "auth_url":
            redirect_uri = str(args["redirect_uri"])
            state = str(args.get("state", "omnidesk-gmail"))
            result = self.adapter.oauth.build_authorization_url(redirect_uri, state)
            return ToolResult(True, data=result, summary="built gmail oauth authorization url")

        if action == "auth_callback":
            code = str(args["code"])
            redirect_uri = str(args["redirect_uri"])
            token = self.adapter.oauth.exchange_code(code, redirect_uri, args.get("state"))
            return ToolResult(True, data={"token_saved": True, "keys": sorted(token.keys())}, summary="exchanged gmail oauth code")

        if action == "build_raw_email":
            to = str(args["to"])
            subject = str(args.get("subject", ""))
            body = str(args.get("body", ""))
            ctx.permissions.verify(proposal("gmail", "build_raw_email", {"to": to, "subject": subject, "body_preview": body[:200], "length": len(body)}, "medium", "生成 Gmail API raw email 载荷", ctx))
            return ToolResult(True, data=self.adapter.build_raw_email(to, subject, body), summary=f"built raw email to {to}")

        if action == "send_email":
            to = str(args["to"])
            subject = str(args.get("subject", ""))
            body = str(args.get("body", ""))
            ctx.permissions.verify(proposal("gmail", "send_email", {"to": to, "subject": subject, "body_preview": body[:200], "length": len(body)}, "high", "通过 Gmail API 发送邮件", ctx))
            result = await self.adapter.send_email(to, subject, body)
            return ToolResult(True, data=result, summary=f"sent gmail email to {to}")

        raise ValueError(f"Unsupported gmail action: {action}")
