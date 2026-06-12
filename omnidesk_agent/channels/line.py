from __future__ import annotations
import base64
import hashlib
import hmac
import os
from typing import Any
from omnidesk_agent.config import LineConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.channels.http_client import ChannelHttpClient

class LineChannel:
    name = "line"
    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        event = (payload.get("events") or [{}])[0]
        source = event.get("source") or {}
        sender = str(source.get("userId") or source.get("groupId") or source.get("roomId") or "unknown")
        msg = event.get("message") or {}
        mid = str(msg.get("id") or event.get("webhookEventId") or "")
        ts = float(event.get("timestamp")) / 1000.0 if event.get("timestamp") else None
        return WebhookEnvelope(source_key=sender, sender_id=sender, message_id=mid or None, timestamp=ts, raw=payload)
    def __init__(self, cfg: LineConfig):
        self.cfg = cfg
        self.token = os.getenv(cfg.channel_access_token_env, "")
        self.secret = os.getenv(cfg.channel_secret_env, "")
        self.http = ChannelHttpClient()

    def verify_signature(self, body: bytes, signature: str) -> bool:
        if not self.secret:
            return False
        digest = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode("ascii")
        return hmac.compare_digest(expected, signature)


    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload) -> None:
        from omnidesk_agent.channels.verify import header
        if not self.verify_signature(body, header(headers, "x-line-signature")):
            raise PermissionError("invalid LINE webhook signature")

    def parse_webhook(self, payload: dict[str, Any]) -> list[ChannelMessage]:
        out = []
        for event in payload.get("events", []):
            source = event.get("source", {})
            user_id = str(source.get("userId") or source.get("groupId") or source.get("roomId") or "")
            if self.cfg.allowed_user_ids and user_id not in self.cfg.allowed_user_ids:
                continue
            message = event.get("message", {})
            text = message.get("text", "")
            if text:
                out.append(ChannelMessage(channel=self.name, sender_id=user_id, thread_id=user_id, message_id=str(message.get("id") or ""), text=text, raw=event))
        return out

    async def send_text(self, recipient: str, text: str, **kwargs) -> None:
        if not self.token:
            raise RuntimeError("LINE channel access token is missing")
        await self.http.post("https://api.line.me/v2/bot/message/push", headers={"Authorization": f"Bearer {self.token}"}, json={"to": recipient, "messages": [{"type": "text", "text": text}]})
