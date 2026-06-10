from __future__ import annotations

import base64
import email.message
from typing import Any, Optional

from omnidesk_agent.config import GmailConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.oauth.gmail_oauth import GmailOAuthManager


class GmailChannel:
    """Gmail API adapter.

    This uses official Google OAuth credentials. It does not scrape browser cookies or sessions.
    """

    name = "gmail"
    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        sender = str(payload.get("emailAddress") or payload.get("from") or "unknown")
        mid = str(payload.get("messageId") or payload.get("historyId") or payload.get("id") or "")
        return WebhookEnvelope(source_key=sender, sender_id=sender, message_id=mid or None, raw=payload)

    def __init__(self, cfg: GmailConfig):
        self.cfg = cfg
        self.oauth = GmailOAuthManager(cfg)

    def configured(self) -> bool:
        return self.cfg.credentials_file.exists() or self.cfg.token_file.exists()

    def authenticated(self) -> bool:
        return self.cfg.token_file.exists()

    def parse_message_summary(self, message: dict[str, Any]) -> Optional[ChannelMessage]:
        payload = message.get("payload", {})
        headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
        sender = headers.get("from", "")
        if self.cfg.allowed_senders and not any(s in sender for s in self.cfg.allowed_senders):
            return None
        subject = headers.get("subject", "")
        snippet = message.get("snippet", "")
        return ChannelMessage(
            channel=self.name,
            sender_id=sender,
            thread_id=str(message.get("threadId", "")),
            message_id=str(message.get("id", "")),
            text=f"Subject: {subject}\n\n{snippet}",
            raw=message,
        )

    def build_raw_email(self, to: str, subject: str, body: str) -> dict[str, str]:
        msg = email.message.EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        return {"raw": raw}

    async def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        service = self.oauth.build_service()
        raw = self.build_raw_email(to, subject, body)
        return service.users().messages().send(userId="me", body=raw).execute()

    async def list_messages(self, query: str = "", max_results: int = 10) -> list[dict[str, Any]]:
        service = self.oauth.build_service()
        resp = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        return resp.get("messages", [])
