from __future__ import annotations
from typing import Any
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal, permission_guarded
from omnidesk_agent.tools.spec import ActionSpec, ToolSpec, obj_schema

# Caller-supplied send_text options are forwarded to adapter.send_text(**options),
# so only keys that cannot change the destination host, credentials or request
# shape are accepted. Deliberately excluded: service_url (Teams would send its
# bot bearer token there), webhook_url (Google Chat), any other *_url, token,
# headers, receive_id_type (interpolated into the Lark query string),
# idempotency_key (Matrix uses it as a URL path segment; the dispatcher owns it)
# and target (would turn an X send into a public post).
_MAX_OPTION_CHARS = 128
_META_SURFACES = ("facebook", "page", "messenger", "instagram", "ig")
SEND_TEXT_OPTION_SCHEMA: dict[str, dict[str, Any]] = {
    "silent": {"type": "boolean", "description": "Best-effort silent delivery hint"},
    "thread_ts": {"type": "string", "maxLength": _MAX_OPTION_CHARS, "description": "Slack thread timestamp to reply in"},
    "surface": {"type": "string", "enum": list(_META_SURFACES), "description": "Meta Graph surface"},
}


def validate_send_text_options(options: Any) -> dict[str, Any]:
    """Return a copy of send_text options or raise ValueError on any unsafe key/value.

    Messages start with "unsupported" so the outbound dispatcher classifies a
    rejected queued row as non-retryable.
    """
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ValueError("unsupported channels.send_text options: expected an object")
    unknown = sorted(str(key) for key in options if key not in SEND_TEXT_OPTION_SCHEMA)
    if unknown:
        allowed = ", ".join(sorted(SEND_TEXT_OPTION_SCHEMA))
        raise ValueError(f"unsupported channels.send_text option(s): {', '.join(unknown)}; allowed options: {allowed}")
    clean: dict[str, Any] = {}
    for key, value in options.items():
        schema = SEND_TEXT_OPTION_SCHEMA[key]
        if schema["type"] == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"unsupported channels.send_text option value: {key} must be a boolean")
        else:
            if not isinstance(value, str) or len(value) > _MAX_OPTION_CHARS:
                raise ValueError(f"unsupported channels.send_text option value: {key} must be a string of at most {_MAX_OPTION_CHARS} characters")
            if "enum" in schema and value.lower() not in schema["enum"]:
                raise ValueError(f"unsupported channels.send_text option value: {key} must be one of {', '.join(schema['enum'])}")
        clean[key] = value
    return clean


class ChannelSendTool:
    name = "channels"
    def __init__(self, adapters: dict[str, Any], outbound_store: Any = None):
        self.adapters = adapters
        self.outbound_store = outbound_store

    def spec(self) -> ToolSpec:
        options_schema = obj_schema(SEND_TEXT_OPTION_SCHEMA, additional=False)
        return ToolSpec(
            name=self.name,
            description="Send messages through configured external channel adapters.",
            permissions=["channels.send_text", "channels.send_email"],
            actions={
                "send_text": ActionSpec(
                    "send_text",
                    "Send a text message to a recipient on a configured channel",
                    obj_schema(
                        {
                            "channel": {"type": "string"},
                            "recipient": {"type": "string"},
                            "text": {"type": "string"},
                            "options": options_schema,
                        },
                        required=["channel", "recipient", "text"],
                    ),
                    risk="high",
                    side_effect=True,
                    requires_approval=True,
                ),
                "send_email": ActionSpec(
                    "send_email",
                    "Build a Gmail raw email payload for an approved send",
                    obj_schema(
                        {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
                        required=["to"],
                    ),
                    risk="high",
                    side_effect=True,
                    requires_approval=True,
                ),
            },
        )

    @staticmethod
    def _require_channel_enabled(adapter: Any, channel: str) -> None:
        cfg = getattr(adapter, "cfg", None)
        if cfg is not None and not bool(getattr(cfg, "enabled", False)):
            raise PermissionError(f"Channel {channel} is disabled by configuration")

    @permission_guarded
    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action == "send_text":
            channel = str(args["channel"])
            recipient = str(args["recipient"])
            text = str(args["text"])
            try:
                options = validate_send_text_options(args.get("options"))
            except ValueError as exc:
                return ToolResult(False, error=str(exc), summary="channels.send_text options rejected")
            expected = str(args.get("expected_result") or f"Send text to {recipient} via {channel}")
            # options are part of the approval payload (and therefore the scope hash).
            decision = ctx.permissions.verify(proposal("channels", "send_text", {"channel": channel, "recipient": recipient, "text_preview": text[:200], "length": len(text), "options": options, "expected_result": expected}, "high", "发送外部渠道消息前必须确认", ctx))
            if decision.mode == "dry_run":
                return ToolResult(False, summary=f"dry-run: send {len(text)} chars via {channel}")
            adapter = self.adapters.get(channel)
            if not adapter:
                return ToolResult(False, error=f"Unknown channel adapter: {channel}")
            if not hasattr(adapter, "send_text"):
                return ToolResult(False, error=f"Channel {channel} does not support send_text")
            self._require_channel_enabled(adapter, channel)
            if self.outbound_store:
                outbound_id = self.outbound_store.create(channel=channel, recipient=recipient, payload={"type": "text", "text": text, "options": options})
                return ToolResult(True, data={"outbound_id": outbound_id, "status": "pending"}, summary=f"queued text via {channel} to {recipient}")
            await adapter.send_text(recipient, text, **options)
            return ToolResult(True, data={"outbound_id": None, "status": "sent"}, summary=f"sent text via {channel} to {recipient}")
        if action == "send_email":
            adapter = self.adapters.get("gmail")
            if not adapter:
                return ToolResult(False, error="Gmail adapter not configured")
            self._require_channel_enabled(adapter, "gmail")
            cfg = getattr(adapter, "cfg", None)
            if cfg is not None and (getattr(cfg, "readonly", True) or not getattr(cfg, "allow_send", False)):
                raise PermissionError("Gmail send is disabled by configuration")
            to = str(args["to"])
            subject = str(args.get("subject", ""))
            body = str(args.get("body", ""))
            decision = ctx.permissions.verify(proposal("channels", "send_email", {"to": to, "subject": subject, "body_preview": body[:200], "length": len(body)}, "high", "发送邮件前必须确认", ctx))
            if decision.mode == "dry_run":
                return ToolResult(False, summary=f"dry-run: email to {to}")
            return ToolResult(True, data=adapter.build_raw_email(to, subject, body), summary=f"built Gmail raw payload for {to}; execute send with configured Gmail client")
        raise ValueError(f"Unsupported channels action: {action}")
