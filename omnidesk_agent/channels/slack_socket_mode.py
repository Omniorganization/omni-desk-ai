"""Slack Socket Mode: Events API payloads over a websocket, without a public URL.

``apps.connections.open`` (app-level ``xapp-`` token with ``connections:write``)
returns a one-time ``wss://`` URL. Slack then sends ``hello``, ``events_api``
envelopes whose ``payload`` is the same body the HTTP Events API would POST,
and ``disconnect`` when the connection should be replaced. Every envelope must
be acknowledged with ``{"envelope_id": ...}`` within three seconds or Slack
retries it. Payloads go through ``SlackChannel.parse_webhook``, so the bot-
message filter and the user/channel allowlists apply as on ``/webhooks/slack``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Callable, Optional

from omnidesk_agent.channels.http_client import ChannelHttpClient, ChannelHttpError
from omnidesk_agent.channels.provider_errors import ensure_provider_success

SLACK_CONNECTIONS_OPEN_URL = "https://slack.com/api/apps.connections.open"


def _default_connect(url: str):
    from websockets.asyncio.client import connect

    return connect(url, open_timeout=15, close_timeout=5, max_size=2**20)


class SlackSocketModeReceiver:
    def __init__(
        self,
        channel: Any,
        app_token: str,
        deliver: Callable[[Any], Any],
        *,
        http: Optional[ChannelHttpClient] = None,
        connect: Callable[[str], Any] = _default_connect,
    ):
        if not app_token.startswith("xapp-"):
            raise ValueError("Slack Socket Mode needs an app-level token (xapp-...) with connections:write")
        self.channel = channel
        self.app_token = app_token
        self.deliver = deliver
        self.http = http or ChannelHttpClient()
        self.connect = connect
        self.acknowledged = 0

    async def open_url(self) -> str:
        result = await self.http.post(SLACK_CONNECTIONS_OPEN_URL, headers={"Authorization": f"Bearer {self.app_token}"}, channel="slack", idempotent=True)
        ensure_provider_success("slack", result)
        url = result.data.get("url") if isinstance(result.data, dict) else None
        if not isinstance(url, str) or not url.startswith("wss://"):
            raise ChannelHttpError("apps.connections.open returned no wss:// URL", channel="slack", request_sent=True, category="config_error")
        return url

    async def handle_frame(self, websocket: Any, raw: Any) -> str:
        """Acknowledge and process one frame; returns its type ("disconnect" ends the connection)."""
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            return "invalid"
        if not isinstance(frame, dict):
            return "invalid"
        frame_type = str(frame.get("type") or "")
        envelope_id = frame.get("envelope_id")
        if isinstance(envelope_id, str) and envelope_id:
            # Ack first: Slack retries anything not acknowledged within 3 s.
            await websocket.send(json.dumps({"envelope_id": envelope_id}))
            self.acknowledged += 1
        if frame_type == "events_api" and isinstance(frame.get("payload"), dict):
            parsed = self.channel.parse_webhook(frame["payload"])
            if parsed is not None and not isinstance(parsed, dict):
                outcome = self.deliver(parsed)
                if inspect.isawaitable(outcome):
                    await outcome
        return frame_type

    async def run_until(self, done: Callable[[], bool], *, timeout_seconds: float, on_connected: Optional[Callable[[], Any]] = None) -> bool:
        """Receive until ``done()`` is true or the timeout passes; reconnects on ``disconnect``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        connected_once = False
        while not done() and loop.time() < deadline:
            async with self.connect(await self.open_url()) as websocket:
                while not done():
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return False
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        return False
                    frame_type = await self.handle_frame(websocket, raw)
                    if frame_type == "hello" and not connected_once:
                        connected_once = True
                        if on_connected is not None:
                            outcome = on_connected()
                            if inspect.isawaitable(outcome):
                                await outcome
                    if frame_type == "disconnect":
                        break
        return done()
