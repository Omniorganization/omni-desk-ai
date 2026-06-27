from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnidesk_agent.api.routes.bigseller import create_bigseller_router
from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.worker import BigSellerConnectorContext


def _signed_headers(body: bytes, secret: str) -> dict[str, str]:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "x-bigseller-signature-256": f"sha256={digest}",
        "content-type": "application/json",
    }


def test_webhook_verifies_signature_and_triggers_order_sync(tmp_path):
    secret = "webhook-secret"
    config = BigSellerConfig(
        enabled=True,
        use_mock=True,
        webhook_secret=secret,
        audit_log_path=tmp_path / "audit.jsonl",
    )
    context = BigSellerConnectorContext.from_config(config)
    app = FastAPI()
    app.include_router(create_bigseller_router(context=context))
    body = json.dumps(
        {
            "event_type": "order.created",
            "external_order_id": "BS-ORDER-1001",
            "store_id": "MY-STORE-1",
        }
    ).encode("utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/integrations/bigseller/webhook",
            content=body,
            headers=_signed_headers(body, secret),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["handled"] == "orders"
    assert payload["sync"]["processed"] == 2


def test_webhook_rejects_bad_signature(tmp_path):
    secret = "webhook-secret"
    config = BigSellerConfig(
        enabled=True,
        use_mock=True,
        webhook_secret=secret,
        audit_log_path=tmp_path / "audit.jsonl",
    )
    context = BigSellerConnectorContext.from_config(config)
    app = FastAPI()
    app.include_router(create_bigseller_router(context=context))

    with TestClient(app) as client:
        response = client.post(
            "/integrations/bigseller/webhook",
            json={
                "event_type": "order.created",
                "external_order_id": "BS-ORDER-1001",
                "store_id": "MY-STORE-1",
            },
            headers={"x-bigseller-signature-256": "sha256=bad"},
        )

    assert response.status_code == 403


def test_disabled_connector_health_is_safe_and_sync_is_blocked(tmp_path):
    config = BigSellerConfig(
        enabled=False, use_mock=True, audit_log_path=tmp_path / "audit.jsonl"
    )
    context = BigSellerConnectorContext.from_config(config)
    app = FastAPI()
    app.include_router(create_bigseller_router(context=context))

    with TestClient(app) as client:
        health = client.get("/integrations/bigseller/health")
        sync = client.post("/integrations/bigseller/sync/orders")

    assert health.status_code == 200
    assert health.json()["enabled"] is False
    assert sync.status_code == 409
