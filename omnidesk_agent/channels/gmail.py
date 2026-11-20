from __future__ import annotations

import asyncio
import base64
import binascii
import email.message
import email.utils
import json
import os
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.config import GmailConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.oauth.gmail_oauth import GmailOAuthManager

# Bounds one push notification's catch-up so a stale history id cannot make
# a single request page through a whole mailbox.
MAX_HISTORY_PAGES = 5


def sender_address(from_header: str) -> str:
    """The bare, lower-cased address of a From header ("Name <a@b>" -> "a@b")."""
    return email.utils.parseaddr(from_header or "")[1].strip().lower()


class GmailChannel:
    """Gmail API adapter.

    This uses official Google OAuth credentials. It does not scrape browser
    cookies or sessions. Inbound mail arrives through Gmail push notifications
    (``users.watch`` → Cloud Pub/Sub → ``POST /webhooks/gmail``); each
    notification only carries a history id, so the new messages are read with
    ``users.history.list`` from the last processed history id.
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
        self.history_state_file = Path(cfg.token_file).expanduser().with_name("gmail_history_state.json")

    def configured(self) -> bool:
        return self.cfg.credentials_file.exists() or self.cfg.token_file.exists()

    def authenticated(self) -> bool:
        return self.cfg.token_file.exists()

    def sender_allowed(self, from_header: str) -> bool:
        # Exact address match: a substring test let a display name such as
        # "boss@company.com <attacker@evil.test>" pass for boss@company.com.
        if not self.cfg.allowed_senders:
            return True
        address = sender_address(from_header)
        return bool(address) and address in {str(item).strip().lower() for item in self.cfg.allowed_senders}

    def parse_message_summary(self, message: dict[str, Any]) -> Optional[ChannelMessage]:
        payload = message.get("payload", {})
        headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
        sender = headers.get("from", "")
        if not self.sender_allowed(sender):
            return None
        subject = headers.get("subject", "")
        snippet = message.get("snippet", "")
        return ChannelMessage(
            channel=self.name,
            sender_id=sender_address(sender) or sender,
            thread_id=str(message.get("threadId", "")),
            message_id=str(message.get("id", "")),
            text=f"Subject: {subject}\n\n{snippet}",
            raw=message,
        )

    # ------------------------------------------------------------------ push notifications

    @staticmethod
    def parse_push(payload: Any) -> tuple[str, int, str]:
        """(emailAddress, historyId, Pub/Sub messageId) of a Gmail push notification."""
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            raise ValueError("Pub/Sub push body has no message")
        message_id = str(message.get("messageId") or message.get("message_id") or "")
        try:
            data = json.loads(base64.b64decode(str(message.get("data") or ""), validate=True))
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Gmail notification data is not base64 JSON") from exc
        if not isinstance(data, dict) or not message_id:
            raise ValueError("Gmail notification is missing fields")
        address = str(data.get("emailAddress") or "").strip().lower()
        history_id = data.get("historyId")
        if not address or not str(history_id or "").isdigit():
            raise ValueError("Gmail notification is missing emailAddress or historyId")
        return address, int(history_id), message_id

    def load_history_id(self) -> Optional[int]:
        try:
            value = json.loads(self.history_state_file.read_text(encoding="utf-8")).get("history_id")
        except (OSError, ValueError, AttributeError):
            return None
        return int(value) if str(value or "").isdigit() else None

    def save_history_id(self, history_id: int) -> None:
        self.history_state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.history_state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"history_id": int(history_id)}), encoding="utf-8")
        os.replace(tmp, self.history_state_file)

    def _fetch_new_messages_sync(self, start_history_id: int) -> tuple[list[ChannelMessage], Optional[int]]:
        service = self.oauth.build_service()
        users = service.users()
        page_token = None
        newest: Optional[int] = None
        message_ids: list[str] = []
        for _page in range(MAX_HISTORY_PAGES):
            request = users.history().list(userId="me", startHistoryId=str(start_history_id), historyTypes=["messageAdded"], labelId="INBOX", pageToken=page_token)
            response = request.execute()
            newest = int(response["historyId"]) if str(response.get("historyId") or "").isdigit() else newest
            for record in response.get("history") or []:
                for added in record.get("messagesAdded") or []:
                    message_id = (added.get("message") or {}).get("id")
                    if message_id and message_id not in message_ids:
                        message_ids.append(message_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        messages: list[ChannelMessage] = []
        for message_id in message_ids:
            full = users.messages().get(userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Subject"]).execute()
            parsed = self.parse_message_summary(full)
            if parsed is not None:
                messages.append(parsed)
        # More pages remain: keep the old cursor so the next notification resumes.
        return messages, (None if page_token else newest)

    async def fetch_new_messages(self, notification_history_id: int) -> list[ChannelMessage]:
        """New INBOX messages since the stored history id; the first notification only sets the cursor."""
        start = self.load_history_id()
        if start is None:
            self.save_history_id(notification_history_id)
            return []
        # googleapiclient is synchronous; keep it off the event loop.
        messages, newest = await asyncio.to_thread(self._fetch_new_messages_sync, start)
        if newest is not None and newest > start:
            self.save_history_id(newest)
        return messages

    def start_watch(self, topic_name: str) -> dict[str, Any]:
        """Call users.watch; Gmail stops pushing after 7 days, so run this at least daily."""
        service = self.oauth.build_service()
        response = service.users().watch(userId="me", body={"topicName": topic_name, "labelIds": ["INBOX"], "labelFilterBehavior": "INCLUDE"}).execute()
        if str(response.get("historyId") or "").isdigit() and self.load_history_id() is None:
            self.save_history_id(int(response["historyId"]))
        return response

    # ------------------------------------------------------------------ outbound

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
        return await asyncio.to_thread(service.users().messages().send(userId="me", body=raw).execute)

    async def list_messages(self, query: str = "", max_results: int = 10) -> list[dict[str, Any]]:
        service = self.oauth.build_service()
        resp = await asyncio.to_thread(service.users().messages().list(userId="me", q=query, maxResults=max_results).execute)
        return resp.get("messages", [])
