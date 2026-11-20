"""Provider redeliveries are acknowledged or accepted, never lost, and replay protection holds.

Regression tests for the 2026-11-21 audit (integrations, medium): a verified
duplicate got 403, the digest was recorded before the enqueue so a failed
enqueue lost the message for good, any event older than 300 s was refused
although providers redeliver for hours to days, and a rate-limited source got
403 instead of 429 with Retry-After.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.repositories.postgres_state import PostgresWebhookSecurity
from omnidesk_agent.security.webhook_security import (
    WebhookDuplicate,
    WebhookRateLimited,
    WebhookSecurity,
    WebhookSecurityConfig,
)

SECRET = "tg-webhook-secret-" + "s" * 24
OWNER = 4242


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "g" / "c.json"
    cfg.channels.gmail.token_file = tmp_path / "g" / "t.json"
    cfg.plugins.enabled = False
    cfg.channels.telegram.enabled = True
    cfg.channels.telegram.allowed_user_ids = [OWNER]
    cfg.ensure_dirs()
    return cfg


def _update(update_id: int, *, sent_at: Optional[float] = None) -> bytes:
    message = {"message_id": update_id, "from": {"id": OWNER}, "chat": {"id": OWNER}, "text": "hello", "date": int(sent_at or time.time())}
    return json.dumps({"update_id": update_id, "message": message}).encode()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:" + "b" * 30)
    from omnidesk_agent.server import create_app

    app = create_app(_cfg(tmp_path))
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, app.state.runtime


def _post(client, body: bytes, secret: str = SECRET):
    return client.post("/webhooks/telegram", content=body, headers={"x-telegram-bot-api-secret-token": secret, "content-type": "application/json"})


def test_a_verified_duplicate_is_acknowledged_and_enqueued_once(client):
    test_client, runtime = client

    first = _post(test_client, _update(1))
    again = _post(test_client, _update(1))

    assert first.status_code == 200 and first.json()["queued"] is True
    assert again.status_code == 200 and again.json() == {"ok": True, "duplicate": True}
    assert len(runtime.job_queue.list()) == 1


def test_a_verified_duplicate_gets_the_body_its_provider_expects(client, monkeypatch):
    # QQ re-sends its op 13 validation after a timeout and needs the op 12 ACK
    # for every push, so a bare {"duplicate": true} would fail it. The adapter's
    # webhook_response rebuilds that body from the verified payload alone.
    test_client, runtime = client
    calls: list[tuple[Any, Any]] = []

    def webhook_response(parsed, payload):
        calls.append((parsed, payload))
        return {"op": 12, "d": 0}

    monkeypatch.setattr(runtime.adapters["telegram"], "webhook_response", webhook_response, raising=False)
    body = _update(7)
    assert _post(test_client, body).status_code == 200
    calls.clear()  # this branch does not shape first deliveries; channels' #233 does

    again = _post(test_client, body)

    assert again.status_code == 200 and again.json() == {"op": 12, "d": 0}
    assert calls == [(None, json.loads(body))]
    assert len(runtime.job_queue.list()) == 1


def test_a_duplicate_body_the_adapter_refuses_is_403_not_500(client, monkeypatch):
    test_client, runtime = client

    def webhook_response(parsed, payload):
        raise PermissionError("QQ validation payload has no plain_token")

    monkeypatch.setattr(runtime.adapters["telegram"], "webhook_response", webhook_response, raising=False)
    body = _update(8)
    assert _post(test_client, body).status_code == 200

    refused = _post(test_client, body)

    assert refused.status_code == 403 and "plain_token" in refused.json()["detail"]


def test_an_unsigned_duplicate_is_still_refused(client):
    test_client, _runtime = client
    assert _post(test_client, _update(2)).status_code == 200

    assert _post(test_client, _update(2), secret="wrong").status_code == 403


def test_a_failed_enqueue_leaves_the_redelivery_acceptable(client, monkeypatch):
    test_client, runtime = client
    real_enqueue = runtime.job_queue.enqueue
    calls = {"n": 0}

    def flaky_enqueue(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(runtime.job_queue, "enqueue", flaky_enqueue)

    failed = _post(test_client, _update(3))
    redelivered = _post(test_client, _update(3))

    assert failed.status_code == 500
    assert redelivered.status_code == 200 and redelivered.json()["queued"] is True and redelivered.json()["created"] is True
    assert _post(test_client, _update(3)).json() == {"ok": True, "duplicate": True}


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_redeliveries_with_the_original_event_time_are_accepted(tmp_path, backend):
    cfg = WebhookSecurityConfig()
    store = WebhookSecurity(tmp_path / "w.sqlite3", cfg) if backend == "sqlite" else PostgresWebhookSecurity(_SharedJsonState(), cfg)  # type: ignore[arg-type]

    # Meta/WhatsApp redeliver for up to 7 days with the original event time.
    assert store.guard(channel="meta", body=b"a", source_key="u", message_id="m1", timestamp=time.time() - 6 * 3600, claim=False)["ok"]
    with pytest.raises(PermissionError, match="timestamp"):
        store.guard(channel="meta", body=b"b", source_key="u", message_id="m2", timestamp=time.time() - 8 * 24 * 3600, claim=False)
    with pytest.raises(PermissionError, match="timestamp"):
        store.guard(channel="meta", body=b"c", source_key="u", message_id="m3", timestamp=time.time() + 3600, claim=False)


def test_the_event_window_never_outlives_the_digest_store(tmp_path):
    cfg = WebhookSecurityConfig(seen_retention_seconds=3600)
    store = WebhookSecurity(tmp_path / "w.sqlite3", cfg)

    with pytest.raises(PermissionError, match="timestamp"):
        store.guard(channel="meta", body=b"a", source_key="u", message_id="m1", timestamp=time.time() - 7200, claim=False)


def test_a_rate_limited_source_gets_429_with_retry_after(client):
    test_client, runtime = client
    runtime.webhook_security.cfg.rate_limit_max_requests = 2

    statuses = [_post(test_client, _update(100 + i)).status_code for i in range(3)]
    limited = _post(test_client, _update(200))

    assert statuses[:2] == [200, 200] and statuses[2] == 429
    assert limited.status_code == 429 and 1 <= int(limited.headers["retry-after"]) <= 61


class _SharedJsonState:
    """In-memory stand-in for the PostgreSQL JSON state (methods the webhook store uses)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict[str, Any]]] = {}

    def insert_once(self, namespace, key, value) -> bool:
        return self.insert_once_or_replace_stale(namespace, key, value, stale_before=float("-inf"))

    def insert_once_or_replace_stale(self, namespace, key, value, *, stale_before) -> bool:
        bucket = self.rows.setdefault(namespace, {})
        current = bucket.get(key)
        if current is not None and current["_updated_at"] >= stale_before:
            return False
        bucket[key] = {**value, "_updated_at": time.time()}
        return True

    def delete_stale(self, namespace, *, stale_before, limit=1000) -> int:
        return 0

    def get(self, namespace, key):
        row = self.rows.get(namespace, {}).get(key)
        return dict(row) if row is not None else None

    def update_locked(self, namespace, key, updater):
        row = updater(dict(self.rows[namespace][key]))
        self.rows[namespace][key] = {**row, "_updated_at": time.time()}
        return dict(row)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_both_stores_check_without_claiming_until_mark_seen(tmp_path, backend):
    cfg = WebhookSecurityConfig(rate_limit_max_requests=3)
    store = WebhookSecurity(tmp_path / "w.sqlite3", cfg) if backend == "sqlite" else PostgresWebhookSecurity(_SharedJsonState(), cfg)  # type: ignore[arg-type]

    first = store.guard(channel="telegram", body=b"{}", source_key="u", message_id="update:9", claim=False)
    again = store.guard(channel="telegram", body=b"{}", source_key="u", message_id="update:9", claim=False)
    assert first["digest"] == again["digest"]  # nothing recorded yet: a failed enqueue loses nothing

    assert store.mark_seen("telegram", first["digest"]) is True
    with pytest.raises(WebhookDuplicate):
        store.guard(channel="telegram", body=b"{}", source_key="u", message_id="update:9", claim=False)
    with pytest.raises(WebhookRateLimited) as limited:
        store.guard(channel="telegram", body=b"{}", source_key="u", message_id="update:10", claim=False)
    assert limited.value.retry_after_seconds >= 1
