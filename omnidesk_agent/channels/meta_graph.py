from __future__ import annotations

import os
import httpx
from typing import Any

from omnidesk_agent.config import MetaGraphConfig
from omnidesk_agent.core.models import ChannelMessage


class MetaGraphChannel:
    """Facebook Page / Instagram professional messaging adapter through Meta Graph API.

    Use this for assets your app is authorized to manage. Personal profile automation should
    be handled only by visible UI Bridge with human approval.
    """

    name = "meta_graph"
    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        try:
            event = payload.get("entry", [{}])[0].get("messaging", [{}])[0]
            sender = str((event.get("sender") or {}).get("id") or "unknown")
            msg = event.get("message") or event.get("postback") or {}
            mid = str(msg.get("mid") or event.get("timestamp") or "")
            ts = float(event.get("timestamp")) / 1000.0 if event.get("timestamp") else None
            return WebhookEnvelope(source_key=sender, sender_id=sender, message_id=mid or None, timestamp=ts, raw=payload)
        except Exception:
            return WebhookEnvelope(raw=payload)

    def __init__(self, cfg: MetaGraphConfig):
        self.cfg = cfg
        self.token = os.getenv(cfg.page_access_token_env, "")

    def parse_webhook(self, payload: dict[str, Any]) -> list[ChannelMessage]:
        out: list[ChannelMessage] = []
        for entry in payload.get("entry", []):
            for messaging in entry.get("messaging", []) or []:
                sender = str(messaging.get("sender", {}).get("id", ""))
                if self.cfg.allowed_psids and sender not in self.cfg.allowed_psids:
                    continue
                text = messaging.get("message", {}).get("text")
                if text:
                    out.append(ChannelMessage(channel=self.name, sender_id=sender, thread_id=sender, text=text, raw=messaging))
        return out

    async def send_page_text(self, recipient_psid: str, text: str) -> None:
        if not self.token:
            raise RuntimeError("Meta page access token is missing")
        url = f"https://graph.facebook.com/{self.cfg.graph_version}/me/messages"
        body = {"recipient": {"id": recipient_psid}, "message": {"text": text}, "messaging_type": "RESPONSE"}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, params={"access_token": self.token}, json=body)
            r.raise_for_status()

    async def send_instagram_text(self, ig_scoped_user_id: str, text: str) -> None:
        if not self.cfg.instagram_account_id:
            raise RuntimeError("instagram_account_id is missing")
        if not self.token:
            raise RuntimeError("Meta page access token is missing")
        url = f"https://graph.facebook.com/{self.cfg.graph_version}/{self.cfg.instagram_account_id}/messages"
        body = {"recipient": {"id": ig_scoped_user_id}, "message": {"text": text}}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, params={"access_token": self.token}, json=body)
            r.raise_for_status()
