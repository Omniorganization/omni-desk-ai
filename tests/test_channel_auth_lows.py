"""Regression tests for the channel low-severity findings L8, L9 and L10.

L8  Google Chat / Matrix inbound auth used a static ``?token=`` (logged by the
    access log) and Google Chat never verified Google's signed bearer JWT.
L9  Google Chat send_text could post to any chat.googleapis.com space.
L10 The unauthenticated X CRC endpoint returned HMAC(secret, any input), which
    signs webhook bodies when the CRC and webhook secrets are the same.

Requests go through the real webhook routes, WebhookGuard and adapters. The
Google signing certificate is a locally generated key; nothing touches the
network.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import io
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnidesk_agent.channels.native_messaging import GoogleChatChannel, MatrixChannel
from omnidesk_agent.channels.x_channel import XChannel
from omnidesk_agent.config import AppConfig, GoogleChatConfig
from omnidesk_agent.server_routes.webhook_guard import WebhookGuard
from omnidesk_agent.server_routes.webhook_routes import register_webhook_routes
from omnidesk_agent.validation.webhook_signatures import x_crc_response

PROJECT_NUMBER = "1234567890"
CHAT_ISSUER = "chat@system.gserviceaccount.com"
REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- helpers


def _b64url(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def chat_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CHAT_ISSUER)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


@pytest.fixture(scope="module")
def other_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwt(key, *, kid: str = "k1", alg: str = "RS256", **overrides) -> str:
    now = int(time.time())
    claims = {"iss": CHAT_ISSUER, "aud": PROJECT_NUMBER, "iat": now, "exp": now + 3600}
    claims.update(overrides)
    head = _b64url(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}))
    body = _b64url(json.dumps(claims))
    signature = key.sign(f"{head}.{body}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64url(signature)}"


class _Queue:
    def __init__(self):
        self.messages = []

    def enqueue(self, message, source_key):
        self.messages.append(message)
        return {"job_id": f"job-{len(self.messages)}", "created": True}


def _client(cfg: AppConfig, **adapters):
    rt = SimpleNamespace(adapters=adapters, job_queue=_Queue(), metrics=None)
    app = FastAPI()
    register_webhook_routes(app, cfg, rt, WebhookGuard(cfg, rt))
    return TestClient(app), rt.job_queue


def _google_chat_cfg(**fields) -> AppConfig:
    cfg = AppConfig()
    cfg.channels.google_chat.enabled = True
    for key, value in fields.items():
        setattr(cfg.channels.google_chat, key, value)
    return cfg


def _matrix_cfg(**fields) -> AppConfig:
    cfg = AppConfig()
    cfg.channels.matrix.enabled = True
    for key, value in fields.items():
        setattr(cfg.channels.matrix, key, value)
    return cfg


def _x_cfg(enabled: bool = True) -> AppConfig:
    cfg = AppConfig()
    cfg.channels.x.enabled = enabled
    return cfg


GCHAT_EVENT = {
    "type": "MESSAGE",
    "message": {"name": "spaces/S/messages/M1", "text": "hello", "space": {"name": "spaces/S"}},
    "user": {"name": "users/owner"},
}
MATRIX_EVENT = {"event": {"event_id": "$e1", "sender": "@owner:example", "room_id": "!r:example", "content": {"msgtype": "m.text", "body": "hi"}}}


@pytest.fixture(autouse=True)
def _channel_env(monkeypatch):
    for name in (
        "GOOGLE_CHAT_CHANNEL_TOKEN",
        "GOOGLE_CHAT_WEBHOOK_SECRET",
        "GOOGLE_CHAT_WEBHOOK_URL",
        "MATRIX_WEBHOOK_TOKEN",
        "MATRIX_WEBHOOK_SECRET",
        "X_WEBHOOK_CRC_TOKEN",
        "X_WEBHOOK_SECRET",
        "OMNIDESK_ENV",
        "APP_ENV",
        "ENV",
        "OMNIDESK_REQUIRE_PRODUCTION_GUARDS",
        "KUBERNETES_SERVICE_HOST",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- L8 Google Chat


def test_google_chat_rejects_static_query_token_even_when_it_matches(monkeypatch):
    monkeypatch.setenv("GOOGLE_CHAT_CHANNEL_TOKEN", "static-chat-token")
    cfg = _google_chat_cfg()
    client, queue = _client(cfg, google_chat=GoogleChatChannel(cfg.channels.google_chat))
    response = client.post("/webhooks/google-chat", params={"token": "static-chat-token"}, json=GCHAT_EVENT)
    assert response.status_code == 403
    assert queue.messages == []


def test_google_chat_header_token_still_works_outside_production(monkeypatch):
    monkeypatch.setenv("GOOGLE_CHAT_CHANNEL_TOKEN", "static-chat-token")
    cfg = _google_chat_cfg()
    client, queue = _client(cfg, google_chat=GoogleChatChannel(cfg.channels.google_chat))
    assert client.post("/webhooks/google-chat", headers={"x-goog-channel-token": "wrong"}, json=GCHAT_EVENT).status_code == 403
    assert client.post("/webhooks/google-chat", headers={"x-goog-channel-token": "static-chat-token"}, json=GCHAT_EVENT).status_code == 200
    assert [m.sender_id for m in queue.messages] == ["users/owner"]


def test_google_chat_legacy_query_token_is_opt_in_and_never_in_production(monkeypatch):
    monkeypatch.setenv("GOOGLE_CHAT_CHANNEL_TOKEN", "static-chat-token")
    cfg = _google_chat_cfg(allow_query_token=True, require_google_jwt=False)
    dev, dev_queue = _client(cfg, google_chat=GoogleChatChannel(cfg.channels.google_chat))
    assert dev.post("/webhooks/google-chat", params={"token": "static-chat-token"}, json=GCHAT_EVENT).status_code == 200
    assert len(dev_queue.messages) == 1

    prod, prod_queue = _client(cfg, google_chat=GoogleChatChannel(cfg.channels.google_chat, production=True))
    assert prod.post("/webhooks/google-chat", params={"token": "static-chat-token"}, json=GCHAT_EVENT).status_code == 403
    assert prod_queue.messages == []


def test_google_chat_production_requires_google_signed_jwt(monkeypatch, chat_key):
    key, pem = chat_key
    monkeypatch.setenv("GOOGLE_CHAT_CHANNEL_TOKEN", "leaked-static-token")
    cfg = _google_chat_cfg(project_number=PROJECT_NUMBER)
    adapter = GoogleChatChannel(cfg.channels.google_chat, production=True, cert_fetcher=lambda refresh=False: {"k1": pem})
    client, queue = _client(cfg, google_chat=adapter)

    spoofed = dict(GCHAT_EVENT, user={"name": "users/ceo"})
    # A leaked static token (or bridge HMAC) no longer authenticates a body that names any sender.
    assert client.post("/webhooks/google-chat", headers={"x-goog-channel-token": "leaked-static-token"}, json=spoofed).status_code == 403
    assert queue.messages == []

    ok = client.post("/webhooks/google-chat", headers={"Authorization": f"Bearer {_jwt(key)}"}, json=GCHAT_EVENT)
    assert ok.status_code == 200, ok.text
    assert [m.sender_id for m in queue.messages] == ["users/owner"]


def _bridge_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_google_chat_production_without_project_number_fails_closed(monkeypatch, chat_key):
    key, pem = chat_key
    monkeypatch.setenv("GOOGLE_CHAT_CHANNEL_TOKEN", "static-chat-token")
    monkeypatch.setenv("GOOGLE_CHAT_WEBHOOK_SECRET", "bridge-hmac-secret")
    cfg = _google_chat_cfg()
    client, queue = _client(cfg, google_chat=GoogleChatChannel(cfg.channels.google_chat, production=True))
    body = json.dumps(GCHAT_EVENT).encode()
    headers = {
        "Authorization": f"Bearer {_jwt(key)}",
        "x-goog-channel-token": "static-chat-token",
        "x-omnidesk-webhook-signature-256": _bridge_signature("bridge-hmac-secret", body),
        "content-type": "application/json",
    }
    assert client.post("/webhooks/google-chat", headers=headers, content=body).status_code == 403
    assert queue.messages == []


def test_google_chat_production_opt_out_accepts_only_the_signed_bridge(monkeypatch):
    monkeypatch.setenv("GOOGLE_CHAT_CHANNEL_TOKEN", "static-chat-token")
    monkeypatch.setenv("GOOGLE_CHAT_WEBHOOK_SECRET", "bridge-hmac-secret")
    cfg = _google_chat_cfg(require_google_jwt=False)
    client, queue = _client(cfg, google_chat=GoogleChatChannel(cfg.channels.google_chat, production=True))
    body = json.dumps(GCHAT_EVENT).encode()
    # The static token does not cover the body, so it is not enough in production.
    assert client.post("/webhooks/google-chat", headers={"x-goog-channel-token": "static-chat-token"}, json=GCHAT_EVENT).status_code == 403
    signed = {"x-omnidesk-webhook-signature-256": _bridge_signature("bridge-hmac-secret", body), "content-type": "application/json"}
    assert client.post("/webhooks/google-chat", headers=signed, content=body).status_code == 200
    assert len(queue.messages) == 1


def _oversized(key) -> str:
    head, body, _sig = _jwt(key).split(".")
    return f"{head}.{body}.{'A' * 9000}"


def _with_bad_segment_charset(key) -> str:
    head, body, sig = _jwt(key).split(".")
    return f"{head}.{body}.{sig[:-2]}+/"


def _nested_header(key) -> str:
    _head, body, sig = _jwt(key).split(".")
    return f"{_b64url('[' * 3000)}.{body}.{sig}"


def _non_ascii_payload(key) -> str:
    head, body, sig = _jwt(key).split(".")
    return f"{head}.{body}\u00e9.{sig}"


def _list_kid(key) -> str:
    now = int(time.time())
    head = _b64url(json.dumps({"alg": "RS256", "kid": ["k1"]}))
    body = _b64url(json.dumps({"iss": CHAT_ISSUER, "aud": PROJECT_NUMBER, "iat": now, "exp": now + 3600}))
    signature = key.sign(f"{head}.{body}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64url(signature)}"


INVALID_BEARER_TOKENS = [
    pytest.param(lambda key, other: _jwt(key, aud="999999"), id="wrong-audience"),
    pytest.param(lambda key, other: _jwt(key, aud=["999999"]), id="wrong-audience-list"),
    pytest.param(lambda key, other: _jwt(key, iss="attacker@example.iam.gserviceaccount.com"), id="wrong-issuer"),
    pytest.param(lambda key, other: _jwt(key, iat=int(time.time()) - 7200, exp=int(time.time()) - 3600), id="expired"),
    pytest.param(lambda key, other: _jwt(key, iat=int(time.time()) + 3600), id="issued-in-future"),
    pytest.param(lambda key, other: _jwt(key, nbf=int(time.time()) + 3600), id="not-before-future"),
    pytest.param(lambda key, other: _jwt(key, exp="never"), id="non-numeric-exp"),
    pytest.param(lambda key, other: _jwt(other), id="signed-by-other-key"),
    pytest.param(lambda key, other: _jwt(key, kid="unknown"), id="unknown-kid"),
    pytest.param(lambda key, other: _list_kid(key), id="non-string-kid"),
    pytest.param(lambda key, other: _jwt(key, alg="RS512"), id="wrong-alg-header"),
    pytest.param(lambda key, other: _tamper(_jwt(key)), id="tampered-claims"),
    pytest.param(lambda key, other: _unsigned(), id="alg-none"),
    pytest.param(lambda key, other: "not-a-jwt", id="malformed"),
    pytest.param(lambda key, other: _oversized(key), id="oversized"),
    pytest.param(lambda key, other: _with_bad_segment_charset(key), id="non-base64url-segment"),
    pytest.param(lambda key, other: _nested_header(key), id="deeply-nested-header"),
    pytest.param(lambda key, other: _non_ascii_payload(key), id="non-ascii-payload"),
]


@pytest.mark.parametrize("production", [True, False], ids=["production", "development-with-fallbacks"])
@pytest.mark.parametrize("make_token", INVALID_BEARER_TOKENS)
def test_google_chat_rejects_invalid_bearer_tokens(monkeypatch, chat_key, other_key, make_token, production):
    key, pem = chat_key
    refreshes = []

    def fetcher(refresh=False):
        refreshes.append(refresh)
        return {"k1": pem}

    # Outside production the static token and bridge HMAC are valid too: a bad
    # bearer token must still be refused, never downgraded to them.
    monkeypatch.setenv("GOOGLE_CHAT_CHANNEL_TOKEN", "static-chat-token")
    monkeypatch.setenv("GOOGLE_CHAT_WEBHOOK_SECRET", "bridge-hmac-secret")
    cfg = _google_chat_cfg(project_number=PROJECT_NUMBER)
    adapter = GoogleChatChannel(cfg.channels.google_chat, production=production, cert_fetcher=fetcher)
    client, queue = _client(cfg, google_chat=adapter)
    body = json.dumps(GCHAT_EVENT).encode()
    token = make_token(key, other_key)
    headers = {
        "x-goog-channel-token": "static-chat-token",
        "x-omnidesk-webhook-signature-256": _bridge_signature("bridge-hmac-secret", body),
        "content-type": "application/json",
    }
    response = client.post("/webhooks/google-chat", headers={"Authorization": f"Bearer {token}".encode("utf-8"), **headers}, content=body)
    assert response.status_code == 403, response.text
    assert queue.messages == []
    if token.split(".")[0] == _jwt(key, kid="unknown").split(".")[0]:
        assert refreshes == [False, True]


def _tamper(token: str) -> str:
    head, body, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["aud"] = "999999"
    return f"{head}.{_b64url(json.dumps(claims))}.{sig}"


def _unsigned() -> str:
    now = int(time.time())
    claims = {"iss": CHAT_ISSUER, "aud": PROJECT_NUMBER, "iat": now, "exp": now + 3600}
    return f"{_b64url(json.dumps({'alg': 'none', 'kid': 'k1'}))}.{_b64url(json.dumps(claims))}."


def test_google_chat_oversized_bearer_is_refused_before_any_key_lookup(chat_key):
    key, _pem = chat_key

    def fetcher(refresh=False):
        raise AssertionError("certificate lookup for an oversized token")

    adapter = GoogleChatChannel(GoogleChatConfig(enabled=True, project_number=PROJECT_NUMBER), production=True, cert_fetcher=fetcher)
    with pytest.raises(PermissionError):
        adapter.verify_request({"authorization": f"Bearer {_oversized(key)}"}, b"{}", {}, {})


def test_google_chat_hs256_with_public_certificate_as_secret_is_rejected(chat_key):
    key, pem = chat_key
    now = int(time.time())
    head = _b64url(json.dumps({"alg": "HS256", "kid": "k1"}))
    body = _b64url(json.dumps({"iss": CHAT_ISSUER, "aud": PROJECT_NUMBER, "iat": now, "exp": now + 60}))
    sig = _b64url(hmac.new(pem.encode("ascii"), f"{head}.{body}".encode("ascii"), hashlib.sha256).digest())
    adapter = GoogleChatChannel(GoogleChatConfig(enabled=True, project_number=PROJECT_NUMBER), production=True, cert_fetcher=lambda refresh=False: {"k1": pem})
    with pytest.raises(PermissionError):
        adapter.verify_request({"authorization": f"Bearer {head}.{body}.{sig}"}, b"{}", {}, {})


def test_google_chat_jwt_is_verified_outside_production_when_configured(chat_key, other_key, monkeypatch):
    key, pem = chat_key
    monkeypatch.setenv("GOOGLE_CHAT_CHANNEL_TOKEN", "static-chat-token")
    adapter = GoogleChatChannel(GoogleChatConfig(enabled=True, project_number=PROJECT_NUMBER), cert_fetcher=lambda refresh=False: {"k1": pem})
    adapter.verify_request({"Authorization": f"Bearer {_jwt(key)}"}, b"{}", {}, {})
    # A bad bearer token is refused outright, not downgraded to the static token.
    with pytest.raises(PermissionError):
        adapter.verify_request({"Authorization": f"Bearer {_jwt(other_key)}", "x-goog-channel-token": "static-chat-token"}, b"{}", {}, {})


def test_google_chat_project_number_must_be_numeric():
    with pytest.raises(ValueError):
        GoogleChatConfig(project_number="my-project")
    # YAML users write the project number unquoted.
    assert AppConfig.model_validate({"channels": {"google_chat": {"project_number": 123456789012}}}).channels.google_chat.project_number == "123456789012"
    from omnidesk_agent.channels.channel_auth import GoogleChatJwtVerifier

    with pytest.raises(ValueError):
        GoogleChatJwtVerifier("../../etc")


def test_google_chat_adapter_prefetches_certificates_when_enabled():
    class Fetcher:
        prefetches = 0

        def __call__(self, refresh=False):
            return {}

        def prefetch(self):
            self.prefetches += 1

    enabled, disabled = Fetcher(), Fetcher()
    GoogleChatChannel(GoogleChatConfig(enabled=True, project_number=PROJECT_NUMBER), cert_fetcher=enabled)
    GoogleChatChannel(GoogleChatConfig(enabled=False, project_number=PROJECT_NUMBER), cert_fetcher=disabled)
    assert (enabled.prefetches, disabled.prefetches) == (1, 0)


def test_google_cert_fetcher_caches_throttles_and_fails_closed():
    from omnidesk_agent.channels.channel_auth import GoogleCertFetcher

    now = [1000.0]
    calls = []
    responses = []

    def http_get(url, timeout):
        calls.append(url)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    pem_doc = json.dumps({"k1": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"}).encode()
    fetcher = GoogleCertFetcher(
        http_get=http_get, clock=lambda: now[0], min_refresh_seconds=60, max_cache_seconds=3600, max_stale_seconds=600, spawn=lambda target: target()
    )

    responses.append(OSError("network down"))
    with pytest.raises(PermissionError):
        fetcher()
    with pytest.raises(PermissionError):  # throttled: no second network call
        fetcher(refresh=True)
    assert len(calls) == 1

    now[0] += 61
    responses.append((pem_doc, "public, max-age=120"))
    fetched_at = now[0]
    assert set(fetcher()) == {"k1"}
    assert set(fetcher()) == {"k1"}
    for _ in range(20):  # attacker-chosen unknown kids cannot force a fetch storm
        fetcher(refresh=True)
    assert len(calls) == 2

    now[0] = fetched_at + 100  # past the throttle window but within max-age: still served from cache
    assert set(fetcher()) == {"k1"}
    assert len(calls) == 2

    now[0] = fetched_at + 121  # max-age elapsed, refresh fails -> bounded stale cache
    responses.append(OSError("network down"))
    assert set(fetcher()) == {"k1"}
    assert len(calls) == 3
    now[0] = fetched_at + 601 + 61
    responses.append(OSError("network down"))
    with pytest.raises(PermissionError):
        fetcher()

    now[0] += 61
    responses.append((b'{"k1": "not a certificate"}', ""))
    with pytest.raises(PermissionError):
        fetcher()
    assert all(url == "https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com" for url in calls)


def test_google_cert_fetcher_never_waits_on_the_network():
    import threading

    from omnidesk_agent.channels.channel_auth import GoogleCertFetcher

    release = threading.Event()
    calls = []
    pem_doc = json.dumps({"k1": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"}).encode()

    def slow_get(url, timeout):
        calls.append(url)
        release.wait(10)
        return pem_doc, "max-age=3600"

    fetcher = GoogleCertFetcher(http_get=slow_get)
    started = time.monotonic()
    with pytest.raises(PermissionError):  # nothing cached yet: refuse now, fetch in the background
        fetcher()
    with pytest.raises(PermissionError):
        fetcher(refresh=True)
    assert time.monotonic() - started < 2
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            certs = fetcher()
            break
        except PermissionError:
            time.sleep(0.01)
    assert set(certs) == {"k1"}
    assert len(calls) == 1


def test_google_cert_fetcher_respects_channel_offline_mode(monkeypatch):
    from omnidesk_agent.channels.channel_auth import GoogleCertFetcher

    monkeypatch.setenv("OMNIDESK_OFFLINE_MODE", "1")
    fetcher = GoogleCertFetcher(spawn=lambda target: target())
    with pytest.raises(PermissionError):
        fetcher()


# --------------------------------------------------------------------------- L8 Matrix


def test_matrix_rejects_static_query_token_even_when_it_matches(monkeypatch):
    monkeypatch.setenv("MATRIX_WEBHOOK_TOKEN", "matrix-hook-token")
    cfg = _matrix_cfg()
    client, queue = _client(cfg, matrix=MatrixChannel(cfg.channels.matrix))
    for name in ("token", "access_token"):
        assert client.post("/webhooks/matrix", params={name: "matrix-hook-token"}, json=MATRIX_EVENT).status_code == 403
    assert queue.messages == []


def test_matrix_accepts_header_tokens(monkeypatch):
    monkeypatch.setenv("MATRIX_WEBHOOK_TOKEN", "matrix-hook-token")
    cfg = _matrix_cfg()
    client, queue = _client(cfg, matrix=MatrixChannel(cfg.channels.matrix))
    assert client.post("/webhooks/matrix", headers={"Authorization": "Bearer wrong"}, json=MATRIX_EVENT).status_code == 403
    assert client.post("/webhooks/matrix", headers={"Authorization": "Bearer matrix-hook-token"}, json=MATRIX_EVENT).status_code == 200
    assert client.post("/webhooks/matrix", headers={"x-matrix-hook-token": "matrix-hook-token"}, json=MATRIX_EVENT).status_code == 200
    assert len(queue.messages) == 2


def test_matrix_non_ascii_token_is_a_denial_not_a_server_error(monkeypatch):
    monkeypatch.setenv("MATRIX_WEBHOOK_TOKEN", "matrix-hook-token")
    adapter = MatrixChannel(_matrix_cfg().channels.matrix)
    with pytest.raises(PermissionError):
        adapter.verify_request({"x-matrix-hook-token": "tökén"}, b"{}", {}, {})


def test_matrix_legacy_query_token_is_opt_in_and_never_in_production(monkeypatch):
    monkeypatch.setenv("MATRIX_WEBHOOK_TOKEN", "matrix-hook-token")
    cfg = _matrix_cfg(allow_query_token=True)
    dev, dev_queue = _client(cfg, matrix=MatrixChannel(cfg.channels.matrix))
    assert dev.post("/webhooks/matrix", params={"access_token": "matrix-hook-token"}, json=MATRIX_EVENT).status_code == 200
    prod, prod_queue = _client(cfg, matrix=MatrixChannel(cfg.channels.matrix, production=True))
    assert prod.post("/webhooks/matrix", params={"access_token": "matrix-hook-token"}, json=MATRIX_EVENT).status_code == 403
    assert (len(dev_queue.messages), len(prod_queue.messages)) == (1, 0)


# --------------------------------------------------------------------------- L8 access log


def test_query_secrets_are_redacted_from_access_log_lines():
    from uvicorn.logging import AccessFormatter

    from omnidesk_agent.observability.access_log import QuerySecretRedactingFilter, redact_query_secrets, uvicorn_log_config

    assert redact_query_secrets("/webhooks/google-chat?token=s3cr3t&x=1") == "/webhooks/google-chat?token=[REDACTED]&x=1"
    assert redact_query_secrets("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=s3cr3t&hub.challenge=42") == (
        "/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=[REDACTED]&hub.challenge=42"
    )
    assert redact_query_secrets("/m?access%5Ftoken=s3cr3t&API-KEY=k&code=c&since_seq=5") == "/m?access%5Ftoken=[REDACTED]&API-KEY=[REDACTED]&code=[REDACTED]&since_seq=5"
    assert redact_query_secrets("/m?%74oken=s3cr3t&hub%2Everify_token=v") == "/m?%74oken=[REDACTED]&hub%2Everify_token=[REDACTED]"
    assert redact_query_secrets("/health") == "/health"

    config = uvicorn_log_config()
    assert all("redact_query_secrets" in handler["filters"] for handler in config["handlers"].values())

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(QuerySecretRedactingFilter())
    handler.setFormatter(AccessFormatter(fmt=config["formatters"]["access"]["fmt"], use_colors=False))
    logger = logging.getLogger("omnidesk.test.access-redaction")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.info('%s - "%s %s HTTP/%s" %d', "203.0.113.9:5000", "POST", "/webhooks/matrix?access_token=s3cr3t", "1.1", 403)
    finally:
        logger.removeHandler(handler)
    line = stream.getvalue()
    assert "/webhooks/matrix?access_token=[REDACTED]" in line
    assert "s3cr3t" not in line


def test_uvicorn_logging_config_redacts_http_and_websocket_lines():
    # Apply the real config through uvicorn in a child process so the global
    # logging state of this test session is untouched.
    script = (
        "import logging, uvicorn\n"
        "from omnidesk_agent.observability.access_log import uvicorn_log_config\n"
        "uvicorn.Config(app=lambda *a: None, log_config=uvicorn_log_config())\n"
        "logging.getLogger('uvicorn.access').info('%s - \"%s %s HTTP/%s\" %d', '203.0.113.9:1', 'POST', '/webhooks/google-chat?token=s3cr3t', '1.1', 403)\n"
        "logging.getLogger('uvicorn.error').info('%s - \"WebSocket %s\" [accepted]', '203.0.113.9:1', '/app/ws?token=s3cr3t&actor=a')\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "s3cr3t" not in output
    assert "/webhooks/google-chat?token=[REDACTED]" in output
    assert "/app/ws?token=[REDACTED]&actor=a" in output


# --------------------------------------------------------------------------- L9 Google Chat outbound


class _RecordingHttp:
    def __init__(self):
        self.posted: list[str] = []

    async def post(self, url, **kwargs):
        self.posted.append(url)
        return SimpleNamespace(data={"name": "m1"}, request_id="r1")


def test_google_chat_send_refuses_spaces_outside_allowlist():
    channel = GoogleChatChannel(GoogleChatConfig(enabled=True))
    channel.http = _RecordingHttp()
    with pytest.raises(PermissionError):
        asyncio.run(channel.send_text("https://chat.googleapis.com/v1/spaces/ATTACKER/messages?key=k&token=t", "internal data"))
    assert channel.http.posted == []


def test_google_chat_send_allows_only_allowlisted_spaces():
    channel = GoogleChatChannel(GoogleChatConfig(enabled=True, allowed_outbound_spaces=["spaces/AAA", "BBB"]))
    channel.http = _RecordingHttp()
    for url in (
        "https://chat.googleapis.com/v1/spaces/AAA/messages?key=k&token=t",
        "https://chat.googleapis.com/v1/spaces/BBB/messages?key=k&token=t&threadKey=x",
    ):
        asyncio.run(channel.send_text(url, "hi"))
    for url in (
        "https://chat.googleapis.com/v1/spaces/CCC/messages?key=k",
        "https://chat.googleapis.com/v1/spaces/AAA/messages/../../CCC/messages",
        "https://chat.googleapis.com/v1/spaces/AAA%2F..%2FCCC/messages",
        "https://chat.googleapis.com:443/v1/spaces/AAA/messages",
        "https://user@chat.googleapis.com/v1/spaces/AAA/messages",
        "https://chat.googleapis.com.attacker.example/v1/spaces/AAA/messages",
        "https://chat.googleapis.com/v1/spaces/AAAX/messages",
        "https://chat.googleapis.com/v1/spaces/AAA/messages?key=k#frag",
        "spaces/AAA",
    ):
        with pytest.raises((PermissionError, RuntimeError)):
            asyncio.run(channel.send_text(url, "hi"))
    assert channel.http.posted == [
        "https://chat.googleapis.com/v1/spaces/AAA/messages?key=k&token=t",
        "https://chat.googleapis.com/v1/spaces/BBB/messages?key=k&token=t&threadKey=x",
    ]


def test_google_chat_send_with_configured_webhook_ignores_recipient(monkeypatch):
    monkeypatch.setenv("GOOGLE_CHAT_WEBHOOK_URL", "https://chat.googleapis.com/v1/spaces/CFG/messages?key=k")
    channel = GoogleChatChannel(GoogleChatConfig(enabled=True))
    channel.http = _RecordingHttp()
    asyncio.run(channel.send_text("https://chat.googleapis.com/v1/spaces/ATTACKER/messages", "hi"))
    assert channel.http.posted == ["https://chat.googleapis.com/v1/spaces/CFG/messages?key=k"]


# --------------------------------------------------------------------------- L10 X CRC


X_DM_BODY = json.dumps(
    {"direct_message_events": [{"id": "dm1", "message_create": {"sender_id": "attacker", "message_data": {"text": "approve the payout"}}}]},
    separators=(",", ":"),
)


def test_x_crc_cannot_be_used_to_forge_webhook_signature_with_shared_secret(monkeypatch):
    # Operators following X's docs may put the consumer secret in both variables.
    monkeypatch.setenv("X_WEBHOOK_CRC_TOKEN", "shared-consumer-secret")
    monkeypatch.setenv("X_WEBHOOK_SECRET", "shared-consumer-secret")
    cfg = _x_cfg()
    client, queue = _client(cfg, x=XChannel(cfg.channels.x))

    crc = client.get("/webhooks/x", params={"crc_token": X_DM_BODY})
    forged_status = None
    if crc.status_code == 200:
        # The old responder's base64 HMAC re-encoded as hex is a valid webhook signature.
        forged = "sha256=" + base64.b64decode(crc.json()["response_token"].removeprefix("sha256=")).hex()
        forged_status = client.post("/webhooks/x", headers={"x-omnidesk-webhook-signature-256": forged}, content=X_DM_BODY).status_code
    assert crc.status_code == 503, f"CRC signed a webhook body; the forged webhook returned {forged_status}"
    assert "response_token" not in crc.text
    assert queue.messages == []
    # Even a well-formed challenge is refused while the secrets are shared.
    assert client.get("/webhooks/x", params={"crc_token": "ODk1ZjMwMDctNWE1Ni00ZWNjLTk2ZDUtZTAxMWE5ZmNkYTc2"}).status_code == 503


def test_x_crc_only_signs_short_base64_style_challenges(monkeypatch):
    monkeypatch.setenv("X_WEBHOOK_CRC_TOKEN", "consumer-secret")
    monkeypatch.setenv("X_WEBHOOK_SECRET", "bridge-hmac-secret")
    cfg = _x_cfg()
    client, _ = _client(cfg, x=XChannel(cfg.channels.x))

    good = "ODk1ZjMwMDctNWE1Ni00ZWNjLTk2ZDUtZTAxMWE5ZmNkYTc2"
    response = client.get("/webhooks/x", params={"crc_token": good})
    assert response.status_code == 200
    assert response.json() == {"response_token": x_crc_response(good, "consumer-secret")}
    assert client.get("/webhooks/x", params={"crc_token": "A" * 128}).status_code == 200

    for bad in (X_DM_BODY, "", "A" * 129, "abc def", "abc\n", '{"a":1}', "ab%cd", "tök"):
        assert client.get("/webhooks/x", params={"crc_token": bad}).status_code == 400, bad
    assert client.get("/webhooks/x").status_code == 400


def test_x_crc_is_refused_when_channel_disabled(monkeypatch):
    monkeypatch.setenv("X_WEBHOOK_CRC_TOKEN", "consumer-secret")
    cfg = _x_cfg(enabled=False)
    client, _ = _client(cfg, x=XChannel(cfg.channels.x))
    assert client.get("/webhooks/x", params={"crc_token": "abc123"}).status_code == 403


def test_x_crc_without_secret_is_unavailable_not_a_server_error(monkeypatch):
    cfg = _x_cfg()
    client, _ = _client(cfg, x=XChannel(cfg.channels.x))
    assert client.get("/webhooks/x", params={"crc_token": "abc123"}).status_code == 503


# --------------------------------------------------------------------------- startup / production checks


def test_production_channel_auth_issues():
    from omnidesk_agent.channels.channel_auth import production_channel_auth_issues

    cfg = AppConfig()
    assert production_channel_auth_issues(cfg, {}) == []

    cfg.channels.x.enabled = True
    cfg.channels.google_chat.enabled = True
    cfg.channels.google_chat.allow_query_token = True
    cfg.channels.matrix.enabled = True
    cfg.channels.matrix.allow_query_token = True
    issues = production_channel_auth_issues(cfg, {"X_WEBHOOK_CRC_TOKEN": "same", "X_WEBHOOK_SECRET": "same"})
    assert any(i.startswith("channels.x webhook_crc_token_env and webhook_secret_env") for i in issues)
    assert any(i.startswith("channels.google_chat.project_number") for i in issues)
    assert any(i.startswith("channels.google_chat.allow_query_token") for i in issues)
    assert any(i.startswith("channels.matrix.allow_query_token") for i in issues)
    assert all("same" not in i for i in issues)

    cfg.channels.google_chat.allow_query_token = False
    cfg.channels.google_chat.project_number = PROJECT_NUMBER
    cfg.channels.matrix.allow_query_token = False
    assert production_channel_auth_issues(cfg, {"X_WEBHOOK_CRC_TOKEN": "consumer", "X_WEBHOOK_SECRET": "bridge"}) == []

    cfg.channels.google_chat.project_number = None
    cfg.channels.google_chat.require_google_jwt = False  # explicit signed-bridge deployment
    assert production_channel_auth_issues(cfg, {}) == []

    cfg.gateway.require_webhook_signatures = False
    assert production_channel_auth_issues(cfg, {}) == ["gateway.require_webhook_signatures must be true in production while google_chat, matrix or x is enabled"]


def test_production_check_lists_channel_auth_issues():
    from omnidesk_agent.validation.production import validate_production_config

    cfg = AppConfig()
    cfg.channels.x.enabled = True
    cfg.channels.google_chat.enabled = True
    env = {"OMNIDESK_ENV": "production", "X_WEBHOOK_CRC_TOKEN": "same-secret", "X_WEBHOOK_SECRET": "same-secret"}
    issues = validate_production_config(cfg, env)["issues"]
    assert any(i.startswith("channels.x webhook_crc_token_env and webhook_secret_env") for i in issues)
    assert any(i.startswith("channels.google_chat.project_number") for i in issues)
    assert validate_production_config(cfg, {"X_WEBHOOK_CRC_TOKEN": "same-secret", "X_WEBHOOK_SECRET": "same-secret"})["issues"] == []


def test_runtime_refuses_to_start_production_with_unsafe_channel_auth(monkeypatch, tmp_path):
    from omnidesk_agent.daemon import OmniDeskRuntime
    from omnidesk_agent.validation.production import ProductionConfigError

    cfg = AppConfig()
    cfg.channels.gmail.token_file = tmp_path / "gmail" / "token.json"
    cfg.channels.x.enabled = True
    monkeypatch.setenv("X_WEBHOOK_CRC_TOKEN", "shared-consumer-secret")
    monkeypatch.setenv("X_WEBHOOK_SECRET", "shared-consumer-secret")
    stub = SimpleNamespace(cfg=cfg)

    adapters = OmniDeskRuntime._build_channel_adapters(stub)  # development: starts, CRC refuses at request time
    assert adapters["google_chat"].production is False

    monkeypatch.setenv("OMNIDESK_ENV", "production")
    with pytest.raises(ProductionConfigError) as excinfo:
        OmniDeskRuntime._build_channel_adapters(stub)
    assert "shared-consumer-secret" not in str(excinfo.value)

    monkeypatch.setenv("X_WEBHOOK_SECRET", "bridge-hmac-secret")
    adapters = OmniDeskRuntime._build_channel_adapters(stub)
    assert adapters["google_chat"].production is True
    assert adapters["matrix"].production is True
    assert adapters["google_chat"].require_jwt is True
