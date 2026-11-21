"""Gmail inbound through Cloud Pub/Sub push.

The catalog advertised Gmail as an inbound channel, but no route or poller
existed. A push request must be authenticated by the subscription's
Google-signed OIDC token; it only carries a history id, so new mail is read
with users.history.list from a durable cursor. No mail may be lost on the
way: the cursor moves only after the messages it covers are queued, a backlog
larger than one request still moves forward, and errors that retrying cannot
fix (expired history, a deleted message) must not wedge the channel behind an
endless 503. Senders are matched by exact address, because a substring match
let a display name impersonate an allowed sender, and production refuses a
push setup that would admit any sender.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
import threading
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

from omnidesk_agent.channels.gmail import MAX_HISTORY_RECORDS_PER_PUSH, GmailChannel, sender_address
from omnidesk_agent.channels.google_oidc import GooglePushJwtVerifier
from omnidesk_agent.config import AppConfig, GmailConfig
from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.server_routes import gmail_push
from omnidesk_agent.validation.production import validate_production_config

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
        _token(key, iat=int(time.time()) + 3600),  # issued in the future
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


def _http_error(status):
    import httplib2
    from googleapiclient.errors import HttpError

    content = json.dumps({"error": {"code": status, "message": "Requested entity was not found."}}).encode()
    return HttpError(httplib2.Response({"status": str(status)}), content, uri="https://gmail.googleapis.com/gmail/v1/users/me/history")


def _message(mid, sender="Boss <boss@company.com>", subject="Hello"):
    return {"id": mid, "threadId": f"t-{mid}", "snippet": "body text", "payload": {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]}}


class FakeGmailService:
    """Gmail as googleapiclient chains it.

    history.list answers like Gmail: records after startHistoryId in
    ascending id order, at most maxResults per page, a nextPageToken while
    more remain, and the mailbox's current historyId; a startHistoryId older
    than ``expired_before`` gets 404.
    """

    def __init__(self, records=(), messages=None, *, current=None, expired_before=None, history_error=None, deleted=(), watch_history_id="7000"):
        self.records = list(records)
        self.messages = dict(messages or {})
        self.current = current
        self.expired_before = expired_before
        self.history_error = history_error
        self.deleted = set(deleted)
        self.watch_history_id = watch_history_id
        self.history_calls, self.get_calls, self.watch_calls, self.execute_threads = [], [], [], []

    def receive(self, record_id, mid, sender="Boss <boss@company.com>"):
        self.records.append({"id": str(record_id), "messagesAdded": [{"message": {"id": mid}}]})
        self.messages[mid] = _message(mid, sender)

    def _history(self, startHistoryId, maxResults=100, pageToken=None, **_):
        if self.history_error is not None:
            raise self.history_error
        start = int(startHistoryId)
        if self.expired_before is not None and start < self.expired_before:
            raise _http_error(404)
        after = [record for record in self.records if int(record["id"]) > start]
        offset = int(pageToken or 0)
        response = {"historyId": str(self.current or max([start] + [int(r["id"]) for r in self.records]))}
        if after[offset:offset + maxResults]:
            response["history"] = after[offset:offset + maxResults]
        if offset + maxResults < len(after):
            response["nextPageToken"] = str(offset + maxResults)
        return response

    def _get(self, id, **_):
        self.get_calls.append(id)
        if id in self.deleted:
            raise _http_error(404)
        return self.messages[id]

    def users(self):
        service = self

        class Request:
            def __init__(self, fn):
                self.fn = fn

            def execute(self):
                service.execute_threads.append(threading.get_ident())
                return self.fn()

        class Users:
            def history(self_inner):
                class History:
                    def list(self_h, **kwargs):
                        service.history_calls.append(kwargs)
                        return Request(lambda: service._history(**kwargs))

                return History()

            def messages(self_inner):
                class Messages:
                    def get(self_m, **kwargs):
                        return Request(lambda: service._get(**kwargs))

                    def send(self_m, **kwargs):
                        return Request(lambda: {"id": "sent-1"})

                    def list(self_m, **kwargs):
                        return Request(lambda: {"messages": [{"id": "m1"}]})

                return Messages()

            def watch(self_inner, **kwargs):
                service.watch_calls.append(kwargs)
                return Request(lambda: {"historyId": service.watch_history_id, "expiration": "1"})

        return Users()


class FlakyQueue:
    """The real job queue; the first enqueue of each id in ``fail_once`` fails like a locked database."""

    def __init__(self, path, fail_once=()):
        self.queue = JobQueue(path)
        self.path = path
        self.fail_once = set(fail_once)

    def enqueue(self, message, source_key=None):
        if message.message_id in self.fail_once:
            self.fail_once.discard(message.message_id)
            raise sqlite3.OperationalError("database is locked")
        return self.queue.enqueue(message, source_key=source_key)

    def queued(self):
        with sqlite3.connect(self.path) as con:
            return [JobQueue.message_from_payload(row[0]) for row in con.execute("SELECT payload_json FROM jobs ORDER BY rowid")]


class Metrics:
    def __init__(self):
        self.counts, self.labels = {}, {}

    def inc(self, name, value=1, **labels):
        self.counts[name] = self.counts.get(name, 0) + value
        self.labels[name] = labels


def _client(tmp_path, pem, service, *, queue=None, enabled=True):
    cfg = AppConfig()
    cfg.channels.gmail.enabled = enabled
    cfg.channels.gmail.token_file = tmp_path / "google" / "gmail_token.json"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.allowed_senders = ["boss@company.com"]
    cfg.channels.gmail.push_audience = AUDIENCE
    cfg.channels.gmail.push_service_account_email = SA
    adapter = GmailChannel(cfg.channels.gmail)
    adapter.oauth.build_service = lambda: service
    queue = queue or FlakyQueue(tmp_path / "jobs.db")
    rt = SimpleNamespace(adapters={"gmail": adapter}, job_queue=queue, metrics=Metrics())
    original = gmail_push.GooglePushJwtVerifier
    gmail_push.GooglePushJwtVerifier = lambda audience, email: GooglePushJwtVerifier(audience, email, cert_fetcher=lambda refresh=False: {"k1": pem})
    try:
        app = FastAPI()
        gmail_push.register_gmail_push_route(app, cfg, rt)
    finally:
        gmail_push.GooglePushJwtVerifier = original
    return TestClient(app, raise_server_exceptions=False), adapter, queue, rt.metrics


def _auth(key):
    return {"Authorization": "Bearer " + _token(key)}


def test_push_reads_new_inbox_mail_from_the_cursor_and_queues_allowed_senders(tmp_path, google_key):
    key, pem = google_key
    service = FakeGmailService(current=1300)
    service.receive(1201, "m1", "Boss <boss@company.com>")
    service.receive(1202, "m2", "boss@company.com <attacker@evil.test>")
    client, adapter, queue, _metrics = _client(tmp_path, pem, service)

    first = client.post("/webhooks/gmail", json=_push(1200), headers=_auth(key))
    assert first.json()["queued"] == 0  # the first notification only sets the cursor
    assert adapter.load_history_id() == 1200

    second = client.post("/webhooks/gmail", json=_push(1250, "pubsub-2"), headers=_auth(key))
    assert second.status_code == 200 and second.json()["queued"] == 1
    call = service.history_calls[0]
    assert call["startHistoryId"] == "1200" and call["labelId"] == "INBOX" and call["maxResults"] == MAX_HISTORY_RECORDS_PER_PUSH
    assert [(m.message_id, m.sender_id) for m in queue.queued()] == [("m1", "boss@company.com")]
    assert adapter.load_history_id() == 1300


def test_enqueue_failure_keeps_the_cursor_so_the_redelivery_queues_the_rest(tmp_path, google_key):
    # Saving the cursor before queueing lost the failing message and every one
    # after it: Pub/Sub's redelivery then read history from the moved cursor.
    key, pem = google_key
    service = FakeGmailService(current=1300)
    for record_id, mid in ((1201, "m1"), (1202, "m2"), (1203, "m3")):
        service.receive(record_id, mid)
    queue = FlakyQueue(tmp_path / "jobs.db", fail_once={"m2"})
    client, adapter, queue, _metrics = _client(tmp_path, pem, service, queue=queue)
    adapter.save_history_id(1200)

    failed = client.post("/webhooks/gmail", json=_push(1300), headers=_auth(key))
    assert failed.status_code == 500
    assert adapter.load_history_id() == 1200 and [m.message_id for m in queue.queued()] == ["m1"]

    redelivered = client.post("/webhooks/gmail", json=_push(1300), headers=_auth(key))
    assert redelivered.status_code == 200
    assert redelivered.json() == {"ok": True, "queued": 2, "duplicates": 1, "more": False}  # m1 again is deduplicated
    assert [m.message_id for m in queue.queued()] == ["m1", "m2", "m3"]
    assert adapter.load_history_id() == 1300


def test_a_notification_that_finishes_late_cannot_move_the_cursor_back(tmp_path):
    # Concurrent notifications can finish out of order; each has queued
    # everything up to its own cursor, so the stored one only moves forward.
    channel = GmailChannel(GmailConfig(token_file=tmp_path / "gmail_token.json"))
    assert channel.advance_history_id(1300) and channel.load_history_id() == 1300
    assert not channel.advance_history_id(1250) and channel.load_history_id() == 1300
    assert not channel.advance_history_id(None) and channel.load_history_id() == 1300


def test_backlog_larger_than_one_request_is_read_forward_across_notifications(tmp_path, google_key):
    # With a fixed start and a page cap the cursor never moved, so a backlog
    # past the cap stalled the channel for good while answering 200.
    key, pem = google_key
    service = FakeGmailService()
    backlog = 2 * MAX_HISTORY_RECORDS_PER_PUSH + 10
    for n in range(1, backlog + 1):
        service.receive(1000 + n, f"m{n}")
    client, adapter, queue, _metrics = _client(tmp_path, pem, service)
    adapter.save_history_id(1000)

    gets_per_push, bodies = [], []
    for push in range(3):
        if push == 1:
            service.receive(1000 + backlog + 1, "late")  # new mail arrives meanwhile
        before = len(service.get_calls)
        response = client.post("/webhooks/gmail", json=_push(2000 + push, f"pubsub-{push}"), headers=_auth(key))
        assert response.status_code == 200
        bodies.append(response.json())
        gets_per_push.append(len(service.get_calls) - before)

    assert [call["startHistoryId"] for call in service.history_calls] == [
        "1000", str(1000 + MAX_HISTORY_RECORDS_PER_PUSH), str(1000 + 2 * MAX_HISTORY_RECORDS_PER_PUSH)
    ]
    assert [body["more"] for body in bodies] == [True, True, False]
    assert max(gets_per_push) <= MAX_HISTORY_RECORDS_PER_PUSH  # bounded work inside the ack deadline
    ids = [m.message_id for m in queue.queued()]
    assert ids == [f"m{n}" for n in range(1, backlog + 1)] + ["late"]
    assert adapter.load_history_id() == 1000 + backlog + 1


def test_expired_history_restarts_from_the_notification_and_reports_the_gap(tmp_path, google_key, caplog):
    # Gmail answers a cursor older than its history window with 404. Retrying
    # (503) could never succeed, so inbound stayed wedged until someone deleted
    # the state file by hand.
    key, pem = google_key
    service = FakeGmailService(expired_before=8000)
    client, adapter, queue, metrics = _client(tmp_path, pem, service)
    adapter.save_history_id(5)
    caplog.set_level(logging.INFO, logger="omnidesk_agent.server_routes.gmail_push")

    # An older notification redelivered late cannot move the cursor back, so
    # it reports no gap; the gap is reported once, by the reset below.
    stale = client.post("/webhooks/gmail", json=_push(3, "pubsub-old"), headers=_auth(key))
    assert stale.status_code == 200 and adapter.load_history_id() == 5
    assert "omnidesk_gmail_history_gaps_total" not in metrics.counts

    response = client.post("/webhooks/gmail", json=_push(9000), headers=_auth(key))

    assert response.status_code == 200 and response.json()["queued"] == 0
    assert adapter.load_history_id() == 9000
    assert metrics.counts.get("omnidesk_gmail_history_gaps_total") == 1
    assert any("history id 5 has expired" in record.getMessage() for record in caplog.records)

    service.receive(9001, "after-gap")
    later = client.post("/webhooks/gmail", json=_push(9001, "pubsub-2"), headers=_auth(key))
    assert later.json()["queued"] == 1 and [m.message_id for m in queue.queued()] == ["after-gap"]


def test_message_deleted_before_it_is_read_is_skipped_not_retried_forever(tmp_path, google_key, caplog):
    key, pem = google_key
    service = FakeGmailService(deleted={"gone"})
    service.receive(1201, "gone")
    service.receive(1202, "ok")
    client, adapter, queue, metrics = _client(tmp_path, pem, service)
    adapter.save_history_id(1200)
    caplog.set_level(logging.INFO, logger="omnidesk_agent.server_routes.gmail_push")

    response = client.post("/webhooks/gmail", json=_push(1202), headers=_auth(key))

    assert response.status_code == 200 and [m.message_id for m in queue.queued()] == ["ok"]
    assert adapter.load_history_id() == 1202
    assert metrics.counts.get("omnidesk_gmail_messages_missing_total") == 1
    assert any("gone" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "failure",
    [
        "missing_library",  # the default image has no Google libraries
        "revoked_token",
        "gmail_5xx",
    ],
)
def test_read_failure_is_logged_with_its_cause_and_retried_without_moving_the_cursor(tmp_path, google_key, caplog, failure):
    key, pem = google_key
    service = FakeGmailService()
    service.receive(1201, "m1")
    client, adapter, queue, metrics = _client(tmp_path, pem, service)
    adapter.save_history_id(1200)
    expected = {"missing_library": "RuntimeError", "revoked_token": "RuntimeError", "gmail_5xx": "HttpError"}[failure]
    if failure == "missing_library":
        def build_service():
            raise RuntimeError("Install google-auth and google-api-python-client")  # gmail_oauth.build_service
        adapter.oauth.build_service = build_service
    elif failure == "revoked_token":
        service.history_error = RuntimeError("invalid_grant: Token has been expired or revoked. access_token=ya29.leaked-value")
    else:
        service.history_error = _http_error(500)
    caplog.set_level(logging.INFO, logger="omnidesk_agent.server_routes.gmail_push")

    response = client.post("/webhooks/gmail", json=_push(1300), headers=_auth(key))

    assert response.status_code == 503  # Pub/Sub redelivers non-2xx
    assert adapter.load_history_id() == 1200 and queue.queued() == []
    assert metrics.counts.get("omnidesk_gmail_push_read_failures_total") == 1
    assert metrics.labels["omnidesk_gmail_push_read_failures_total"]["reason"] == expected
    assert "omnidesk_gmail_history_gaps_total" not in metrics.counts  # a 5xx is not an expired cursor
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert expected in logged and "ya29.leaked-value" not in logged
    if failure == "missing_library":
        assert "Install google-auth" in logged


def test_push_without_a_valid_oidc_token_is_refused(tmp_path, google_key):
    key, pem = google_key
    client, adapter, queue, _metrics = _client(tmp_path, pem, FakeGmailService())
    for headers in ({}, {"Authorization": "Bearer " + _token(key, aud="https://elsewhere.example")}):
        assert client.post("/webhooks/gmail", json=_push(), headers=headers).status_code == 403
    assert adapter.load_history_id() is None and queue.queued() == []


def test_route_is_closed_until_push_auth_is_configured(tmp_path):
    cfg = AppConfig()
    cfg.channels.gmail.enabled = True
    cfg.channels.gmail.token_file = tmp_path / "gmail_token.json"
    rt = SimpleNamespace(adapters={"gmail": GmailChannel(cfg.channels.gmail)}, job_queue=None, metrics=None)
    app = FastAPI()
    gmail_push.register_gmail_push_route(app, cfg, rt)
    assert TestClient(app).post("/webhooks/gmail", json=_push()).status_code == 404


def test_route_is_closed_while_gmail_is_disabled_even_with_push_auth_set(tmp_path, google_key):
    key, pem = google_key
    client, adapter, queue, _metrics = _client(tmp_path, pem, FakeGmailService(), enabled=False)
    assert client.post("/webhooks/gmail", json=_push(), headers=_auth(key)).status_code == 404
    assert adapter.load_history_id() is None and queue.queued() == []


def test_production_requires_a_sender_allowlist_once_gmail_push_is_configured():
    # An empty allowed_senders admits every sender; production requires an
    # allowlist for every other inbound channel, so Gmail push must have one.
    env = {"OMNIDESK_ENV": "production"}
    issue = "channels.gmail must configure at least one production allowlist when push is configured: allowed_senders"
    cfg = AppConfig()
    cfg.channels.gmail.enabled = True
    assert issue not in validate_production_config(cfg, env)["issues"]  # outbound-only Gmail has no inbound path
    cfg.channels.gmail.push_audience = AUDIENCE
    cfg.channels.gmail.push_service_account_email = SA
    assert issue in validate_production_config(cfg, env)["issues"]
    cfg.channels.gmail.allowed_senders = ["boss@company.com"]
    assert issue not in validate_production_config(cfg, env)["issues"]


def test_start_watch_filters_the_inbox_and_keeps_an_existing_cursor(tmp_path):
    service = FakeGmailService(watch_history_id="7000")
    channel = GmailChannel(GmailConfig(token_file=tmp_path / "gmail_token.json"))
    channel.oauth.build_service = lambda: service

    channel.start_watch("projects/p/topics/t")
    assert service.watch_calls == [{"userId": "me", "body": {"topicName": "projects/p/topics/t", "labelIds": ["INBOX"], "labelFilterBehavior": "INCLUDE"}}]
    assert channel.load_history_id() == 7000

    # A renewal must not skip mail received since the stored cursor; an
    # expired cursor is reset by the next notification (history 404) instead.
    channel.save_history_id(7100)
    service.watch_history_id = "7200"
    channel.start_watch("projects/p/topics/t")
    assert channel.load_history_id() == 7100


def test_gmail_api_calls_run_off_the_event_loop(tmp_path):
    service = FakeGmailService()
    service.receive(1201, "m1")
    channel = GmailChannel(GmailConfig(token_file=tmp_path / "gmail_token.json", allowed_senders=["boss@company.com"]))
    channel.oauth.build_service = lambda: service
    channel.save_history_id(1200)

    async def run():
        await channel.send_email("someone@example.test", "Subject", "Body")
        await channel.list_messages("in:inbox", 5)
        batch = await channel.fetch_new_messages(1201)
        return threading.get_ident(), batch

    loop_thread, batch = asyncio.run(run())
    assert [m.message_id for m in batch.messages] == ["m1"]
    assert len(service.execute_threads) == 4 and loop_thread not in service.execute_threads


def test_gmail_watch_script_refuses_a_disabled_channel(monkeypatch, capsys):
    from scripts import gmail_watch

    calls = []

    class FakeChannel:
        def __init__(self, cfg):
            pass

        def start_watch(self, topic):
            calls.append(topic)
            return {"historyId": "1", "expiration": "2"}

    cfg = AppConfig()
    monkeypatch.setattr(gmail_watch, "GmailChannel", FakeChannel)
    monkeypatch.setattr(gmail_watch, "load_config", lambda path: cfg)
    argv = ["--config", "config.yaml", "--topic", "projects/p/topics/t"]
    assert gmail_watch.main(argv) == 2 and calls == []
    cfg.channels.gmail.enabled = True
    assert gmail_watch.main(argv) == 0 and calls == ["projects/p/topics/t"]
    assert json.loads(capsys.readouterr().out.strip()) == {"historyId": "1", "expiration": "2"}


def test_malformed_notifications_are_rejected():
    for payload in ({}, {"message": {"data": "%%%", "messageId": "1"}}, {"message": {"data": base64.b64encode(b'{"emailAddress":"a@b"}').decode(), "messageId": "1"}}):
        with pytest.raises(ValueError):
            GmailChannel.parse_push(payload)
