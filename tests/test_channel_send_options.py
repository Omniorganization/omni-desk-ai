from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnidesk_agent.channels.native_messaging import MicrosoftTeamsChannel
from omnidesk_agent.config import MicrosoftTeamsConfig
from omnidesk_agent.core.models import ApprovalDecision
from omnidesk_agent.core.outbound_dispatcher import OutboundDispatcher
from omnidesk_agent.core.outbound_messages import OutboundMessageStore
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.channel_send import ChannelSendTool, validate_send_text_options


class RecordingPermissions:
    def __init__(self):
        self.proposals = []

    def verify(self, proposal):
        self.proposals.append(proposal)
        return ApprovalDecision(True, "allow", "test")


class SendAdapter:
    def __init__(self):
        self.sent = []

    async def send_text(self, recipient, text, **kwargs):
        self.sent.append((recipient, text, kwargs))
        return {"message_id": "m1", "request_id": "r1"}


def _send(tool, options, perms=None):
    perms = perms or RecordingPermissions()
    args = {"channel": "telegram", "recipient": "u1", "text": "hello"}
    if options is not ...:
        args["options"] = options
    return asyncio.run(tool.call("send_text", args, ToolContext(permissions=perms))), perms


@pytest.mark.parametrize(
    "options",
    [
        {"service_url": "https://attacker.example"},
        {"webhook_url": "https://attacker.example/hook"},
        {"callback_url": "https://attacker.example"},
        {"token": "x"},
        {"headers": {"Authorization": "Bearer x"}},
        {"receive_id_type": "open_id&x=1"},
        {"idempotency_key": "../../evil"},
        {"target": "public"},
        {"silent": True, "service_url": "https://attacker.example"},
    ],
)
def test_send_text_rejects_unsafe_option_keys_before_approval(options):
    adapter = SendAdapter()
    result, perms = _send(ChannelSendTool({"telegram": adapter}), options)
    assert result.ok is False
    assert "unsupported channels.send_text option" in result.error
    assert perms.proposals == []
    assert adapter.sent == []


@pytest.mark.parametrize(
    "options",
    ["silent", ["silent"], {"silent": "yes"}, {"thread_ts": 123}, {"thread_ts": "1" * 129}, {"surface": "email"}],
)
def test_send_text_rejects_malformed_option_values(options):
    adapter = SendAdapter()
    result, perms = _send(ChannelSendTool({"telegram": adapter}), options)
    assert result.ok is False
    assert result.error.startswith("unsupported channels.send_text option")
    assert perms.proposals == [] and adapter.sent == []


def test_send_text_options_are_shown_in_approval_and_bound_to_scope_hash():
    adapter = SendAdapter()
    tool = ChannelSendTool({"telegram": adapter})
    sent, perms = _send(tool, {"silent": True, "thread_ts": "1700000000.000100"})
    assert sent.ok is True
    assert adapter.sent == [("u1", "hello", {"silent": True, "thread_ts": "1700000000.000100"})]
    approved = perms.proposals[0]
    assert approved.args["options"] == {"silent": True, "thread_ts": "1700000000.000100"}

    _, plain_perms = _send(tool, ...)
    assert plain_perms.proposals[0].args["options"] == {}
    assert plain_perms.proposals[0].scope_hash != approved.scope_hash


def test_send_text_queues_only_validated_options(tmp_path: Path):
    store = OutboundMessageStore(tmp_path / "outbound.sqlite3")
    tool = ChannelSendTool({"telegram": SendAdapter()}, store)
    queued, _ = _send(tool, {"surface": "instagram"})
    assert queued.ok is True and queued.data["status"] == "pending"
    rejected, _ = _send(tool, {"service_url": "https://attacker.example"})
    assert rejected.ok is False
    rows = store.list(status="pending")
    assert len(rows) == 1


def test_channel_send_tool_publishes_strict_schema():
    spec = ChannelSendTool({}).spec()
    send_text = spec.actions["send_text"]
    options_schema = send_text.input_schema["properties"]["options"]
    assert options_schema["additionalProperties"] is False
    assert set(options_schema["properties"]) == {"silent", "thread_ts", "surface"}
    assert send_text.risk == "high" and send_text.requires_approval is True
    assert send_text.validate_args({"channel": "telegram", "recipient": "u1", "text": "hi", "expected_result": "sent"}) == []
    assert "unknown arg: service_url" in send_text.validate_args({"channel": "t", "recipient": "u", "text": "x", "service_url": "https://a"})
    assert "missing required arg: channel" in send_text.validate_args({"recipient": "u", "text": "x"})
    assert set(spec.actions) == {"send_text", "send_email"}


def test_validate_send_text_options_returns_copy():
    original = {"silent": False}
    clean = validate_send_text_options(original)
    assert clean == original and clean is not original
    assert validate_send_text_options(None) == {}


def test_dispatcher_dead_letters_queued_row_with_unsafe_options(tmp_path: Path):
    async def run_case():
        store = OutboundMessageStore(tmp_path / "outbound.sqlite3")
        adapter = SendAdapter()
        bad_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "hi", "options": {"service_url": "https://attacker.example"}})
        dispatcher = OutboundDispatcher(store, {"telegram": adapter})
        assert await dispatcher.run_once() is True
        row = store.get(bad_id)
        assert row["status"] == "dead_letter"
        assert adapter.sent == []

        good_id = store.create(channel="telegram", recipient="u1", payload={"type": "text", "text": "hi", "options": {"silent": True}})
        assert await dispatcher.run_once() is True
        assert store.get(good_id)["status"] == "sent"
        assert adapter.sent[0][2] == {"silent": True, "idempotency_key": store.get(good_id)["idempotency_key"]}

    asyncio.run(run_case())


def test_teams_send_text_ignores_caller_service_url(monkeypatch):
    monkeypatch.setenv("MICROSOFT_TEAMS_BOT_TOKEN", "bot-token")
    channel = MicrosoftTeamsChannel(MicrosoftTeamsConfig(service_url="https://smba.trafficmanager.net/emea/"))
    calls = []

    async def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(data={"id": "a1"}, request_id="req-1")

    channel.http = SimpleNamespace(post=fake_post)
    result = asyncio.run(channel.send_text("conv-1", "hi", service_url="https://attacker.example"))

    assert result == {"message_id": "a1", "request_id": "req-1"}
    assert calls[0][0] == "https://smba.trafficmanager.net/emea/v3/conversations/conv-1/activities"
    assert "attacker" not in calls[0][0]


def test_teams_send_text_requires_configured_service_url(monkeypatch):
    monkeypatch.setenv("MICROSOFT_TEAMS_BOT_TOKEN", "bot-token")
    channel = MicrosoftTeamsChannel(MicrosoftTeamsConfig())
    with pytest.raises(RuntimeError, match="service_url is missing"):
        asyncio.run(channel.send_text("conv-1", "hi", service_url="https://attacker.example"))
