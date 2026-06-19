from __future__ import annotations

import os
from typing import Any

from omnidesk_agent.config import MetaGraphConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.channels.http_client import ChannelHttpClient

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
        self.http = ChannelHttpClient()


    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload) -> None:
        from omnidesk_agent.channels.verify import env_secret, header, verify_hmac_sha256
        verify_hmac_sha256(body, env_secret(self.cfg.app_secret_env, channel=self.name), header(headers, "x-hub-signature-256"), prefix="sha256=")

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

    async def send_page_text(self, recipient_psid: str, text: str, **kwargs) -> None:
        if not self.token:
            raise RuntimeError("Meta page access token is missing")
        url = f"https://graph.facebook.com/{self.cfg.graph_version}/me/messages"
        body = {"recipient": {"id": recipient_psid}, "message": {"text": text}, "messaging_type": "RESPONSE"}
        return await self.http.post(url, params={"access_token": self.token}, json=body, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)

    async def send_text(self, recipient: str, text: str, **kwargs) -> None:
        surface = str(kwargs.get("surface") or kwargs.get("target") or "facebook").lower()
        if surface in {"instagram", "ig"}:
            return await self.send_instagram_text(recipient, text, idempotency_key=kwargs.get("idempotency_key"))
        if surface in {"facebook", "page", "messenger"}:
            return await self.send_page_text(recipient, text, idempotency_key=kwargs.get("idempotency_key"))
        raise ValueError(f"Unsupported Meta send_text surface: {surface}")

    async def send_instagram_text(self, ig_scoped_user_id: str, text: str, **kwargs) -> None:
        if not self.cfg.instagram_account_id:
            raise RuntimeError("instagram_account_id is missing")
        if not self.token:
            raise RuntimeError("Meta page access token is missing")
        url = f"https://graph.facebook.com/{self.cfg.graph_version}/{self.cfg.instagram_account_id}/messages"
        body = {"recipient": {"id": ig_scoped_user_id}, "message": {"text": text}}
        return await self.http.post(url, params={"access_token": self.token}, json=body, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
