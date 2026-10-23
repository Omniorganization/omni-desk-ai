"""Regression tests for webhook replay keys shared across chats and replicas.

M10: Telegram message_id is only unique inside one chat, so keying replay
protection on it let a user who is not on the allowlist burn the ids another
chat would use next (dropped as "duplicate" for 7 days on SQLite, forever on
Postgres).

M11: the WeChat plaintext-mode signature covers only token/timestamp/nonce,
and the single-use nonce lived in process memory, so a captured signed query
string could be replayed with a forged body on another replica.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from omnidesk_agent.channels.telegram import TelegramChannel
from omnidesk_agent.channels.wechat_official import WECHAT_SIGNATURE_MAX_AGE_SECONDS, WeChatOfficialChannel
from omnidesk_agent.config import AppConfig, TelegramConfig, WeChatOfficialConfig
from omnidesk_agent.repositories.postgres_state import PostgresWebhookSecurity
from omnidesk_agent.security.webhook_security import WebhookSecurity, WebhookSecurityConfig
from omnidesk_agent.server_routes.webhook_guard import WebhookGuard
from omnidesk_agent.server_routes.webhook_routes import register_webhook_routes

OWNER = 1111
ATTACKER = 6666


class SharedJsonState:
    """In-memory stand-in for the Postgres JSON state shared by replicas."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict[str, Any]]] = {}

    def insert_once(self, namespace: str, key: str, value: dict[str, Any]) -> bool:
        return self.insert_once_or_replace_stale(namespace, key, value, stale_before=float("-inf"))

    def insert_once_or_replace_stale(self, namespace: str, key: str, value: dict[str, Any], *, stale_before: float) -> bool:
        bucket = self.rows.setdefault(namespace, {})
        current = bucket.get(key)
        if current is not None and current["_updated_at"] >= stale_before:
            return False
        bucket[key] = {**value, "_updated_at": time.time()}
        return True

    def delete_stale(self, namespace: str, *, stale_before: float, limit: int = 1000) -> int:
        bucket = self.rows.get(namespace, {})
        stale = [key for key, row in bucket.items() if row["_updated_at"] < stale_before][:limit]
        for key in stale:
            del bucket[key]
        return len(stale)

    def get(self, namespace: str, key: str) -> Optional[dict[str, Any]]:
        row = self.rows.get(namespace, {}).get(key)
        return dict(row) if row is not None else None

    def update_locked(self, namespace: str, key: str, updater):  # type: ignore[no-untyped-def]
        row = updater(dict(self.rows[namespace][key]))
        self.rows[namespace][key] = {**row, "_updated_at": time.time()}
        return dict(row)


def _telegram_update(update_id: int, *, user: int, chat: int, message_id: int, text: str = "x") -> dict[str, Any]:
    return {"update_id": update_id, "message": {"message_id": message_id, "from": {"id": user}, "chat": {"id": chat}, "text": text}}


def _app_config(channel: str) -> AppConfig:
    cfg = AppConfig()
    getattr(cfg.channels, channel).enabled = True
    return cfg


async def _deliver(guard: WebhookGuard, channel: str, adapter: Any, body: bytes, *, headers=None, query=None, payload=None):
    request = SimpleNamespace(body=AsyncMock(return_value=body), headers=headers or {}, query_params=query or {})
    return await guard.guard(channel, adapter, request, payload=payload)


# --- M10: Telegram -----------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_non_allowlisted_user_cannot_burn_owner_telegram_message_ids(tmp_path: Path, backend: str) -> None:
    channel = TelegramChannel(TelegramConfig(allowed_user_ids=[OWNER]))
    security = (
        WebhookSecurity(tmp_path / "webhooks.sqlite3")
        if backend == "sqlite"
        else PostgresWebhookSecurity(SharedJsonState())  # type: ignore[arg-type]
    )

    def deliver(update: dict[str, Any]):
        envelope = channel.extract_envelope(update)
        security.guard(
            channel="telegram",
            body=json.dumps(update).encode(),
            source_key=envelope.source_key,
            message_id=envelope.message_id,
            timestamp=envelope.timestamp,
        )
        return channel.parse_update(update)

    update_id = 1000
    for message_id in range(1, 61):
        update_id += 1
        assert deliver(_telegram_update(update_id, user=ATTACKER, chat=ATTACKER, message_id=message_id)) is None

    update_id += 1
    owner_update = _telegram_update(update_id, user=OWNER, chat=OWNER, message_id=42, text="approve deploy")
    message = deliver(owner_update)
    assert message is not None and message.sender_id == str(OWNER) and message.text == "approve deploy"

    # A redelivery of the same update is still a replay.
    with pytest.raises(PermissionError, match="duplicate"):
        deliver(owner_update)


@pytest.mark.asyncio
async def test_telegram_guard_keys_replay_on_update_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _app_config("telegram")
    monkeypatch.setenv(cfg.channels.telegram.webhook_secret_env, "tg-secret")
    adapter = TelegramChannel(TelegramConfig(allowed_user_ids=[OWNER, 2222]))
    guard = WebhookGuard(cfg, SimpleNamespace(webhook_security=WebhookSecurity(tmp_path / "webhooks.sqlite3")))
    headers = {"x-telegram-bot-api-secret-token": "tg-secret"}

    # Two chats legitimately reuse message_id 7; both must get through.
    first = json.dumps(_telegram_update(5001, user=OWNER, chat=OWNER, message_id=7)).encode()
    second = json.dumps(_telegram_update(5002, user=2222, chat=2222, message_id=7)).encode()
    _, first_envelope = await _deliver(guard, "telegram", adapter, first, headers=headers)
    _, second_envelope = await _deliver(guard, "telegram", adapter, second, headers=headers)
    assert first_envelope.message_id == "update:5001"
    assert second_envelope.message_id == "update:5002"

    with pytest.raises(HTTPException) as exc:
        await _deliver(guard, "telegram", adapter, first, headers=headers)
    assert exc.value.status_code == 403 and "duplicate" in exc.value.detail


def test_postgres_seen_digests_expire_after_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    state = SharedJsonState()
    security = PostgresWebhookSecurity(state, WebhookSecurityConfig(seen_retention_seconds=3600))  # type: ignore[arg-type]
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    security.guard(channel="telegram", body=b"{}", source_key="u", message_id="update:1")
    monkeypatch.setattr(time, "time", lambda: now + 1800)
    with pytest.raises(PermissionError, match="duplicate"):
        security.guard(channel="telegram", body=b"{}", source_key="u", message_id="update:1")
    monkeypatch.setattr(time, "time", lambda: now + 3601)
    assert security.guard(channel="telegram", body=b"{}", source_key="u", message_id="update:1")["ok"] is True
    # The periodic purge removed the expired rate buckets; the seen row was re-claimed.
    assert set(state.rows["webhook_seen"]) and all(row["_updated_at"] == now + 3601 for row in state.rows["webhook_seen"].values())
    assert all(row["_updated_at"] >= now + 3601 - 120 for row in state.rows["webhook_rate"].values())


# --- M11: WeChat -------------------------------------------------------------------------------


def _wechat_query(token: str, *, nonce: str = "123456789", timestamp: Optional[str] = None) -> dict[str, str]:
    ts = timestamp or str(int(time.time()))
    signature = hashlib.sha1("".join(sorted([token, ts, nonce])).encode(), usedforsecurity=False).hexdigest()
    return {"signature": signature, "timestamp": ts, "nonce": nonce, "openid": "o_customer"}


def _wechat_xml(sender: str, content: str, msg_id: str, ts: str) -> bytes:
    return (
        f"<xml><ToUserName>gh_x</ToUserName><FromUserName>{sender}</FromUserName><CreateTime>{ts}</CreateTime>"
        f"<MsgType>text</MsgType><Content>{content}</Content><MsgId>{msg_id}</MsgId></xml>"
    ).encode()


def _wechat_replica(cfg: AppConfig, security: Any) -> tuple[WebhookGuard, WeChatOfficialChannel]:
    """One gateway process: its own adapter (and nonce memory), the shared webhook store."""
    adapter = WeChatOfficialChannel(WeChatOfficialConfig(allowed_open_ids=["o_owner"]))
    return WebhookGuard(cfg, SimpleNamespace(webhook_security=security)), adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_captured_wechat_signature_cannot_be_replayed_on_another_replica(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    monkeypatch.setenv("WECHAT_TOKEN", "server-token")
    cfg = _app_config("wechat_official")
    if backend == "sqlite":
        def store() -> Any:
            return WebhookSecurity(tmp_path / "webhooks.sqlite3")
    else:
        shared = SharedJsonState()

        def store() -> Any:
            return PostgresWebhookSecurity(shared)  # type: ignore[arg-type]

    replica_a = _wechat_replica(cfg, store())
    replica_b = _wechat_replica(cfg, store())
    query = _wechat_query("server-token")
    legit = _wechat_xml("o_customer", "hi", "1", query["timestamp"])
    forged = _wechat_xml("o_owner", "shell: cat ~/.omnidesk/secrets", "999", query["timestamp"])

    guard_a, adapter_a = replica_a
    body, _ = await _deliver(guard_a, "wechat", adapter_a, legit, query=query, payload=legit)
    assert body == legit

    guard_b, adapter_b = replica_b
    with pytest.raises(HTTPException) as exc:
        await _deliver(guard_b, "wechat", adapter_b, forged, query=query, payload=forged)
    assert exc.value.status_code == 403 and "replayed webhook nonce" in exc.value.detail

    # A restarted process (fresh adapter memory, same store) rejects it as well.
    guard_c, adapter_c = _wechat_replica(cfg, store())
    with pytest.raises(HTTPException):
        await _deliver(guard_c, "wechat", adapter_c, forged, query=query, payload=forged)

    # The rejected replay did not record the forged MsgId, so it cannot burn a later real message.
    fresh = _wechat_query("server-token", nonce="987654321")
    real = _wechat_xml("o_owner", "hello", "999", fresh["timestamp"])
    body, _ = await _deliver(guard_b, "wechat", adapter_b, real, query=fresh, payload=real)
    assert adapter_b.parse_xml(body).sender_id == "o_owner"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_nonce_claims_expire_after_their_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    security: Any = (
        WebhookSecurity(tmp_path / "webhooks.sqlite3") if backend == "sqlite" else PostgresWebhookSecurity(SharedJsonState())  # type: ignore[arg-type]
    )
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    security.claim_nonce(channel="wechat", nonce="ts:n", ttl_seconds=600)
    security.claim_nonce(channel="other", nonce="ts:n", ttl_seconds=600)  # channels are separate namespaces
    monkeypatch.setattr(time, "time", lambda: now + 599)
    with pytest.raises(PermissionError, match="replayed webhook nonce"):
        security.claim_nonce(channel="wechat", nonce="ts:n", ttl_seconds=600)
    monkeypatch.setattr(time, "time", lambda: now + 601)
    security.claim_nonce(channel="wechat", nonce="ts:n", ttl_seconds=600)


def test_wechat_nonce_claim_outlives_the_signature_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WECHAT_TOKEN", "server-token")
    adapter = WeChatOfficialChannel(WeChatOfficialConfig())
    query = _wechat_query("server-token", nonce="n1")
    claim = adapter.signed_request_nonce(query)
    # Keyed on the verified signature, i.e. on the signed concatenation itself.
    assert claim.value == query["signature"]
    # A timestamp is accepted up to the max age on either side of now.
    assert claim.ttl_seconds >= 2 * WECHAT_SIGNATURE_MAX_AGE_SECONDS


def test_resplit_wechat_signature_is_not_a_new_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """sha1(sorted([token, ts, nonce]) joined) cannot tell where ts ends and nonce begins."""
    monkeypatch.setenv("WECHAT_TOKEN", "server-token")
    adapter = WeChatOfficialChannel(WeChatOfficialConfig())
    original = _wechat_query("server-token", nonce="0123456780")
    variant = {**original, "timestamp": "0" + original["timestamp"], "nonce": "012345678"}
    assert adapter.verify_signature(variant["signature"], variant["timestamp"], variant["nonce"])  # same signature
    adapter.verify_request({}, b"", original, None)
    with pytest.raises(PermissionError, match="invalid WeChat webhook timestamp"):
        adapter.verify_request({}, b"", variant, None)
    assert adapter.signed_request_nonce(variant).value == adapter.signed_request_nonce(original).value
    ts = original["timestamp"]
    for bad in ("+" + ts, ts[:1] + "_" + ts[1:], " " + ts, "\u0661" + ts[1:]):
        # Correctly signed, but int() would accept these spellings of a timestamp.
        with pytest.raises(PermissionError, match="invalid WeChat webhook timestamp"):
            adapter.verify_request({}, b"", _wechat_query("server-token", nonce="n2", timestamp=bad), None)


@pytest.mark.asyncio
async def test_unsigned_mode_does_not_record_attacker_nonces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WECHAT_TOKEN", "server-token")
    cfg = _app_config("wechat_official")
    cfg.gateway.require_webhook_signatures = False
    security = WebhookSecurity(tmp_path / "webhooks.sqlite3")
    guard, adapter = _wechat_replica(cfg, security)
    query = _wechat_query("server-token")
    xml = _wechat_xml("o_owner", "hi", "1", query["timestamp"])
    await _deliver(guard, "wechat", adapter, xml, query=query, payload=xml)
    # Nothing was claimed, so the signed request is still claimable once signatures are enforced.
    security.claim_nonce(channel="wechat", nonce=adapter.signed_request_nonce(query).value, ttl_seconds=600)


def _wechat_app(cfg: AppConfig, security: Any) -> tuple[TestClient, WeChatOfficialChannel]:
    adapter = WeChatOfficialChannel(WeChatOfficialConfig(allowed_open_ids=["o_owner"]))
    runtime = SimpleNamespace(adapters={"wechat_official": adapter}, webhook_security=security)
    app = FastAPI()
    register_webhook_routes(app, cfg, runtime, WebhookGuard(cfg, runtime))
    return TestClient(app), adapter


def test_wechat_handshake_query_cannot_sign_a_forged_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WECHAT_TOKEN", "server-token")
    cfg = _app_config("wechat_official")
    client_a, _ = _wechat_app(cfg, WebhookSecurity(tmp_path / "webhooks.sqlite3"))
    client_b, _ = _wechat_app(cfg, WebhookSecurity(tmp_path / "webhooks.sqlite3"))
    query = {**_wechat_query("server-token"), "echostr": "echo-1"}
    with client_a, client_b:
        handshake = client_a.get("/webhooks/wechat", params=query)
        assert handshake.status_code == 200 and handshake.text == "echo-1"
        assert client_a.get("/webhooks/wechat", params=query).status_code == 403
        forged = _wechat_xml("o_owner", "shell: id", "77", query["timestamp"])
        assert client_b.post("/webhooks/wechat", params=query, content=forged).status_code == 403
        stale = {**_wechat_query("server-token", nonce="n9", timestamp=str(int(time.time()) - 3600)), "echostr": "x"}
        assert client_b.get("/webhooks/wechat", params=stale).status_code == 403
        assert client_b.get("/webhooks/wechat", params={**query, "signature": "0" * 40}).status_code == 403


def test_wechat_handshake_rejected_while_channel_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WECHAT_TOKEN", "server-token")
    client, _ = _wechat_app(AppConfig(), WebhookSecurity(tmp_path / "webhooks.sqlite3"))
    with client:
        response = client.get("/webhooks/wechat", params={**_wechat_query("server-token"), "echostr": "e"})
    assert response.status_code == 403


def test_postgres_purge_is_batch_limited() -> None:
    state = SharedJsonState()
    for index in range(2500):
        state.rows.setdefault("webhook_seen", {})[f"old-{index}"] = {"_updated_at": 0.0}
    security = PostgresWebhookSecurity(state)  # type: ignore[arg-type]
    security.guard(channel="telegram", body=b"{}", source_key="u", message_id="update:1")
    assert len(state.rows["webhook_seen"]) == 2500 - security.purge_batch_rows + 1
    # Expired rows that were not purged yet never block a new claim.
    assert state.insert_once_or_replace_stale("webhook_seen", "old-2499", {}, stale_before=time.time() - 60) is True


# --- Real PostgreSQL (CI job with OMNIDESK_TEST_POSTGRES_DSN) ---------------------------------


@pytest.fixture
def isolated_postgres():
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = "webhook_replay_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(dsn, options=f"-csearch_path={schema}")
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_real_postgres_replicas_share_nonces_and_expire_claims(isolated_postgres) -> None:
    from omnidesk_agent.repositories.postgres import PostgresRepositoryFactory

    factories = [PostgresRepositoryFactory(isolated_postgres, pool_size=2) for _ in range(2)]
    try:
        replica_a, replica_b = (factory.webhook_security() for factory in factories)
        replica_a.claim_nonce(channel="wechat", nonce="1700000000:n1", ttl_seconds=600)
        with pytest.raises(PermissionError, match="replayed webhook nonce"):
            replica_b.claim_nonce(channel="wechat", nonce="1700000000:n1", ttl_seconds=600)

        replica_a.guard(channel="telegram", body=b"{}", source_key="1", message_id="update:1")
        replica_b.guard(channel="telegram", body=b"{}", source_key="2", message_id="update:2")
        with pytest.raises(PermissionError, match="duplicate"):
            replica_b.guard(channel="telegram", body=b"{}", source_key="1", message_id="update:1")

        state = replica_a.state
        assert state.insert_once_or_replace_stale("webhook_nonce", "k", {"n": 1}, stale_before=time.time() - 60) is True
        assert state.insert_once_or_replace_stale("webhook_nonce", "k", {"n": 2}, stale_before=time.time() - 60) is False
        # Once the row is older than stale_before it can be claimed again.
        assert state.insert_once_or_replace_stale("webhook_nonce", "k", {"n": 3}, stale_before=time.time() + 1) is True
        assert state.get("webhook_nonce", "k") == {"n": 3}
        assert state.delete_stale("webhook_nonce", stale_before=time.time() + 1) >= 1
        assert state.get("webhook_nonce", "k") is None
        for index in range(3):
            state.insert_once("webhook_seen", f"old-{index}", {"n": index})
        assert state.delete_stale("webhook_seen", stale_before=time.time() + 1, limit=2) == 2
        assert state.delete_stale("webhook_seen", stale_before=time.time() + 1, limit=2) >= 1
    finally:
        for factory in factories:
            factory.close()
