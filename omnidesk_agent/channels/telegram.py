from __future__ import annotations

import os
from typing import Any, Optional

from omnidesk_agent.config import TelegramConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.channels.http_client import ChannelHttpClient

class TelegramChannel:
    name = "telegram"
    def extract_envelope(self, payload: dict[str, Any]):
        from omnidesk_agent.channels.base import WebhookEnvelope
        msg = (
            payload.get("message")
            or payload.get("edited_message")
            or payload.get("channel_post")
            or payload.get("business_message")
            or {}
        )
        chat = msg.get("chat") or {}
        user = msg.get("from") or {}
        sender = str(user.get("id") or chat.get("id") or "unknown")
        return WebhookEnvelope(source_key=sender, sender_id=sender, message_id=self._replay_key(payload, msg, chat), raw=payload)

    @staticmethod
    def _replay_key(payload: dict[str, Any], msg: dict[str, Any], chat: dict[str, Any]) -> Optional[str]:
        # message_id is only unique inside one chat, so keying replay protection
        # on it lets any user burn the ids another chat will use. update_id is
        # assigned by Telegram and unique per bot; the prefixes keep these keys
        # apart from digests stored under the old message_id-only scheme.
        update_id = payload.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            return f"update:{update_id}"
        message_id = msg.get("message_id")
        chat_id = chat.get("id")
        if message_id is not None and chat_id is not None:
            return f"chat:{chat_id}:{message_id}"
        return None

    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg
        self.token = os.getenv(cfg.bot_token_env, "")
        self.base = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.http = ChannelHttpClient()


    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload) -> None:
        import hmac
        from omnidesk_agent.channels.verify import env_secret, header
        secret = env_secret(self.cfg.webhook_secret_env, channel=self.name)
        provided = header(headers, "x-telegram-bot-api-secret-token")
        if not hmac.compare_digest(provided, secret):
            raise PermissionError("invalid Telegram webhook secret token")

    def parse_update(self, update: dict[str, Any]) -> Optional[ChannelMessage]:
        msg = update.get("message") or update.get("business_message")
        if not msg or "text" not in msg:
            return None
        user = msg.get("from", {})
        user_id = int(user.get("id", 0))
        if self.cfg.allowed_user_ids and user_id not in self.cfg.allowed_user_ids:
            return None
        return ChannelMessage(
            channel=self.name,
            sender_id=str(user_id),
            thread_id=str(msg.get("chat", {}).get("id")),
            message_id=str(msg.get("message_id")),
            text=msg.get("text", ""),
            raw=update,
        )

    async def send_text(self, recipient: str, text: str, **kwargs) -> None:
        if not self.base:
            raise RuntimeError("Telegram token is not configured")
        return await self.http.post(f"{self.base}/sendMessage", json={"chat_id": recipient, "text": text}, idempotency_key=kwargs.get("idempotency_key"), channel=self.name)
