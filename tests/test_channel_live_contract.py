"""Recorded-fixture contract tests for the live channel contract runner.

CI cannot reach Telegram, so these tests replay provider responses (fixtures
under tests/fixtures/channels/telegram) through the same runner the
`Channel Live Contract` workflow uses. They pin what a live run must prove
(auth, no competing webhook, a sent message id, a reply from the allowlisted
test user carrying this run's code) and that evidence never contains raw chat
or user ids or a pass the run did not observe.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from omnidesk_agent.channels.http_client import ChannelHttpError, ChannelHttpResult

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "channels" / "telegram"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_channel_live_contract", ROOT / "scripts" / "run_channel_live_contract.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RecordedTelegram:
    """Answers Bot API calls from fixtures; ``reply`` is what the test user sends back."""

    def __init__(self, *, reply_text=None, reply_from=5000000002, webhook_url="", stale_updates=0):
        self.reply_text = reply_text
        self.reply_from = reply_from
        self.webhook_url = webhook_url
        self.stale_updates = stale_updates
        self.sent_text = None
        self.calls: list[str] = []

    def _result(self, fixture: dict[str, Any]) -> ChannelHttpResult:
        status = int(fixture.get("status_code", 200))
        body = fixture["response"]
        if status >= 400:
            raise ChannelHttpError(f"channel provider HTTP {status}", status_code=status, channel="telegram", response_json=body)
        return ChannelHttpResult(status, body)

    async def get(self, url: str, *, params=None, channel="telegram", headers=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)
        if method == "getMe":
            return self._result(_fixture("getMe"))
        if method == "getWebhookInfo":
            info = copy.deepcopy(_fixture("getWebhookInfo"))
            info["response"]["result"]["url"] = self.webhook_url
            return self._result(info)
        if method == "getUpdates":
            if self.stale_updates:
                self.stale_updates -= 1
                stale = copy.deepcopy(_fixture("getUpdates_reply"))
                stale["response"]["result"][0]["update_id"] = 900000001 + self.stale_updates
                stale["response"]["result"][0]["message"]["text"] = "an old message from before the run"
                return self._result(stale)
            if self.sent_text is None or self.reply_text is None:
                return ChannelHttpResult(200, {"ok": True, "result": []})
            reply = copy.deepcopy(_fixture("getUpdates_reply"))
            message = reply["response"]["result"][0]["message"]
            code = self.sent_text.split("code ", 1)[1].split(" ", 1)[0]
            message["text"] = self.reply_text.replace("__CODE__", code)
            message["from"]["id"] = self.reply_from
            self.reply_text = None  # delivered once
            return self._result(reply)
        raise AssertionError(f"unexpected GET {method}")

    async def post(self, url: str, *, json=None, **kwargs):
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)
        assert method == "sendMessage"
        self.sent_text = json["text"]
        return self._result(_fixture("sendMessage"))


@pytest.fixture
def runner(monkeypatch):
    module = _load_runner()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "c" * 35)
    monkeypatch.setenv("TELEGRAM_LIVE_CHAT_ID", "5000000002")
    monkeypatch.setenv("TELEGRAM_LIVE_SENDER_ID", "5000000002")
    for name, value in {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "Omniorganization/omni-desk-ai", "GITHUB_RUN_ID": "123", "GITHUB_SHA": "abc"}.items():
        monkeypatch.setenv(name, value)
    return module


def _run(module, provider, **kwargs):
    return asyncio.run(module.run_telegram(mode=kwargs.get("mode", "send-receive"), reply_timeout_seconds=kwargs.get("timeout", 60), http=provider, poll_http=provider))


def test_send_receive_passes_only_with_the_allowlisted_reply_carrying_this_runs_code(runner):
    provider = RecordedTelegram(reply_text="here you go: __CODE__", stale_updates=2)

    result = _run(runner, provider)
    evidence = runner.build_evidence(result, channel="telegram", mode="send-receive", error=None)

    checks = evidence["checks"]
    assert evidence["status"] == "passed" and evidence["run_url"] == "https://github.com/Omniorganization/omni-desk-ai/actions/runs/123"
    assert checks["auth"]["bot_username"] == "omnidesk_test_bot"
    assert checks["drain"]["pending_updates_confirmed"] == 2  # old messages never count as the reply
    assert checks["send"]["provider_message_id"] == "4242"
    assert checks["receive"]["provider_message_id"] == "4243"
    serialized = json.dumps(evidence)
    assert "5000000002" not in serialized and "7000000001" not in serialized  # ids only as digests
    assert provider.calls.index("sendMessage") > provider.calls.index("getWebhookInfo")


def test_reply_from_another_user_does_not_count(runner, monkeypatch):
    provider = RecordedTelegram(reply_text="__CODE__", reply_from=5999999999)
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))

    with pytest.raises(runner.LiveContractFailure, match="no reply"):
        _run(runner, provider, timeout=60)


def test_reply_without_the_code_does_not_count(runner, monkeypatch):
    provider = RecordedTelegram(reply_text="ok")
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))

    with pytest.raises(runner.LiveContractFailure, match="no reply"):
        _run(runner, provider, timeout=60)


def test_a_bot_with_a_webhook_is_refused_before_anything_is_sent(runner):
    provider = RecordedTelegram(reply_text="__CODE__", webhook_url="https://example.test/hook")

    with pytest.raises(runner.LiveContractFailure, match="webhook"):
        _run(runner, provider)
    assert "sendMessage" not in provider.calls


def test_send_only_records_no_receive_pass(runner):
    result = _run(runner, RecordedTelegram(), mode="send-only")
    assert result["checks"]["send"]["passed"] is True
    assert result["checks"]["receive"]["passed"] is None


def test_failed_run_writes_failed_evidence_and_exit_code(runner, monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_LIVE_CHAT_ID")
    output = tmp_path / "telegram.json"

    code = runner.main(["--channel", "telegram", "--output", str(output)])

    evidence = json.loads(output.read_text())
    assert code == 1 and evidence["status"] == "failed" and "TELEGRAM_LIVE_CHAT_ID" in evidence["error"]
    assert evidence["checks"] == {}


def test_webhook_conflict_fixture_classifies_as_config_error():
    from omnidesk_agent.channels.provider_errors import classify_channel_error

    fixture = _fixture("getUpdates_conflict")
    error = ChannelHttpError("channel provider HTTP 409", status_code=409, channel="telegram", response_json=fixture["response"])
    assert classify_channel_error(error).category == "config_error"


# ----------------------------------------------------------------------------- Slack (Socket Mode)

SLACK = ROOT / "tests" / "fixtures" / "channels" / "slack"


def _slack(name: str) -> Any:
    return json.loads((SLACK / f"{name}.json").read_text(encoding="utf-8"))


class FakeSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[dict] = []

    async def recv(self):
        if not self.frames:
            await asyncio.sleep(3600)
        item = self.frames.pop(0)
        return item() if callable(item) else json.dumps(item)

    async def send(self, data):
        self.sent.append(json.loads(data))


class FakeConnect:
    def __init__(self, sockets):
        self.sockets = list(sockets)
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        socket = self.sockets.pop(0)

        class Ctx:
            async def __aenter__(self_inner):
                return socket

            async def __aexit__(self_inner, *exc):
                return False

        return Ctx()


class RecordedSlackWeb:
    def __init__(self):
        self.posted_text = None
        self.calls: list[str] = []

    async def post(self, url, **kwargs):
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)
        if method == "auth.test":
            return ChannelHttpResult(200, _slack("auth_test")["response"])
        if method == "apps.connections.open":
            assert kwargs["headers"]["Authorization"].startswith("Bearer xapp-")
            return ChannelHttpResult(200, _slack("apps_connections_open")["response"])
        if method == "chat.postMessage":
            self.posted_text = kwargs["json"]["text"]
            return ChannelHttpResult(200, _slack("chat_postMessage")["response"])
        raise AssertionError(method)


@pytest.fixture
def slack_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-placeholder")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-placeholder")
    monkeypatch.setenv("SLACK_LIVE_CHANNEL_ID", "C0000000001")
    monkeypatch.setenv("SLACK_LIVE_SENDER_ID", "U0000000002")


def _reply_frame(web, *, user="U0000000002", text_template="__CODE__"):
    def build():
        frame = copy.deepcopy(_slack("socket_frames")["reply"])
        code = web.posted_text.split("code ", 1)[1].split(" ", 1)[0]
        frame["payload"]["event"]["text"] = text_template.replace("__CODE__", code)
        frame["payload"]["event"]["user"] = user
        return json.dumps(frame)

    return build


def test_slack_send_receive_over_socket_mode(runner, slack_env):
    frames = _slack("socket_frames")
    web = RecordedSlackWeb()
    first = FakeSocket([frames["hello"], frames["bot_echo"], frames["disconnect"]])
    second = FakeSocket([frames["hello"], _reply_frame(web)])
    connect = FakeConnect([first, second])

    result = asyncio.run(runner.run_slack(mode="send-receive", reply_timeout_seconds=60, http=web, connect=connect))

    checks = result["checks"]
    # The message is posted only after the socket is up; our own echo and the
    # refresh disconnect do not end the run; the reply is parsed by the adapter.
    assert web.calls.index("chat.postMessage") > web.calls.index("apps.connections.open")
    assert len(connect.urls) == 2
    assert checks["send"]["provider_message_id"] == "1763683200.000100"
    assert checks["receive"]["provider_message_id"] == "00000000-0000-4000-8000-000000000001"
    assert checks["receive"]["event_id"] == "Ev0000000001"
    # Every envelope was acknowledged so Slack does not redeliver it.
    assert [ack["envelope_id"] for ack in first.sent + second.sent] == ["env-bot-echo", "env-reply"]
    assert "U0000000002" not in json.dumps(checks) and "C0000000001" not in json.dumps(checks)


def test_slack_reply_from_another_user_times_out_with_partial_checks(runner, slack_env, monkeypatch):
    frames = _slack("socket_frames")
    web = RecordedSlackWeb()
    connect = FakeConnect([FakeSocket([frames["hello"], _reply_frame(web, user="U0000000999")])])

    with pytest.raises(runner.LiveContractFailure) as exc:
        asyncio.run(runner.run_slack(mode="send-receive", reply_timeout_seconds=1, http=web, connect=connect))
    assert exc.value.checks["send"]["passed"] is True and "receive" not in exc.value.checks


def test_slack_send_only_needs_no_app_token(runner, slack_env, monkeypatch):
    monkeypatch.delenv("SLACK_APP_TOKEN")
    web = RecordedSlackWeb()
    result = asyncio.run(runner.run_slack(mode="send-only", reply_timeout_seconds=60, http=web))
    assert result["checks"]["send"]["passed"] is True and result["checks"]["receive"]["passed"] is None
    assert "apps.connections.open" not in web.calls


# ----------------------------------------------------------------------------- WhatsApp, Feishu


class RecordedProvider:
    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return ChannelHttpResult(200, self.responses[url.rsplit("/", 1)[-1].split("?")[0]])

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return ChannelHttpResult(200, self.responses[url.rsplit("/", 1)[-1].split("?")[0]])


def _shape(platform: str, name: str) -> Any:
    return json.loads((ROOT / "tests" / "fixtures" / "channels" / platform / f"{name}.json").read_text(encoding="utf-8"))["response"]


def test_whatsapp_send_only_sends_the_hello_world_template(runner, monkeypatch):
    monkeypatch.setenv("WHATSAPP_CLOUD_TOKEN", "wa-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "100000000000001")
    monkeypatch.setenv("WHATSAPP_LIVE_RECIPIENT", "15550000000")
    provider = RecordedProvider({"100000000000001": _shape("whatsapp", "phone_number"), "messages": _shape("whatsapp", "template_sent")})

    result = asyncio.run(runner.run_whatsapp(mode="send-only", reply_timeout_seconds=60, http=provider))

    send_body = provider.calls[1][2]["json"]
    assert send_body["type"] == "template" and send_body["template"] == {"name": "hello_world", "language": {"code": "en_US"}}
    assert result["checks"]["send"]["provider_message_id"].startswith("wamid.")
    assert "15550000000" not in json.dumps(result["checks"])
    with pytest.raises(runner.LiveContractFailure, match="public HTTPS webhook"):
        asyncio.run(runner.run_whatsapp(mode="send-receive", reply_timeout_seconds=60, http=provider))


def test_feishu_send_only_sends_json_string_content_to_the_open_id(runner, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_placeholder")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("FEISHU_LIVE_OPEN_ID", "ou_placeholder")
    provider = RecordedProvider({"internal": _shape("feishu", "tenant_access_token"), "messages": _shape("feishu", "message_sent")})

    result = asyncio.run(runner.run_feishu(mode="send-only", reply_timeout_seconds=60, http=provider))

    send = next(kwargs for method, url, kwargs in provider.calls if url.endswith("/im/v1/messages"))
    assert send["params"] == {"receive_id_type": "open_id"} and json.loads(send["json"]["content"])["text"].startswith("OmniDesk live contract")
    assert result["checks"]["send"]["provider_message_id"] == "om_placeholder000000000000000001"
