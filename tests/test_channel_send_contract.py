from __future__ import annotations

import asyncio

import pytest

from omnidesk_agent.channels.meta_graph import MetaGraphChannel
from omnidesk_agent.channels.x_channel import XChannel
from omnidesk_agent.config import MetaGraphConfig, XConfig


def test_meta_graph_adapter_exposes_unified_send_text_contract():
    adapter = MetaGraphChannel(MetaGraphConfig())
    assert hasattr(adapter, "send_text")
    with pytest.raises(ValueError):
        asyncio.run(adapter.send_text("recipient", "hello", surface="unknown"))


def test_x_adapter_exposes_send_text_without_implicit_public_posting():
    adapter = XChannel(XConfig())
    assert hasattr(adapter, "send_text")
    with pytest.raises(NotImplementedError):
        asyncio.run(adapter.send_text("user-123", "hello"))
