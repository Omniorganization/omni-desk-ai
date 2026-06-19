from __future__ import annotations
from omnidesk_agent.channels.telegram import TelegramChannel
from omnidesk_agent.channels.whatsapp_cloud import WhatsAppCloudChannel
from omnidesk_agent.channels.line import LineChannel
from omnidesk_agent.config import TelegramConfig, WhatsAppCloudConfig, LineConfig


def test_telegram_extract_envelope():
    env = TelegramChannel(TelegramConfig()).extract_envelope({"message": {"message_id": 5, "from": {"id": 9}, "chat": {"id": 1}}})
    assert env.sender_id == "9"
    assert env.message_id == "5"


def test_whatsapp_extract_envelope():
    payload = {"entry": [{"changes": [{"value": {"messages": [{"from": "6012", "id": "wamid.1"}]}}]}]}
    env = WhatsAppCloudChannel(WhatsAppCloudConfig()).extract_envelope(payload)
    assert env.sender_id == "6012"
    assert env.message_id == "wamid.1"


def test_line_extract_envelope():
    payload = {"events": [{"source": {"userId": "U1"}, "message": {"id": "M1"}}]}
    env = LineChannel(LineConfig()).extract_envelope(payload)
    assert env.sender_id == "U1"
    assert env.message_id == "M1"
