from __future__ import annotations
from typing import Any
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal

class ChannelSendTool:
    name = "channels"
    def __init__(self, adapters: dict[str, Any]):
        self.adapters = adapters

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action == "send_text":
            channel = str(args["channel"])
            recipient = str(args["recipient"])
            text = str(args["text"])
            expected = str(args.get("expected_result") or f"Send text to {recipient} via {channel}")
            decision = ctx.permissions.verify(proposal("channels", "send_text", {"channel": channel, "recipient": recipient, "text_preview": text[:200], "length": len(text), "expected_result": expected}, "high", "发送外部渠道消息前必须确认", ctx))
            if decision.mode == "dry_run":
                return ToolResult(False, summary=f"dry-run: send {len(text)} chars via {channel}")
            adapter = self.adapters.get(channel)
            if not adapter:
                return ToolResult(False, error=f"Unknown channel adapter: {channel}")
            if not hasattr(adapter, "send_text"):
                return ToolResult(False, error=f"Channel {channel} does not support send_text")
            await adapter.send_text(recipient, text, **args.get("options", {}))
            return ToolResult(True, summary=f"sent text via {channel} to {recipient}")
        if action == "send_email":
            adapter = self.adapters.get("gmail")
            if not adapter:
                return ToolResult(False, error="Gmail adapter not configured")
            to = str(args["to"]); subject = str(args.get("subject", "")); body = str(args.get("body", ""))
            decision = ctx.permissions.verify(proposal("channels", "send_email", {"to": to, "subject": subject, "body_preview": body[:200], "length": len(body)}, "high", "发送邮件前必须确认", ctx))
            if decision.mode == "dry_run":
                return ToolResult(False, summary=f"dry-run: email to {to}")
            return ToolResult(True, data=adapter.build_raw_email(to, subject, body), summary=f"built Gmail raw payload for {to}; execute send with configured Gmail client")
        raise ValueError(f"Unsupported channels action: {action}")
