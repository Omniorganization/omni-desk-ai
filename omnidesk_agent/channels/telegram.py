from __future__ import annotations

import os
import httpx
from typing import Any

from omnidesk_agent.config import TelegramConfig
from omnidesk_agent.core.models import ChannelMessage


class TelegramChannel:
    name = "telegram"

    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg
        self.token = os.getenv(cfg.bot_token_env, "")
        self.base = f"https://api.telegram.org/bot{self.token}" if self.token else ""

    def parse_update(self, update: dict[str, Any]) -> ChannelMessage | None:
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
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{self.base}/sendMessage", json={"chat_id": recipient, "text": text})
            r.raise_for_status()
