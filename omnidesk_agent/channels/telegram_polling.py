"""Telegram getUpdates long polling, for deployments without a public webhook URL."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Callable, Optional

from omnidesk_agent.channels.http_client import ChannelHttpClient, ChannelHttpError
from omnidesk_agent.channels.provider_errors import ensure_provider_success

LOG = logging.getLogger(__name__)
# Only the update types TelegramChannel.parse_update turns into messages.
TELEGRAM_POLL_UPDATE_TYPES = ("message", "business_message")


class TelegramPoller:
    """Long-polls ``getUpdates`` and hands each update to ``TelegramChannel.parse_update``.

    The same parser runs as on ``/webhooks/telegram``, so the sender
    allowlist applies unchanged; requests carry the bot token over TLS, so
    there is no webhook secret to check. An update is confirmed (the next call
    asks for ``update_id + 1``) only after its message was delivered. A crash
    in between re-reads it; the job queue's dedupe key keeps that from
    creating a second job. Telegram refuses ``getUpdates`` while a webhook is
    set (HTTP 409), which surfaces as a ``config_error``.
    """

    def __init__(
        self,
        channel: Any,
        deliver: Callable[[Any], Any],
        *,
        timeout_seconds: int = 30,
        error_backoff_seconds: float = 5.0,
        http: Optional[ChannelHttpClient] = None,
    ):
        self.channel = channel
        self.deliver = deliver
        self.timeout_seconds = max(1, min(int(timeout_seconds), 50))
        self.error_backoff_seconds = error_backoff_seconds
        # The HTTP timeout must outlast the long poll itself.
        self.http = http or ChannelHttpClient(timeout=self.timeout_seconds + 15.0)
        self.offset: Optional[int] = None

    async def poll_once(self, *, timeout_seconds: Optional[int] = None) -> int:
        """Fetch one batch, deliver its messages, and return how many updates were confirmed."""
        base = getattr(self.channel, "base", "")
        if not base:
            raise RuntimeError("Telegram token is not configured")
        params: dict[str, Any] = {
            "timeout": self.timeout_seconds if timeout_seconds is None else max(0, int(timeout_seconds)),
            "allowed_updates": json.dumps(list(TELEGRAM_POLL_UPDATE_TYPES)),
        }
        if self.offset is not None:
            params["offset"] = self.offset
        result = await self.http.get(f"{base}/getUpdates", params=params, channel="telegram")
        ensure_provider_success("telegram", result)
        updates = result.data.get("result") if isinstance(result.data, dict) else None
        if not isinstance(updates, list):
            raise ChannelHttpError("Telegram getUpdates returned no result list", channel="telegram", request_sent=True, category="provider_rejected")
        confirmed = 0
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not isinstance(update_id, int) or isinstance(update_id, bool):
                continue
            message = self.channel.parse_update(update)
            if message is not None:
                outcome = self.deliver(message)
                if inspect.isawaitable(outcome):
                    await outcome
            self.offset = update_id + 1
            confirmed += 1
        return confirmed

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.poll_once()
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep polling; the error text is redacted by the transport
                LOG.warning("telegram getUpdates failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.error_backoff_seconds)
            except asyncio.TimeoutError:
                pass
