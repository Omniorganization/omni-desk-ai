from __future__ import annotations

import hashlib
import hmac
import json
import time

from omnidesk_agent.channels.ecosystem import channel_matrix, resolve_channel
from omnidesk_agent.channels.native_messaging import (
    DiscordChannel,
    GoogleChatChannel,
    IMessageChannel,
    MatrixChannel,
    MicrosoftTeamsChannel,
    QQChannel,
    SignalChannel,
    SlackChannel,
)
from omnidesk_agent.config import (
    AppConfig,
    DiscordConfig,
    GoogleChatConfig,
    IMessageConfig,
    MatrixConfig,
    MicrosoftTeamsConfig,
    QQConfig,
    SignalConfig,
    SlackConfig,
)
from omnidesk_agent.daemon import OmniDeskRuntime


REQUIRED_NATIVE = {
    "telegram",
    "whatsapp_cloud",
    "slack",
    "discord",
    "google_chat",
    "signal",
    "imessage",
    "microsoft_teams",
    "matrix",
    "line",
    "wechat_official",
    "qq",
}


def test_requested_channels_are_native_catalog_entries():
    matrix = {item["name"]: item for item in channel_matrix()}
    for name in REQUIRED_NATIVE:
        assert matrix[name]["status"] == "native_adapter"
        assert matrix[name]["inbound"] is True
        assert matrix[name]["outbound"] is True
    assert resolve_channel("send this to Slack").status == "native_adapter"
    assert resolve_channel("通知 Teams").name == "microsoft_teams"
    assert resolve_channel("发到 QQ 群").name == "qq"


def test_runtime_registers_native_channel_adapters(tmp_path):
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    rt = OmniDeskRuntime(cfg)
    try:
        for name in REQUIRED_NATIVE:
            assert name in rt.adapters
        assert "whatsapp" in rt.adapters
        assert "wechat" in rt.adapters
        assert "teams" in rt.adapters
    finally:
        rt.close()


def test_slack_signature_and_message_parse(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    channel = SlackChannel(SlackConfig(allowed_user_ids=["U1"], allowed_channel_ids=["C1"]))
    payload = {"type": "event_callback", "event_id": "E1", "event": {"type": "message", "user": "U1", "channel": "C1", "text": "hello", "event_ts": "1700000000.1"}}
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"secret", b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    channel.verify_request({"x-slack-request-timestamp": ts, "x-slack-signature": sig}, body, {}, payload)
    msg = channel.parse_webhook(payload)
    assert msg.text == "hello"
    assert msg.thread_id == "C1"
    assert channel.parse_webhook({"type": "url_verification", "challenge": "ok"}) == {"challenge": "ok"}


def test_new_native_adapters_parse_normalized_messages():
    assert DiscordChannel(DiscordConfig(allowed_user_ids=["u1"])).parse_webhook({"id": "m1", "channel_id": "c1", "author": {"id": "u1"}, "content": "discord hi"}).text == "discord hi"
    assert GoogleChatChannel(GoogleChatConfig()).parse_webhook({"message": {"name": "spaces/s/messages/m", "text": "gchat hi", "space": {"name": "spaces/s"}}, "user": {"name": "users/u"}}).text == "gchat hi"
    assert SignalChannel(SignalConfig()).parse_webhook({"envelope": {"sourceNumber": "+100", "timestamp": 1, "dataMessage": {"message": "signal hi"}}}).text == "signal hi"
    assert IMessageChannel(IMessageConfig()).parse_webhook({"handle": "+100", "chat_id": "chat1", "guid": "g1", "text": "imessage hi"}).text == "imessage hi"
    assert MicrosoftTeamsChannel(MicrosoftTeamsConfig()).parse_webhook({"type": "message", "id": "a1", "from": {"id": "u1"}, "conversation": {"id": "conv"}, "text": "teams hi"}).text == "teams hi"
    assert MatrixChannel(MatrixConfig()).parse_webhook({"event": {"event_id": "$1", "sender": "@u:example", "room_id": "!r:example", "content": {"msgtype": "m.text", "body": "matrix hi"}}}).text == "matrix hi"
    assert QQChannel(QQConfig()).parse_webhook({"d": {"id": "m1", "author": {"id": "u1"}, "group_openid": "g1", "content": "qq hi"}}).text == "qq hi"
