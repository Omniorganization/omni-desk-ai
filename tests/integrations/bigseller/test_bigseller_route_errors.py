from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnidesk_agent.api.routes.bigseller import create_bigseller_router
from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.worker import BigSellerConnectorContext


def test_admin_sync_error_uses_safe_error_envelope(tmp_path):
    config = BigSellerConfig(
        enabled=False,
        use_mock=True,
        audit_log_path=tmp_path / "audit.jsonl",
        state_db_path=tmp_path / "bigseller.sqlite3",
    )
    context = BigSellerConnectorContext.from_config(config)
    app = FastAPI()
    app.include_router(create_bigseller_router(context=context))

    with TestClient(app) as client:
        response = client.post(
            "/integrations/bigseller/sync/orders",
            headers={"x-request-id": "trace-1"},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail == {
        "code": "BIGSELLER_DISABLED",
        "message": "BigSeller connector is disabled",
        "trace_id": "trace-1",
        "retryable": True,
    }


def test_admin_retry_error_uses_safe_not_found_envelope(tmp_path):
    config = BigSellerConfig(
        enabled=True,
        use_mock=True,
        audit_log_path=tmp_path / "audit.jsonl",
        state_db_path=tmp_path / "bigseller.sqlite3",
    )
    context = BigSellerConnectorContext.from_config(config)
    app = FastAPI()
    app.include_router(create_bigseller_router(context=context))

    with TestClient(app) as client:
        response = client.post("/integrations/bigseller/errors/missing/retry")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "BIGSELLER_NOT_FOUND"
    assert detail["message"] == "BigSeller resource was not found"
    assert "missing" not in str(detail)
