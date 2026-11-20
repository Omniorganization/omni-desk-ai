"""Gmail inbound through Cloud Pub/Sub push.

The catalog advertised Gmail as an inbound channel, but no route or poller
existed. A push request must be authenticated by the subscription's
Google-signed OIDC token; it only carries a history id, so new mail is read
with users.history.list from a durable cursor. A failed read must be retried
(503) without losing the cursor, and senders are matched by exact address,
because a substring match let a display name impersonate an allowed sender.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnidesk_agent.channels.gmail import GmailChannel, sender_address
from omnidesk_agent.channels.google_oidc import GooglePushJwtVerifier
from omnidesk_agent.config import AppConfig, GmailConfig
from omnidesk_agent.server_routes import gmail_push

AUDIENCE = "https://gateway.example.test/webhooks/gmail"
SA = "gmail-push@project.iam.gserviceaccount.com"


@pytest.fixture(autouse=True)
def _token_encryption_key(monkeypatch):
    # An enabled Gmail channel refuses to start without its token encryption key.
    monkeypatch.setenv("OMNIDESK_GMAIL_TOKEN_ENCRYPTION_KEY", "unit-test-gmail-token-key")


@pytest.fixture(scope="module")
def google_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(datetime.now(timezone.utc) - timedelta(days=1)).not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _token(key, kid="k1", **claims):
    now = int(time.time())
    body = {"iss": "https://accounts.google.com", "aud": AUDIENCE, "email": SA, "email_verified": True, "iat": now, "exp": now + 3600, "sub": "1"}
    body.update(claims)
    signing_input = f"{_b64(json.dumps({'alg': 'RS256', 'kid': kid, 'typ': 'JWT'}).encode())}.{_b64(json.dumps(body).encode())}"
    return signing_input + "." + _b64(key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()))


def _verifier(pem):
    return GooglePushJwtVerifier(AUDIENCE, SA, cert_fetcher=lambda refresh=False: {"k1": pem})


def test_push_token_must_be_google_signed_for_our_audience_and_service_account(google_key):
    key, pem = google_key
    verifier = _verifier(pem)
    assert verifier.verify(_token(key))["email"] == SA
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in (
        _token(key, iss="https://evil.example"),
        _token(key, aud="https://other.example/hook"),
        _token(key, email="someone@evil.example"),
        _token(key, email_verified=False),
        _token(key, exp=int(time.time()) - 3600),
        _token(key, kid="unknown"),
        _token(other),
        "not-a-jwt",
        "",
    ):
        with pytest.raises(PermissionError):
            verifier.verify(token)


def test_exact_sender_match_defeats_display_name_spoofing(tmp_path):
    channel = GmailChannel(GmailConfig(allowed_senders=["boss@company.com"], token_file=tmp_path / "gmail_token.json"))
    assert sender_address('"Boss" <BOSS@company.com>') == "boss@company.com"
    assert channel.sender_allowed("Boss <boss@company.com>")
    assert not channel.sender_allowed("boss@company.com <attacker@evil.test>")
    assert not channel.sender_allowed("attacker@evil.test (boss@company.com)")


def _push(history_id=1200, message_id="pubsub-1"):
    data = base64.b64encode(json.dumps({"emailAddress": "inbox@company.com", "historyId": history_id}).encode()).decode()
    return {"message": {"data": data, "messageId": message_id, "publishTime": "2026-11-21T00:00:00Z"}, "subscription": "projects/p/subscriptions/s"}


class FakeGmailService:
    """Records history.list / messages.get the way googleapiclient chains them."""

    def __init__(self, history, messages, fail=False):
        self.history_pages = list(history)
        self.messages = messages
        self.fail = fail
        self.history_calls = []

    def users(self):
        service = self

        class Request:
            def __init__(self, fn):
                self.fn = fn

            def execute(self):
                return self.fn()

        class Users:
            def history(self_inner):
                class History:
                    def list(self_h, **kwargs):
                        service.history_calls.append(kwargs)
                        if service.fail:
                            return Request(lambda: (_ for _ in ()).throw(RuntimeError("token expired")))
                        return Request(lambda: service.history_pages.pop(0))

                return History()

            def messages(self_inner):
                class Messages:
                    def get(self_m, **kwargs):
                        return Request(lambda: service.messages[kwargs["id"]])

                return Messages()

        return Users()


def _message(mid, sender, subject):
    return {"id": mid, "threadId": f"t-{mid}", "snippet": "body text", "payload": {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]}}


def _client(tmp_path, pem, service):
    cfg = AppConfig()
    cfg.channels.gmail.enabled = True
    cfg.channels.gmail.token_file = tmp_path / "google" / "gmail_token.json"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.allowed_senders = ["boss@company.com"]
    cfg.channels.gmail.push_audience = AUDIENCE
    cfg.channels.gmail.push_service_account_email = SA
    adapter = GmailChannel(cfg.channels.gmail)
    adapter.oauth.build_service = lambda: service
    queued = []
    job_queue = SimpleNamespace(enqueue=lambda message, source_key: queued.append(message) or {"job_id": f"j{len(queued)}", "created": True})
    rt = SimpleNamespace(adapters={"gmail": adapter}, job_queue=job_queue, metrics=None)
    original = gmail_push.GooglePushJwtVerifier
    gmail_push.GooglePushJwtVerifier = lambda audience, email: GooglePushJwtVerifier(audience, email, cert_fetcher=lambda refresh=False: {"k1": pem})
    try:
        app = FastAPI()
        gmail_push.register_gmail_push_route(app, cfg, rt)
    finally:
        gmail_push.GooglePushJwtVerifier = original
    return TestClient(app), adapter, queued


def test_push_reads_new_inbox_mail_from_the_cursor_and_queues_allowed_senders(tmp_path, google_key):
    key, pem = google_key
    service = FakeGmailService(
        history=[{"historyId": "1300", "history": [{"messagesAdded": [{"message": {"id": "m1"}}, {"message": {"id": "m2"}}]}]}],
        messages={"m1": _message("m1", "Boss <boss@company.com>", "Quarterly"), "m2": _message("m2", "boss@company.com <attacker@evil.test>", "Pay this")},
    )
    client, adapter, queued = _client(tmp_path, pem, service)
    headers = {"Authorization": "Bearer " + _token(key)}

    first = client.post("/webhooks/gmail", json=_push(1200), headers=headers)
    assert first.json() == {"ok": True, "queued": 0}  # the first notification only sets the cursor
    assert adapter.load_history_id() == 1200

    second = client.post("/webhooks/gmail", json=_push(1250, "pubsub-2"), headers=headers)
    assert second.status_code == 200 and second.json()["queued"] == 1
    assert service.history_calls[0]["startHistoryId"] == "1200" and service.history_calls[0]["labelId"] == "INBOX"
    assert [m.message_id for m in queued] == ["m1"] and queued[0].sender_id == "boss@company.com"
    assert adapter.load_history_id() == 1300


def test_push_without_a_valid_oidc_token_is_refused(tmp_path, google_key):
    key, pem = google_key
    client, adapter, queued = _client(tmp_path, pem, FakeGmailService([], {}))
    for headers in ({}, {"Authorization": "Bearer " + _token(key, aud="https://elsewhere.example")}):
        assert client.post("/webhooks/gmail", json=_push(), headers=headers).status_code == 403
    assert adapter.load_history_id() is None and queued == []


def test_failed_gmail_read_is_retried_without_moving_the_cursor(tmp_path, google_key):
    key, pem = google_key
    client, adapter, queued = _client(tmp_path, pem, FakeGmailService([], {}, fail=True))
    adapter.save_history_id(1200)

    response = client.post("/webhooks/gmail", json=_push(1250), headers={"Authorization": "Bearer " + _token(key)})

    assert response.status_code == 503  # Pub/Sub redelivers non-2xx
    assert adapter.load_history_id() == 1200


def test_route_is_closed_until_push_auth_is_configured(tmp_path):
    cfg = AppConfig()
    cfg.channels.gmail.enabled = True
    cfg.channels.gmail.token_file = tmp_path / "gmail_token.json"
    rt = SimpleNamespace(adapters={"gmail": GmailChannel(cfg.channels.gmail)}, job_queue=None, metrics=None)
    app = FastAPI()
    gmail_push.register_gmail_push_route(app, cfg, rt)
    assert TestClient(app).post("/webhooks/gmail", json=_push()).status_code == 404


def test_malformed_notifications_are_rejected():
    for payload in ({}, {"message": {"data": "%%%", "messageId": "1"}}, {"message": {"data": base64.b64encode(b'{"emailAddress":"a@b"}').decode(), "messageId": "1"}}):
        with pytest.raises(ValueError):
            GmailChannel.parse_push(payload)
    assert asyncio.iscoroutinefunction(GmailChannel.fetch_new_messages)
