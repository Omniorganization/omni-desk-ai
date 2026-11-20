"""Provider error envelopes decide whether a message was delivered.

WeChat, DingTalk, Lark and Slack answer most failures with HTTP 200 and an
error code in the body. Recording those as "sent" silently loses the message
(for example WeChat 45015, outside the 48h window). These tests check that each
envelope becomes the outbox outcome its meaning requires, and that successful
sends record the provider's message id for reconciliation.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnidesk_agent.channels import http_client
from omnidesk_agent.channels.dingtalk import DingTalkChannel
from omnidesk_agent.channels.http_client import ChannelHttpError
from omnidesk_agent.channels.lark_feishu import LarkChannel
from omnidesk_agent.channels.meta_graph import MetaGraphChannel
from omnidesk_agent.channels.native_messaging import DiscordChannel, SlackChannel
from omnidesk_agent.channels.provider_errors import classify_channel_error
from omnidesk_agent.channels.wechat_official import WeChatOfficialChannel
from omnidesk_agent.channels.whatsapp_cloud import WhatsAppCloudChannel
from omnidesk_agent.config import DingTalkConfig, DiscordConfig, LarkConfig, MetaGraphConfig, SlackConfig, WeChatOfficialConfig, WhatsAppCloudConfig
from omnidesk_agent.core.outbound_dispatcher import OutboundDispatcher
from omnidesk_agent.core.outbound_messages import OutboundMessageStore


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)
        self.headers = headers or {}

    def json(self):
        return self._body


class TimeoutException(Exception):
    pass


class NetworkError(Exception):
    pass


class TransportError(Exception):
    pass


class ScriptedHttpx:
    TimeoutException = TimeoutException
    NetworkError = NetworkError
    TransportError = TransportError

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def AsyncClient(self, timeout):
        module = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, headers=None, params=None, json=None, data=None):
                module.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "params": dict(params or {}), "json": json})
                item = module.script.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item

        return Client()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(http_client.asyncio, "sleep", no_sleep)


def _dispatch(tmp_path: Path, channel: str, adapter, recipient: str) -> dict:
    async def run_case():
        store = OutboundMessageStore(tmp_path / f"{channel}.sqlite3", max_retries=3, base_retry_seconds=1)
        outbound_id = store.create(channel=channel, recipient=recipient, payload={"type": "text", "text": "hello"}, idempotency_key=f"{channel}-key")
        await OutboundDispatcher(store, {channel: adapter}).run_once()
        return store.get(outbound_id)

    return asyncio.run(run_case())


def _wechat(monkeypatch) -> WeChatOfficialChannel:
    monkeypatch.setenv("WECHAT_APP_ID", "wx-app")
    monkeypatch.setenv("WECHAT_APP_SECRET", "wx-secret")
    return WeChatOfficialChannel(WeChatOfficialConfig(enabled=True))


TOKEN = FakeResponse(200, {"access_token": "wx-token", "expires_in": 7200})


def test_wechat_outside_48h_window_dead_letters_instead_of_sent(monkeypatch, tmp_path):
    module = ScriptedHttpx([TOKEN, FakeResponse(200, {"errcode": 45015, "errmsg": "response out of time limit or subscription is canceled"})])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch(tmp_path, "wechat_official", _wechat(monkeypatch), "openid-1")

    assert row["status"] == "dead_letter"
    assert row["error_category"] == "policy_error"
    assert "45015" in row["last_error"]


def test_wechat_token_rejected_refreshes_once_and_resends(monkeypatch, tmp_path):
    # 40001 means the send was refused for the token, not performed, so one
    # resend with a fresh token cannot duplicate the message.
    module = ScriptedHttpx([
        TOKEN,
        FakeResponse(200, {"errcode": 40001, "errmsg": "invalid credential, access_token is invalid or not latest"}),
        FakeResponse(200, {"access_token": "wx-token-2", "expires_in": 7200}),
        FakeResponse(200, {"errcode": 0, "errmsg": "ok"}),
    ])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch(tmp_path, "wechat_official", _wechat(monkeypatch), "openid-1")

    assert row["status"] == "sent", row
    sends = [call for call in module.calls if call["url"].endswith("/message/custom/send")]
    assert [call["params"]["access_token"] for call in sends] == ["wx-token", "wx-token-2"]


def test_wechat_token_uses_stable_token_with_secret_in_body(monkeypatch, tmp_path):
    module = ScriptedHttpx([TOKEN, FakeResponse(200, {"errcode": 0, "errmsg": "ok"}), FakeResponse(200, {"errcode": 0, "errmsg": "ok"})])
    monkeypatch.setattr(http_client, "httpx", module)
    channel = _wechat(monkeypatch)

    async def two_sends():
        await channel.send_text("openid-1", "a")
        await channel.send_text("openid-1", "b")

    asyncio.run(two_sends())

    token_calls = [call for call in module.calls if "stable_token" in call["url"]]
    assert len(token_calls) == 1  # cached across sends
    assert token_calls[0]["method"] == "POST"
    assert token_calls[0]["json"]["force_refresh"] is False  # replicas share one token
    assert "secret" not in token_calls[0]["params"]


def test_wechat_token_error_is_a_config_error_not_a_key_error(monkeypatch, tmp_path):
    module = ScriptedHttpx([FakeResponse(200, {"errcode": 40013, "errmsg": "invalid appid"})])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch(tmp_path, "wechat_official", _wechat(monkeypatch), "openid-1")

    assert row["status"] == "dead_letter"
    assert row["error_category"] == "config_error"


def test_dingtalk_robot_keyword_or_signature_failure_is_not_sent(monkeypatch, tmp_path):
    monkeypatch.setenv("DINGTALK_ROBOT_TOKEN", "robot-token")
    module = ScriptedHttpx([FakeResponse(200, {"errcode": 310000, "errmsg": "keywords not in content"})])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch(tmp_path, "dingtalk", DingTalkChannel(DingTalkConfig(enabled=True)), "robot")

    assert row["status"] == "dead_letter"
    assert row["error_category"] == "config_error"


def test_dingtalk_robot_rate_limit_is_retried_later(monkeypatch, tmp_path):
    monkeypatch.setenv("DINGTALK_ROBOT_TOKEN", "robot-token")
    module = ScriptedHttpx([FakeResponse(200, {"errcode": 410100, "errmsg": "send too fast, exceed 20 times per minute"})])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch(tmp_path, "dingtalk", DingTalkChannel(DingTalkConfig(enabled=True)), "robot")

    assert row["status"] == "retry"
    assert row["error_category"] == "rate_limit"


def test_lark_error_code_inside_200_is_not_sent(monkeypatch, tmp_path):
    monkeypatch.setenv("LARK_APP_ID", "cli_a")
    monkeypatch.setenv("LARK_APP_SECRET", "s")
    module = ScriptedHttpx([
        FakeResponse(200, {"code": 0, "tenant_access_token": "t-1", "expire": 7200}),
        FakeResponse(200, {"code": 230002, "msg": "Bot/User can NOT be out of the chat."}),
    ])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch(tmp_path, "lark", LarkChannel(LarkConfig(enabled=True)), "oc_chat")

    assert row["status"] == "dead_letter"
    assert row["error_category"] == "recipient_unavailable"


def test_slack_ok_false_maps_to_slack_vocabulary(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    for error, status, category in (
        ("channel_not_found", "dead_letter", "bad_request"),
        ("invalid_auth", "dead_letter", "auth_error"),
        ("ratelimited", "retry", "rate_limit"),
        # Slack says part of the operation may have succeeded: reconcile, don't resend.
        ("internal_error", "ambiguous", "provider_5xx"),
    ):
        module = ScriptedHttpx([FakeResponse(200, {"ok": False, "error": error})])
        monkeypatch.setattr(http_client, "httpx", module)
        row = _dispatch(tmp_path / error, "slack", SlackChannel(SlackConfig(enabled=True)), "C1")
        assert (row["status"], row["error_category"]) == (status, category), error


def test_graph_throttling_in_a_400_is_retried_not_dead_lettered():
    # Graph reports throttling as HTTP 400 with error.code 4/17/32/613/130429.
    for code in (4, 17, 32, 613, 130429, 131056):
        exc = ChannelHttpError("channel provider HTTP 400", status_code=400, channel="whatsapp_cloud", response_json={"error": {"code": code, "message": "throttled"}})
        info = classify_channel_error(exc)
        assert (info.category, info.retryable, info.dead_letter_now) == ("rate_limit", True, False), code
    window = ChannelHttpError("channel provider HTTP 400", status_code=400, channel="whatsapp_cloud", response_json={"error": {"code": 131047, "message": "Re-engagement message"}})
    assert classify_channel_error(window).category == "policy_error"
    messenger_window = ChannelHttpError("channel provider HTTP 400", status_code=400, channel="meta_graph", response_json={"error": {"code": 10, "error_subcode": 2018278}})
    assert classify_channel_error(messenger_window).category == "policy_error"
    expired = ChannelHttpError("channel provider HTTP 400", status_code=400, channel="meta_graph", response_json={"error": {"code": 190, "message": "Error validating access token"}})
    assert classify_channel_error(expired).category == "auth_error"


def test_whatsapp_records_the_wamid(monkeypatch, tmp_path):
    monkeypatch.setenv("WHATSAPP_CLOUD_TOKEN", "wa-token")
    module = ScriptedHttpx([FakeResponse(200, {"messaging_product": "whatsapp", "contacts": [{"input": "1555", "wa_id": "1555"}], "messages": [{"id": "wamid.HBgL"}]})])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch(tmp_path, "whatsapp_cloud", WhatsAppCloudChannel(WhatsAppCloudConfig(enabled=True, phone_number_id="10950")), "1555")

    assert row["status"] == "sent"
    assert row["provider_message_id"] == "wamid.HBgL"


def test_meta_sends_the_page_token_in_a_header_not_the_url(monkeypatch, tmp_path):
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "page-token")
    module = ScriptedHttpx([FakeResponse(200, {"recipient_id": "psid-1", "message_id": "m_abc"})])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch(tmp_path, "meta_graph", MetaGraphChannel(MetaGraphConfig(enabled=True)), "psid-1")

    call = module.calls[0]
    assert "access_token" not in call["params"] and "page-token" not in call["url"]
    assert call["headers"]["Authorization"] == "Bearer page-token"
    assert row["provider_message_id"] == "m_abc"


def test_discord_send_carries_an_enforced_nonce_stable_per_message(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot")
    module = ScriptedHttpx([TimeoutException("timed out"), FakeResponse(200, {"id": "111"})])
    monkeypatch.setattr(http_client, "httpx", module)

    result = asyncio.run(DiscordChannel(DiscordConfig(enabled=True)).send_text("chan", "hi", idempotency_key="outbox-key"))

    # Discord returns the existing message for a repeated nonce, so the
    # transport may retry this send; both attempts carry the same nonce.
    nonces = {call["json"]["nonce"] for call in module.calls}
    assert len(module.calls) == 2 and len(nonces) == 1 and len(nonces.pop()) <= 25
    assert all(call["json"]["enforce_nonce"] is True for call in module.calls)
    assert result["message_id"] == "111"
