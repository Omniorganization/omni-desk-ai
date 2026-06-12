from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac

import pytest

from omnidesk_agent.channels.line import LineChannel
from omnidesk_agent.channels.meta_graph import MetaGraphChannel
from omnidesk_agent.channels.telegram import TelegramChannel
from omnidesk_agent.channels.whatsapp_cloud import WhatsAppCloudChannel
from omnidesk_agent.config import LineConfig, MetaGraphConfig, TelegramConfig, WhatsAppCloudConfig


def test_telegram_parse_envelope_and_missing_send_token():
    channel = TelegramChannel(TelegramConfig(allowed_user_ids=[42]))
    payload = {"update_id": 9, "message": {"message_id": 4, "from": {"id": 42}, "chat": {"id": 77}, "text": "hi"}}

    msg = channel.parse_update(payload)
    envelope = channel.extract_envelope(payload)

    assert msg.sender_id == "42"
    assert msg.thread_id == "77"
    assert envelope.source_key == "42"
    assert channel.parse_update({"message": {"from": {"id": 99}, "text": "blocked"}}) is None
    assert channel.parse_update({"message": {"from": {"id": 42}}}) is None

    async def run_case():
        with pytest.raises(RuntimeError, match="token is not configured"):
            await channel.send_text("42", "hello")

    asyncio.run(run_case())


def test_whatsapp_parse_envelope_and_send_preflight_errors():
    channel = WhatsAppCloudChannel(WhatsAppCloudConfig(allowed_wa_ids=["1555"]))
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {"from": "1555", "id": "m1", "timestamp": "1700000000", "text": {"body": "hello"}},
            {"from": "blocked", "id": "m2", "text": {"body": "nope"}},
        ]}}]}]
    }

    messages = channel.parse_webhook(payload)
    envelope = channel.extract_envelope(payload)

    assert [m.text for m in messages] == ["hello"]
    assert envelope.source_key == "1555"
    assert channel.extract_envelope({"entry": "bad"}).raw == {"entry": "bad"}

    async def run_case():
        with pytest.raises(RuntimeError, match="phone_number_id"):
            await channel.send_text("1555", "hello")

    asyncio.run(run_case())


def test_meta_parse_envelope_and_send_surface_errors():
    channel = MetaGraphChannel(MetaGraphConfig(allowed_psids=["p1"]))
    payload = {"entry": [{"messaging": [
        {"sender": {"id": "p1"}, "timestamp": 1700000000000, "message": {"mid": "m1", "text": "hello"}},
        {"sender": {"id": "blocked"}, "message": {"text": "nope"}},
    ]}]}

    messages = channel.parse_webhook(payload)
    envelope = channel.extract_envelope(payload)

    assert [m.text for m in messages] == ["hello"]
    assert envelope.source_key == "p1"
    assert channel.extract_envelope({"entry": "bad"}).raw == {"entry": "bad"}

    async def run_case():
        with pytest.raises(ValueError, match="Unsupported Meta"):
            await channel.send_text("p1", "hello", surface="threads")
        with pytest.raises(RuntimeError, match="access token"):
            await channel.send_text("p1", "hello", surface="facebook")
        with pytest.raises(RuntimeError, match="instagram_account_id"):
            await channel.send_text("p1", "hello", surface="instagram")

    asyncio.run(run_case())


def test_line_signature_parse_and_missing_send_token(monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "secret")
    channel = LineChannel(LineConfig(allowed_user_ids=["u1"]))
    body = b'{"events":[]}'
    signature = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode("ascii")
    payload = {"events": [
        {"source": {"userId": "u1"}, "timestamp": 1700000000000, "webhookEventId": "e1", "message": {"id": "m1", "text": "hello"}},
        {"source": {"userId": "blocked"}, "message": {"id": "m2", "text": "nope"}},
    ]}

    assert channel.verify_signature(body, signature) is True
    assert channel.verify_signature(body, "bad") is False
    assert [m.text for m in channel.parse_webhook(payload)] == ["hello"]
    assert channel.extract_envelope(payload).source_key == "u1"

    async def run_case():
        with pytest.raises(RuntimeError, match="access token"):
            await channel.send_text("u1", "hello")

    asyncio.run(run_case())
