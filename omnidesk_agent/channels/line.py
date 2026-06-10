from __future__ import annotations
import base64, hashlib, hmac, os
from typing import Any
import httpx
from omnidesk_agent.config import LineConfig
from omnidesk_agent.core.models import ChannelMessage

class LineChannel:
    name = "line"
    def __init__(self, cfg: LineConfig):
        self.cfg = cfg
        self.token = os.getenv(cfg.channel_access_token_env, "")
        self.secret = os.getenv(cfg.channel_secret_env, "")

    def verify_signature(self, body: bytes, signature: str) -> bool:
        if not self.secret:
            return False
        digest = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode("ascii")
        return hmac.compare_digest(expected, signature)

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
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post("https://api.line.me/v2/bot/message/push", headers={"Authorization": f"Bearer {self.token}"}, json={"to": recipient, "messages": [{"type": "text", "text": text}]})
            r.raise_for_status()
