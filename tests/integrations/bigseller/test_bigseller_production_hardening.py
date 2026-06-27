from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from omnidesk_agent.api.routes.bigseller import create_bigseller_router
from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import SQLiteBigSellerSyncErrorQueue
from omnidesk_agent.integrations.bigseller.idempotency import (
    SQLiteBigSellerIdempotencyGuard,
)
from omnidesk_agent.integrations.bigseller.schemas import BigSellerWebhookEvent
from omnidesk_agent.integrations.bigseller.webhooks import parse_bigseller_webhook
from omnidesk_agent.integrations.bigseller.worker import BigSellerConnectorContext


def _signed_headers(body: bytes, secret: str, *, timestamp: datetime | None = None):
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    timestamp = timestamp or datetime.now(timezone.utc)
    return {
        "x-bigseller-signature-256": f"sha256={signature}",
        "x-bigseller-timestamp": timestamp.isoformat(),
    }


def test_sqlite_idempotency_persists_across_instances(tmp_path):
    db_path = tmp_path / "bigseller.sqlite3"
    first = SQLiteBigSellerIdempotencyGuard(db_path)
    assert first.claim(external_id="evt-1", store_id="store-1", action_type="webhook")
    first.complete(external_id="evt-1", store_id="store-1", action_type="webhook")

    second = SQLiteBigSellerIdempotencyGuard(db_path)
    assert not second.claim(
        external_id="evt-1", store_id="store-1", action_type="webhook"
    )
    assert second.stats()["durable"] is True
    assert second.stats()["completed"] == 1


def test_sqlite_idempotency_ttl_purges_expired_records(tmp_path):
    db_path = tmp_path / "bigseller.sqlite3"
    guard = SQLiteBigSellerIdempotencyGuard(db_path, ttl_seconds=60)
    assert guard.claim(external_id="evt-ttl", store_id="store-1", action_type="webhook")
    with guard._connect() as conn:  # noqa: SLF001 - contract-level migration/TTL assertion
        conn.execute(
            "UPDATE bigseller_idempotency_records SET expires_at = ? WHERE key = ?",
            ("1970-01-01T00:00:00+00:00", "store-1:evt-ttl:webhook"),
        )

    assert guard.purge_expired() == 1
    assert guard.claim(external_id="evt-ttl", store_id="store-1", action_type="webhook")


def test_sqlite_error_queue_persists_and_redacts(tmp_path):
    db_path = tmp_path / "bigseller.sqlite3"
    first = SQLiteBigSellerSyncErrorQueue(db_path, max_retries=1)
    first.enqueue(
        entity_type="order",
        external_id="BS-1",
        store_id="MY",
        action="sync",
        payload={"access_token": "raw-token"},
        error="authorization=Bearer abc123",
    )

    second = SQLiteBigSellerSyncErrorQueue(db_path, max_retries=1)
    items = second.list()
    assert len(items) == 1
    assert "raw-token" not in str(items[0].payload)
    assert "abc123" not in items[0].error_message
    assert second.stats()["durable"] is True


def test_real_mode_webhook_requires_timestamp_and_event_id(tmp_path):
    secret = "test-secret"
    body = json.dumps(
        {
            "event_type": "order.updated",
            "event_id": "evt-live-1",
            "store_id": "MY-STORE-1",
        }
    ).encode("utf-8")
    config = BigSellerConfig(
        enabled=True,
        use_mock=False,
        base_url="https://bigseller-private.example",
        app_id="app-id",
        app_key="app-key",
        access_token="token",
        webhook_secret=secret,
        state_db_path=tmp_path / "bigseller.sqlite3",
    )

    event = parse_bigseller_webhook(body, _signed_headers(body, secret), config)

    assert event.event_id == "evt-live-1"
    assert event.signature_digest


def test_real_mode_webhook_rejects_stale_timestamp(tmp_path):
    secret = "test-secret"
    body = json.dumps({"event_type": "order.updated", "event_id": "evt-old"}).encode(
        "utf-8"
    )
    config = BigSellerConfig(
        enabled=True,
        use_mock=False,
        base_url="https://bigseller-private.example",
        app_id="app-id",
        app_key="app-key",
        access_token="token",
        webhook_secret=secret,
        state_db_path=tmp_path / "bigseller.sqlite3",
    )
    stale = datetime.now(timezone.utc) - timedelta(hours=1)

    with pytest.raises(PermissionError, match="stale"):
        parse_bigseller_webhook(body, _signed_headers(body, secret, timestamp=stale), config)


def test_webhook_event_id_is_deduplicated_by_worker(tmp_path):
    config = BigSellerConfig(
        enabled=True,
        use_mock=True,
        webhook_secret="test-secret",
        state_db_path=tmp_path / "bigseller.sqlite3",
    )
    context = BigSellerConnectorContext.from_config(config)
    event = BigSellerWebhookEvent(
        event_type="order.updated",
        event_id="evt-duplicate",
        store_id="MY-STORE-1",
        payload={"status": "paid"},
    )

    first = context.worker.handle_webhook(event)
    second = context.worker.handle_webhook(event)

    assert first["handled"] == "orders"
    assert second["handled"] == "duplicate"
    status = context.worker.status()
    assert status["metrics"]["bigseller_webhook_duplicate_total"] == 1
    assert status["prometheus_metrics"]["omnidesk_bigseller_webhook_duplicate_total"] == 1


def test_worker_dead_letter_metric_is_current_gauge(tmp_path):
    config = BigSellerConfig(
        enabled=True,
        use_mock=True,
        state_db_path=tmp_path / "bigseller.sqlite3",
    )
    context = BigSellerConnectorContext.from_config(config)
    context.worker.errors.enqueue(
        entity_type="order",
        external_id="BS-1",
        store_id="MY",
        action="sync",
        payload={},
        error="first",
    )
    status = context.worker.status()
    assert status["metrics"]["bigseller_dead_letter_current"] == 0
    assert status["prometheus_metrics"]["omnidesk_bigseller_dead_letter_current"] == 0
    context.worker.errors.enqueue(
        entity_type="order",
        external_id="BS-1",
        store_id="MY",
        action="sync",
        payload={},
        error="second",
    )
    status = context.worker.status()
    assert status["metrics"]["bigseller_dead_letter_current"] == 1
    assert status["prometheus_metrics"]["omnidesk_bigseller_dead_letter_current"] == 1


def test_webhook_rejects_payload_over_configured_limit(tmp_path):
    config = BigSellerConfig(
        enabled=True,
        use_mock=True,
        webhook_secret="test-secret",
        webhook_max_body_bytes=8,
        state_db_path=tmp_path / "bigseller.sqlite3",
    )
    context = BigSellerConnectorContext.from_config(config)
    app = FastAPI()
    app.include_router(create_bigseller_router(context=context))
    client = TestClient(app)

    response = client.post(
        "/integrations/bigseller/webhook",
        content=b"0123456789",
        headers={"content-length": "10"},
    )

    assert response.status_code == 413
    status = context.worker.status()
    assert status["metrics"]["bigseller_webhook_rejected_total"] == 1
    assert status["prometheus_metrics"]["omnidesk_bigseller_webhook_rejected_total"] == 1
