from __future__ import annotations
import base64, hashlib, hmac, os
from typing import Any
import httpx
from omnidesk_agent.config import XConfig
from omnidesk_agent.core.models import ChannelMessage

class XChannel:
    name = "x"
    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        event = (payload.get("direct_message_events") or [{}])[0]
        sender = str((event.get("message_create") or {}).get("sender_id") or "unknown")
        mid = str(event.get("id") or "")
        return WebhookEnvelope(source_key=sender, sender_id=sender, message_id=mid or None, raw=payload)
    def __init__(self, cfg: XConfig):
        self.cfg = cfg
        self.bearer_token = os.getenv(cfg.bearer_token_env, "")
        self.crc_token = os.getenv(cfg.webhook_crc_token_env, "")

    def crc_response(self, crc_token: str) -> dict[str, str]:
        if not self.crc_token:
            raise RuntimeError("X webhook CRC secret is missing")
        digest = hmac.new(self.crc_token.encode("utf-8"), crc_token.encode("utf-8"), hashlib.sha256).digest()
        return {"response_token": "sha256=" + base64.b64encode(digest).decode("ascii")}

    def parse_webhook(self, payload: dict[str, Any]) -> list[ChannelMessage]:
        out = []
        for event in payload.get("direct_message_events", []) or []:
            sender = str(event.get("message_create", {}).get("sender_id", ""))
            if self.cfg.allowed_user_ids and sender not in self.cfg.allowed_user_ids:
                continue
            text = event.get("message_create", {}).get("message_data", {}).get("text", "")
            if text:
                out.append(ChannelMessage(channel=self.name, sender_id=sender, thread_id=sender, message_id=str(event.get("id") or ""), text=text, raw=event))
        return out

    async def post_text(self, text: str) -> None:
        if not self.bearer_token:
            raise RuntimeError("X bearer token is missing")
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post("https://api.x.com/2/tweets", headers={"Authorization": f"Bearer {self.bearer_token}"}, json={"text": text})
            r.raise_for_status()
