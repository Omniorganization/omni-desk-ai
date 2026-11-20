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
