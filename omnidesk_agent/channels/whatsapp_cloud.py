from __future__ import annotations

import os
from typing import Any

from omnidesk_agent.config import WhatsAppCloudConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.channels.http_client import ChannelHttpClient

class WhatsAppCloudChannel:
    """WhatsApp Business Platform Cloud API adapter.

    This is for business numbers and official Cloud API. It is not a personal WhatsApp
    reverse-engineering client.
    """

    name = "whatsapp_cloud"
    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        try:
            value = payload.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
            msg = (value.get("messages") or [{}])[0]
            sender = str(msg.get("from") or "unknown")
            mid = str(msg.get("id") or "")
            ts = float(msg.get("timestamp")) if msg.get("timestamp") else None
            return WebhookEnvelope(source_key=sender, sender_id=sender, message_id=mid or None, timestamp=ts, raw=payload)
        except Exception:
            return WebhookEnvelope(raw=payload)

    def __init__(self, cfg: WhatsAppCloudConfig):
        self.cfg = cfg
        self.token = os.getenv(cfg.access_token_env, "")
        self.http = ChannelHttpClient()


    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload) -> None:
        from omnidesk_agent.channels.verify import env_secret, header, verify_hmac_sha256
        verify_hmac_sha256(body, env_secret(self.cfg.app_secret_env, channel=self.name), header(headers, "x-hub-signature-256"), prefix="sha256=")

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
        await self.http.post(url, headers={"Authorization": f"Bearer {self.token}"}, json=body)
