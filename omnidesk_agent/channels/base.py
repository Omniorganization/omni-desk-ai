from __future__ import annotations

from typing import Protocol, Callable, Awaitable
from omnidesk_agent.core.models import ChannelMessage

MessageHandler = Callable[[ChannelMessage], Awaitable[dict]]


class Channel(Protocol):
    name: str

    async def start(self, handler: MessageHandler) -> None: ...
    async def send_text(self, recipient: str, text: str, **kwargs) -> None: ...
