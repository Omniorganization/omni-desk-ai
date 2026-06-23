from __future__ import annotations

from typing import Any

from omnidesk_agent.channels.gmail import GmailChannel
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal


class GmailTool:
    name = "gmail"

    def __init__(self, adapter: GmailChannel):
        self.adapter = adapter

    def _require_enabled(self) -> None:
        cfg = getattr(self.adapter, "cfg", None)
        if cfg is not None and not getattr(cfg, "enabled", False):
            raise PermissionError("Gmail tool is disabled by configuration")

    def _require_compose_allowed(self) -> None:
        self._require_enabled()
        cfg = getattr(self.adapter, "cfg", None)
        if cfg is not None and not getattr(cfg, "allow_compose", False):
            raise PermissionError("Gmail compose is disabled by configuration")

    def _require_send_allowed(self) -> None:
        self._require_enabled()
        cfg = getattr(self.adapter, "cfg", None)
        if cfg is not None and (getattr(cfg, "readonly", True) or not getattr(cfg, "allow_send", False)):
            raise PermissionError("Gmail send is disabled by configuration")


    def spec(self):
        from omnidesk_agent.tools.spec import ActionSpec, ToolSpec
        return ToolSpec(
            name=self.name,
            description="Gmail OAuth and Gmail API tool.",
            permissions=["gmail.read", "gmail.compose", "gmail.send"],
            actions={
                "configured": ActionSpec("configured", "Check Gmail configuration", {}, risk="low", side_effect=False, requires_approval=False),
                "auth_local": ActionSpec("auth_local", "Start local OAuth flow", {}, risk="high", side_effect=True, requires_approval=True),
                "auth_url": ActionSpec("auth_url", "Build OAuth authorization URL", {"redirect_uri": "string"}, risk="medium", side_effect=False, requires_approval=False),
                "auth_callback": ActionSpec("auth_callback", "Exchange OAuth callback code", {"code": "string", "redirect_uri": "string", "state": "string"}, risk="high", side_effect=True, requires_approval=True),
                "build_raw_email": ActionSpec("build_raw_email", "Build raw email payload", {"to": "string", "subject": "string", "body": "string"}, risk="medium", side_effect=False, requires_approval=True),
                "send_email": ActionSpec("send_email", "Send Gmail email", {"to": "string", "subject": "string", "body": "string"}, risk="high", side_effect=True, requires_approval=True),
            },
        )


    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action == "configured":
            return ToolResult(True, data={"configured": self.adapter.configured(), "authenticated": self.adapter.authenticated()}, summary="checked gmail configuration")

        if action == "auth_local":
            self._require_enabled()
            ctx.permissions.verify(proposal("gmail", "auth_local", {}, "high", "启动 Gmail OAuth 本地授权流程", ctx))
            token = self.adapter.oauth.run_local_flow(port=int(args.get("port", 0)))
            return ToolResult(True, data={"token_saved": True, "keys": sorted(token.keys())}, summary="gmail oauth token saved")

        if action == "auth_url":
            self._require_enabled()
            redirect_uri = str(args["redirect_uri"])
            state = str(args.get("state", "omnidesk-gmail"))
            result = self.adapter.oauth.build_authorization_url(redirect_uri, state, actor=ctx.actor)
            return ToolResult(True, data=result, summary="built gmail oauth authorization url")

        if action == "auth_callback":
            self._require_enabled()
            code = str(args["code"])
            redirect_uri = str(args["redirect_uri"])
            token = self.adapter.oauth.exchange_code(code, redirect_uri, args.get("state"), actor=ctx.actor)
            return ToolResult(True, data={"token_saved": True, "keys": sorted(token.keys())}, summary="exchanged gmail oauth code")

        if action == "build_raw_email":
            self._require_compose_allowed()
            to = str(args["to"])
            subject = str(args.get("subject", ""))
            body = str(args.get("body", ""))
            ctx.permissions.verify(proposal("gmail", "build_raw_email", {"to": to, "subject": subject, "body_preview": body[:200], "length": len(body)}, "medium", "生成 Gmail API raw email 载荷", ctx))
            return ToolResult(True, data=self.adapter.build_raw_email(to, subject, body), summary=f"built raw email to {to}")

        if action == "send_email":
            self._require_send_allowed()
            to = str(args["to"])
            subject = str(args.get("subject", ""))
            body = str(args.get("body", ""))
            ctx.permissions.verify(proposal("gmail", "send_email", {"to": to, "subject": subject, "body_preview": body[:200], "length": len(body)}, "high", "通过 Gmail API 发送邮件", ctx))
            result = await self.adapter.send_email(to, subject, body)
            return ToolResult(True, data=result, summary=f"sent gmail email to {to}")

        raise ValueError(f"Unsupported gmail action: {action}")
