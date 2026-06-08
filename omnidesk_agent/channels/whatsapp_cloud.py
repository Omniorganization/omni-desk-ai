from __future__ import annotations

import os
import httpx
from typing import Any

from omnidesk_agent.config import WhatsAppCloudConfig
from omnidesk_agent.core.models import ChannelMessage


class WhatsAppCloudChannel:
    """WhatsApp Business Platform Cloud API adapter.

    This is for business numbers and official Cloud API. It is not a personal WhatsApp
    reverse-engineering client.
    """

    name = "whatsapp_cloud"

    def __init__(self, cfg: WhatsAppCloudConfig):
        self.cfg = cfg
        self.token = os.getenv(cfg.access_token_env, "")

    def parse_webhook(self, payload: dict[str, Any]) -> list[ChannelMessage]:
        out: list[ChannelMessage] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []) or []:
                    wa_id = str(msg.get("from", ""))
                    if self.cfg.allowed_wa_ids and wa_id not in self.cfg.allowed_wa_ids:
                        continue
                    text = msg.get("text", {}).get("body") or ""
                    if text:
                        out.append(ChannelMessage(
                            channel=self.name,
                            sender_id=wa_id,
                            thread_id=wa_id,
                            message_id=msg.get("id"),
                            text=text,
                            raw=msg,
                        ))
        return out

    async def send_text(self, recipient: str, text: str, **kwargs) -> None:
        if not self.cfg.phone_number_id:
            raise RuntimeError("WhatsApp phone_number_id is missing")
        if not self.token:
            raise RuntimeError("WhatsApp access token is missing")
        url = f"https://graph.facebook.com/{self.cfg.graph_version}/{self.cfg.phone_number_id}/messages"
        body = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"body": text},
        }
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {self.token}"}, json=body)
            r.raise_for_status()
