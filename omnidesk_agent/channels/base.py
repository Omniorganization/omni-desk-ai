from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol
from omnidesk_agent.core.models import ChannelMessage

MessageHandler = Callable[[ChannelMessage], Awaitable[dict]]


@dataclass
class WebhookEnvelope:
    source_key: str = "unknown"
    sender_id: str = "unknown"
    message_id: Optional[str] = None
    timestamp: Optional[float] = None
    event_type: Optional[str] = None
    raw: Optional[dict[str, Any]] = None


class Channel(Protocol):
    name: str

    async def start(self, handler: MessageHandler) -> None: ...
    async def send_text(self, recipient: str, text: str, **kwargs) -> None: ...


class ChannelAdapter(Protocol):
    name: str

    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload: Any) -> None: ...
    def extract_envelope(self, payload: Any) -> WebhookEnvelope: ...
    def parse_webhook(self, payload: Any) -> Any: ...
