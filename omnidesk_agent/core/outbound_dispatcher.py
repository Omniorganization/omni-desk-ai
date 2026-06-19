from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from omnidesk_agent.core.outbound_messages import OutboundMessageStore
from omnidesk_agent.channels.provider_errors import classify_channel_error
from omnidesk_agent.channels.idempotency import capability_for


class OutboundDispatcher:
    """Background worker that delivers queued outbound channel messages.

    Provider sends are isolated from tool execution so user-facing tasks can
    create a durable pending outbound message and rely on retry/dead-letter
    handling when providers rate-limit or fail transiently.
    """

    def __init__(
        self,
        store: OutboundMessageStore,
        adapters: dict[str, Any],
        *,
        poll_interval_seconds: float = 0.25,
        lease_seconds: int = 300,
    ):
        self.store = store
        self.adapters = adapters
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop: asyncio.Event | None = None
        self._stop_loop: asyncio.AbstractEventLoop | None = None

    def _get_stop_event(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        if self._stop is None or self._stop_loop is not loop:
            self._stop = asyncio.Event()
            self._stop_loop = loop
        return self._stop

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.store.recover_stale_running(lease_seconds=self.lease_seconds)
        self._get_stop_event().clear()
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        self._get_stop_event().set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass

    async def run_forever(self) -> None:
        stop_event = self._get_stop_event()
        while not stop_event.is_set():
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(self.poll_interval_seconds)

    async def run_once(self) -> bool:
        message = self.store.claim_next()
        if not message:
            return False
        message_id = str(message["id"])
        try:
            await self._deliver(message)
        except Exception as exc:
            info = classify_channel_error(exc)
            channel = str(message.get("channel", "unknown"))
            capability = capability_for(channel)
            ambiguous_categories = {"timeout", "network_error", "provider_5xx"}
            if info.retryable and capability.mode != "native" and info.category in ambiguous_categories:
                self.store.mark_ambiguous(
                    message_id,
                    str(exc),
                    category=info.category,
                    provider_request_id=str(message.get("idempotency_key") or message_id),
                )
            else:
                self.store.mark_failed(message_id, str(exc), dead_letter=info.dead_letter_now or not info.retryable, category=info.category)
            metrics = getattr(self.store, "metrics", None)
            inc = getattr(metrics, "inc", None)
            if callable(inc):
                inc("omnidesk_outbound_failures_total", channel=channel, category=info.category)
                if info.retryable and capability.mode != "native" and info.category in ambiguous_categories:
                    inc("omnidesk_outbound_ambiguous_send_total", channel=channel, category=info.category)
                elif info.dead_letter_now or not info.retryable:
                    inc("omnidesk_outbound_dead_letter_total", channel=channel, category=info.category)
                else:
                    inc("omnidesk_outbound_retry_total", channel=channel, category=info.category)
        return True

    async def _deliver(self, message: dict[str, Any]) -> None:
        channel = str(message["channel"])
        recipient = str(message["recipient"])
        adapter = self.adapters.get(channel)
        if adapter is None:
            raise RuntimeError(f"Unknown channel adapter: {channel}")
        if not hasattr(adapter, "send_text"):
            raise RuntimeError(f"Channel {channel} does not support send_text")
        payload = json.loads(str(message["payload_json"]))
        payload_type = payload.get("type")
        if payload_type != "text":
            raise RuntimeError(f"Unsupported outbound payload type: {payload_type}")
        options = dict(payload.get("options") or {})
        idempotency_key = str(message.get("idempotency_key") or message["id"])
        options.setdefault("idempotency_key", idempotency_key)
        result = await adapter.send_text(recipient, str(payload.get("text", "")), **options)
        provider_message_id = None
        provider_request_id = idempotency_key
        if isinstance(result, dict):
            provider_message_id = result.get("message_id") or result.get("id")
            provider_request_id = result.get("request_id") or provider_request_id
        self.store.mark_sent(
            str(message["id"]),
            provider_message_id=provider_message_id,
            provider_request_id=provider_request_id,
        )
