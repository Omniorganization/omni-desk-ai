"""Provider-facing responses and Telegram polling, end to end.

QQ only accepts a callback URL that answers op 13 with the signed
plain_token, and expects an op 12 ACK for each push; Discord marks an
interaction failed unless it gets an interaction response. Telegram polling
was advertised but missing; polled updates must take the webhook's path
(allowlist, durable queue) and be confirmed only after they were queued.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnidesk_agent.channels import http_client
from omnidesk_agent.channels.http_client import ChannelHttpError
from omnidesk_agent.channels.native_messaging import DiscordChannel, QQChannel, qq_signing_key
from omnidesk_agent.channels.telegram import TelegramChannel
from omnidesk_agent.channels.telegram_polling import TelegramPoller
from omnidesk_agent.config import AppConfig, TelegramConfig
from omnidesk_agent.server_routes.webhook_guard import WebhookGuard
from omnidesk_agent.server_routes.webhook_routes import register_webhook_routes


class _Queue:
    def __init__(self):
        self.messages = []

    def enqueue(self, message, source_key):
        self.messages.append((message, source_key))
        return {"job_id": f"job-{len(self.messages)}", "created": True}


def _client(cfg: AppConfig, **adapters):
    rt = SimpleNamespace(adapters=adapters, job_queue=_Queue(), metrics=None)
    app = FastAPI()
    register_webhook_routes(app, cfg, rt, WebhookGuard(cfg, rt))
    return TestClient(app), rt.job_queue


QQ_SECRET = "qq-app-secret-for-tests-0123"


def test_qq_route_answers_op13_and_acks_pushes(monkeypatch):
    for name in ("QQ_BOT_APP_ID", "QQ_BOT_TOKEN", "QQ_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QQ_BOT_SECRET", QQ_SECRET)
    cfg = AppConfig()
    cfg.channels.qq.enabled = True
    client, queue = _client(cfg, qq=QQChannel(cfg.channels.qq))

    validation = client.post("/webhooks/qq", content=json.dumps({"op": 13, "d": {"plain_token": "tok123", "event_ts": "1725442341"}}))
    assert validation.status_code == 200
    answer = validation.json()
    assert set(answer) == {"plain_token", "signature"}
    qq_signing_key(QQ_SECRET).public_key().verify(bytes.fromhex(answer["signature"]), b"1725442341tok123")

    push = {"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"id": "m1", "content": "hi", "author": {"user_openid": "U1"}}}
    body = json.dumps(push).encode()
    signature = qq_signing_key(QQ_SECRET).sign(b"1725442400" + body).hex()
    acked = client.post("/webhooks/qq", content=body, headers={"X-Signature-Ed25519": signature, "X-Signature-Timestamp": "1725442400"})
    assert acked.status_code == 200 and acked.json() == {"op": 12, "d": 0}
    assert [m.thread_id for m, _ in queue.messages] == ["c2c:U1"]

    unsigned = client.post("/webhooks/qq", content=json.dumps({**push, "d": {**push["d"], "id": "m2"}}))
    assert unsigned.status_code == 403
    assert len(queue.messages) == 1


def test_discord_route_answers_commands_within_the_interaction_contract(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex())
    cfg = AppConfig()
    cfg.channels.discord.enabled = True
    client, queue = _client(cfg, discord=DiscordChannel(cfg.channels.discord))

    def post(payload):
        body = json.dumps(payload).encode()
        return client.post("/webhooks/discord", content=body, headers={"X-Signature-Ed25519": key.sign(b"1700000000" + body).hex(), "X-Signature-Timestamp": "1700000000"})

    assert post({"type": 1, "id": "ping"}).json() == {"type": 1}
    command = post({"type": 2, "id": "i1", "channel_id": "c1", "member": {"user": {"id": "u1"}}, "data": {"name": "ask"}})
    assert command.json()["type"] == 4
    assert len(queue.messages) == 1


# ----------------------------------------------------------------------------- Telegram polling


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": []}
        self.text = json.dumps(self._body)
        self.headers = {}

    def json(self):
        return self._body


class ScriptedHttpx:
    class TimeoutException(Exception):
        pass

    class NetworkError(Exception):
        pass

    class TransportError(Exception):
        pass

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.timeouts = []

    def AsyncClient(self, timeout):
        module = self
        module.timeouts.append(timeout)

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, headers=None, params=None, json=None, data=None):
                module.calls.append({"method": method, "url": url, "params": dict(params or {})})
                return module.script.pop(0)

        return Client()


def _update(update_id, user_id, text="hi"):
    return {"update_id": update_id, "message": {"message_id": update_id, "from": {"id": user_id}, "chat": {"id": user_id}, "text": text}}


def test_poller_delivers_allowlisted_messages_and_confirms_offsets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "b" * 35)
    module = ScriptedHttpx([
        FakeResponse(200, {"ok": True, "result": [_update(10, 42, "hello"), _update(11, 99, "not allowlisted")]}),
        FakeResponse(200, {"ok": True, "result": []}),
    ])
    monkeypatch.setattr(http_client, "httpx", module)
    delivered = []
    poller = TelegramPoller(TelegramChannel(TelegramConfig(enabled=True, allowed_user_ids=[42])), delivered.append, timeout_seconds=30)

    assert asyncio.run(poller.poll_once()) == 2
    asyncio.run(poller.poll_once())

    assert [m.text for m in delivered] == ["hello"]
    assert "offset" not in module.calls[0]["params"] and module.calls[1]["params"]["offset"] == 12
    assert json.loads(module.calls[0]["params"]["allowed_updates"]) == ["message", "business_message"]
    assert module.calls[0]["params"]["timeout"] == 30 and module.timeouts[0] > 30  # HTTP timeout outlasts the long poll


def test_poller_does_not_confirm_an_update_whose_delivery_failed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "b" * 35)
    module = ScriptedHttpx([FakeResponse(200, {"ok": True, "result": [_update(20, 42)]})])
    monkeypatch.setattr(http_client, "httpx", module)

    def failing_deliver(_message):
        raise RuntimeError("job queue unavailable")

    poller = TelegramPoller(TelegramChannel(TelegramConfig(enabled=True, allowed_user_ids=[42])), failing_deliver)
    with pytest.raises(RuntimeError):
        asyncio.run(poller.poll_once())
    assert poller.offset is None  # update 20 is fetched again on the next poll


def test_poller_reports_webhook_conflict_as_a_config_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "b" * 35)
    module = ScriptedHttpx([FakeResponse(409, {"ok": False, "error_code": 409, "description": "Conflict: can't use getUpdates method while webhook is active; use deleteWebhook to delete the webhook first"})])
    monkeypatch.setattr(http_client, "httpx", module)
    poller = TelegramPoller(TelegramChannel(TelegramConfig(enabled=True)), lambda m: None)

    with pytest.raises(ChannelHttpError) as exc:
        asyncio.run(poller.poll_once())
    from omnidesk_agent.channels.provider_errors import classify_channel_error

    assert classify_channel_error(exc.value).category == "config_error"


def test_daemon_runs_the_poller_only_in_polling_mode(monkeypatch, tmp_path):
    from omnidesk_agent.daemon import OmniDeskRuntime

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "b" * 35)
    started = []

    async def fake_run_forever(self, stop):
        started.append(self)
        await stop.wait()

    monkeypatch.setattr(TelegramPoller, "run_forever", fake_run_forever)

    def runtime(mode):
        root = tmp_path / mode
        cfg = AppConfig()
        cfg.workspace.root = root / "workspace"
        cfg.workspace.memory_db = root / "memory.sqlite3"
        cfg.workspace.skills_dirs = [root / "skills"]
        cfg.workspace.plugins_dirs = [root / "plugins"]
        cfg.permissions.audit_log = root / "audit.log"
        cfg.learning.growth_plan_file = root / "growth.json"
        cfg.channels.gmail.credentials_file = root / "google" / "credentials.json"
        cfg.channels.gmail.token_file = root / "google" / "token.json"
        cfg.channels.telegram.enabled = True
        cfg.channels.telegram.mode = mode
        return OmniDeskRuntime(cfg)

    async def run_case():
        for mode in ("webhook", "polling"):
            rt = runtime(mode)
            await rt.start()
            await asyncio.sleep(0)
            await rt.aclose()

    asyncio.run(run_case())
    assert len(started) == 1


def test_whatsapp_status_callbacks_get_their_own_rate_bucket_and_event_ids():
    # Status callbacks have no "from"; they all landed in the "unknown" bucket
    # (60/min across every customer) and could be throttled with 403.
    from omnidesk_agent.channels.whatsapp_cloud import WhatsAppCloudChannel
    from omnidesk_agent.config import WhatsAppCloudConfig

    channel = WhatsAppCloudChannel(WhatsAppCloudConfig())

    def status(state):
        return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "10950"}, "statuses": [{"id": "wamid.1", "status": state, "timestamp": "1700000000", "recipient_id": "1555"}]}}]}]}

    sent, delivered = channel.extract_envelope(status("sent")), channel.extract_envelope(status("delivered"))
    assert sent.source_key == "1555" and sent.message_id == "status:wamid.1:sent"
    assert delivered.message_id != sent.message_id
    assert channel.parse_webhook(status("read")) == []  # statuses are not tasks
