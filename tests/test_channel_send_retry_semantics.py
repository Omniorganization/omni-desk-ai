"""Transport retries must never turn one approved message into several.

The outbound dispatcher records a timeout or 5xx from a provider without
native dedupe as an *ambiguous* send for reconciliation. That only works if the
transport underneath hands the error up instead of quietly POSTing again: a
customer would otherwise get the message twice while the outbox shows one send.
These tests drive the real ChannelHttpClient (with a scripted httpx stand-in)
under the real dispatcher.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from omnidesk_agent.channels import http_client
from omnidesk_agent.channels.http_client import ChannelHttpClient, ChannelHttpError
from omnidesk_agent.channels.line import LineChannel
from omnidesk_agent.channels.provider_errors import classify_channel_error
from omnidesk_agent.channels.telegram import TelegramChannel
from omnidesk_agent.config import LineConfig, TelegramConfig
from omnidesk_agent.core.outbound_dispatcher import OutboundDispatcher
from omnidesk_agent.core.outbound_messages import OutboundMessageStore


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = {"ok": True} if body is None else body
        self.text = json.dumps(self._body)
        self.headers = headers or {}

    def json(self):
        return self._body


class TimeoutException(Exception):
    pass


class ReadTimeout(TimeoutException):
    pass


class NetworkError(Exception):
    pass


class ConnectError(NetworkError):
    pass


class TransportError(Exception):
    pass


class ScriptedHttpx:
    """httpx stand-in replaying a script of responses/exceptions, one per request."""

    TimeoutException = TimeoutException
    ReadTimeout = ReadTimeout
    NetworkError = NetworkError
    ConnectError = ConnectError
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
                module.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "json": json})
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


def _telegram(monkeypatch) -> TelegramChannel:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "a" * 35)
    return TelegramChannel(TelegramConfig(enabled=True))


def _dispatch_once(tmp_path: Path, channel: str, adapter, recipient: str = "42") -> dict:
    async def run_case():
        store = OutboundMessageStore(tmp_path / "outbound.sqlite3", max_retries=3, base_retry_seconds=1)
        outbound_id = store.create(channel=channel, recipient=recipient, payload={"type": "text", "text": "hello"}, idempotency_key="outbox-key-1")
        await OutboundDispatcher(store, {channel: adapter}).run_once()
        return store.get(outbound_id)

    return asyncio.run(run_case())


def test_telegram_read_timeout_is_one_post_and_an_ambiguous_send(monkeypatch, tmp_path):
    # The audit's probe: timeout, then 503, then 200 used to become 3 POSTs and
    # one "sent" row. Now the first timeout ends the attempt.
    module = ScriptedHttpx([ReadTimeout("The read operation timed out"), FakeResponse(503), FakeResponse(200, {"ok": True, "result": {"message_id": 7}})])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch_once(tmp_path, "telegram", _telegram(monkeypatch))

    assert len(module.calls) == 1
    assert row["status"] == "ambiguous"
    assert row["error_category"] == "timeout"
    assert "bot123456" not in (row["last_error"] or "")


def test_telegram_5xx_is_one_post_and_an_ambiguous_send(monkeypatch, tmp_path):
    module = ScriptedHttpx([FakeResponse(502, {"ok": False, "error_code": 502, "description": "Bad Gateway"}), FakeResponse(200)])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch_once(tmp_path, "telegram", _telegram(monkeypatch))

    assert len(module.calls) == 1
    assert row["status"] == "ambiguous"
    assert row["error_category"] == "provider_5xx"


def test_connection_failure_before_send_is_retried_and_message_id_recorded(monkeypatch, tmp_path):
    # Nothing reached Telegram, so repeating cannot duplicate anything.
    module = ScriptedHttpx([ConnectError("connection refused"), FakeResponse(200, {"ok": True, "result": {"message_id": 99}})])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch_once(tmp_path, "telegram", _telegram(monkeypatch))

    assert len(module.calls) == 2
    assert row["status"] == "sent"
    assert row["provider_message_id"] == "99"


def test_unreachable_provider_retries_later_instead_of_reconciliation(monkeypatch, tmp_path):
    module = ScriptedHttpx([ConnectError("refused")] * 3)
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch_once(tmp_path, "telegram", _telegram(monkeypatch))

    assert len(module.calls) == 3
    # Never sent, so it is a plain retry, not an ambiguous send someone must reconcile.
    assert row["status"] == "retry"
    assert row["error_category"] == "not_sent"


def test_rate_limit_is_repeated_because_the_provider_did_not_act(monkeypatch, tmp_path):
    module = ScriptedHttpx([
        FakeResponse(429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1", "parameters": {"retry_after": 1}}),
        FakeResponse(200, {"ok": True, "result": {"message_id": 5}}),
    ])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch_once(tmp_path, "telegram", _telegram(monkeypatch))

    assert len(module.calls) == 2
    assert row["status"] == "sent"


def test_long_provider_backoff_is_handed_to_the_outbox_not_slept_through(monkeypatch, tmp_path):
    module = ScriptedHttpx([FakeResponse(429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 40", "parameters": {"retry_after": 40}})])
    monkeypatch.setattr(http_client, "httpx", module)

    async def send():
        return await _telegram(monkeypatch).send_text("42", "hi")

    with pytest.raises(ChannelHttpError) as exc:
        asyncio.run(send())
    assert len(module.calls) == 1
    assert exc.value.retry_after_seconds == 40
    assert classify_channel_error(exc.value).category == "rate_limit"


def test_blocked_recipient_dead_letters_without_an_auth_alert(monkeypatch, tmp_path):
    module = ScriptedHttpx([FakeResponse(403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"})])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch_once(tmp_path, "telegram", _telegram(monkeypatch))

    assert row["status"] == "dead_letter"
    assert row["error_category"] == "recipient_unavailable"
    assert classify_channel_error(ChannelHttpError("x", status_code=403, channel="telegram", response_json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"})).alert is False


def test_idempotent_get_is_still_retried_after_a_timeout(monkeypatch):
    module = ScriptedHttpx([ReadTimeout("timed out"), FakeResponse(503), FakeResponse(200, {"ok": True})])
    monkeypatch.setattr(http_client, "httpx", module)

    result = asyncio.run(ChannelHttpClient().get("https://provider.test/status"))

    assert result.status_code == 200
    assert len(module.calls) == 3


def test_line_retries_with_one_retry_key_and_treats_accepted_409_as_sent(monkeypatch, tmp_path):
    # LINE deduplicates on X-Line-Retry-Key, so a timed-out push may be repeated;
    # the repeat's 409 with x-line-accepted-request-id means the first one landed.
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "line-token")
    module = ScriptedHttpx([
        ReadTimeout("timed out"),
        FakeResponse(409, {"message": "The retry key is already accepted", "sentMessages": [{"id": 461230966842064897}]}, {"x-line-accepted-request-id": "accepted-req-1"}),
    ])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch_once(tmp_path, "line", LineChannel(LineConfig(enabled=True)), recipient="U123")

    keys = {call["headers"]["X-Line-Retry-Key"] for call in module.calls}
    assert len(module.calls) == 2 and len(keys) == 1
    assert str(uuid.UUID(keys.pop())) is not None
    assert row["status"] == "sent"
    assert row["provider_request_id"] == "accepted-req-1"
    assert row["provider_message_id"] == "461230966842064897"


def test_line_5xx_goes_back_to_the_outbox_for_a_keyed_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "line-token")
    module = ScriptedHttpx([FakeResponse(500)] * 3)
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch_once(tmp_path, "line", LineChannel(LineConfig(enabled=True)), recipient="U123")

    # Native dedupe: retrying with the same key cannot double-send, so this is
    # a scheduled retry rather than an ambiguous row.
    assert row["status"] == "retry"
    assert row["error_category"] == "provider_5xx"


def test_line_push_records_the_sent_message_id(monkeypatch, tmp_path):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "line-token")
    module = ScriptedHttpx([FakeResponse(200, {"sentMessages": [{"id": "461230966842064897", "quoteToken": "q"}]})])
    monkeypatch.setattr(http_client, "httpx", module)

    row = _dispatch_once(tmp_path, "line", LineChannel(LineConfig(enabled=True)), recipient="U123")

    assert row["status"] == "sent"
    assert row["provider_message_id"] == "461230966842064897"
