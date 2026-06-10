from __future__ import annotations
import base64, email.message
from typing import Any
from omnidesk_agent.config import GmailConfig
from omnidesk_agent.core.models import ChannelMessage

class GmailChannel:
    name = "gmail"
    scopes = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.modify"]
    def __init__(self, cfg: GmailConfig):
        self.cfg = cfg
    def configured(self) -> bool:
        return self.cfg.credentials_file.exists() or self.cfg.token_file.exists()
    def parse_message_summary(self, message: dict[str, Any]) -> ChannelMessage | None:
        payload = message.get("payload", {})
        headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
        sender = headers.get("from", "")
        if self.cfg.allowed_senders and not any(s in sender for s in self.cfg.allowed_senders):
            return None
        subject = headers.get("subject", "")
        snippet = message.get("snippet", "")
        return ChannelMessage(channel=self.name, sender_id=sender, thread_id=str(message.get("threadId", "")), message_id=str(message.get("id", "")), text=f"Subject: {subject}\n\n{snippet}", raw=message)
    def build_raw_email(self, to: str, subject: str, body: str) -> dict[str, str]:
        msg = email.message.EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        return {"raw": raw}
