from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


def _isolated_config(tmp_path):
    from omnidesk_agent.config import AppConfig

    cfg = AppConfig()
    cfg.workspace.root = tmp_path
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.learning.growth_plan_file = tmp_path / "growth_plan.json"
    return cfg


class DummyRequest:
    headers = {}
    client = type("Client", (), {"host": "127.0.0.1"})()
    url = type("Url", (), {"path": "/admin/status"})()


def _endpoint(app, path: str):
    for route in app.routes:
        if getattr(route, "path", "") == path:
            return route.endpoint
    raise AssertionError(f"route not found: {path}")


def test_health_is_public_and_redacted(tmp_path, monkeypatch):
    from omnidesk_agent.server import create_app

    cfg = _isolated_config(tmp_path)
    cfg.gateway.allow_local_admin_without_token = True
    cfg.ensure_dirs()
    app = create_app(cfg)
    data = asyncio.run(_endpoint(app, "/health")())
    assert data["ok"] is True
    assert "version" in data
    assert "workspace" not in data
    assert "tools" not in data


def test_admin_status_requires_auth_when_local_bypass_disabled(tmp_path):
    fastapi = pytest.importorskip("fastapi")
    from omnidesk_agent.server import create_app

    cfg = _isolated_config(tmp_path)
    cfg.gateway.allow_local_admin_without_token = False
    cfg.ensure_dirs()
    app = create_app(cfg)
    with pytest.raises(fastapi.HTTPException) as exc:
        asyncio.run(_endpoint(app, "/admin/status")(DummyRequest()))
    assert exc.value.status_code == 403


def test_readiness_routes_cover_runtime_snapshot_and_middleware(tmp_path, monkeypatch):
    from omnidesk_agent.server import create_app

    monkeypatch.setenv("OMNIDESK_ADMIN_TOKEN", "a" * 40)
    monkeypatch.setenv("OMNIDESK_GATEWAY_SECRET", "g" * 40)
    cfg = _isolated_config(tmp_path)
    cfg.gateway.allow_local_admin_without_token = False
    cfg.gateway.admin_allowed_ips = ["127.0.0.1", "testclient"]
    cfg.plugins.enabled = False
    cfg.ensure_dirs()

    app = create_app(cfg)
    with TestClient(app) as client:
        ready = client.get("/ready", headers={"x-request-id": "rid-1"})
        assert ready.status_code == 200
        assert ready.json() == {"ok": True}
        assert ready.headers["x-request-id"] == "rid-1"
        assert ready.headers["traceparent"].startswith("00-")

        admin_ready = client.get("/admin/ready", headers={"x-omnidesk-admin-token": "a" * 40})
        assert admin_ready.status_code == 200
        assert admin_ready.json()["checks"]["database"] is True

        status = client.get("/admin/status", headers={"x-omnidesk-admin-token": "a" * 40})
        assert status.status_code == 200
        runtime = status.json()["runtime"]
        assert runtime["resource_guard"]["enabled"] is True
        assert runtime["resource_guard"]["backend"] in {"memory", "sqlite", "postgres"}
        assert "cost_ledger" in runtime
        assert runtime["release_evidence"]["summary_path"].endswith("real-ga-evidence-summary-1.12.6.json")


def test_readiness_reports_database_and_secret_failures(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from omnidesk_agent.server import create_app

    monkeypatch.delenv("OMNIDESK_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("OMNIDESK_GATEWAY_SECRET", raising=False)
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "v" * 40)
    cfg = _isolated_config(tmp_path)
    cfg.gateway.allow_local_admin_without_token = False
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    app = create_app(cfg)
    app.state.runtime.repository_factory.health_check = lambda: (_ for _ in ()).throw(RuntimeError("database unavailable"))

    with pytest.raises(fastapi.HTTPException) as ready_exc:
        asyncio.run(_endpoint(app, "/ready")())
    assert ready_exc.value.status_code == 503
    assert ready_exc.value.detail == {"ok": False}

    with pytest.raises(fastapi.HTTPException) as admin_exc:
        request = DummyRequest()
        request.headers = {"authorization": "Bearer " + "v" * 40}
        asyncio.run(_endpoint(app, "/admin/ready")(request))
    assert admin_exc.value.status_code == 503
    detail = admin_exc.value.detail
    assert detail["checks"]["database"] is False
    assert "database unavailable" in detail["checks"]["database_error"]
    assert "OMNIDESK_ADMIN_TOKEN" in detail["checks"]["missing_secrets"]


def test_api_resource_guard_limits_public_requests(tmp_path):
    from omnidesk_agent.server import create_app

    cfg = _isolated_config(tmp_path)
    cfg.gateway.allow_local_admin_without_token = True
    cfg.api_resource_guard.window_seconds = 60
    cfg.api_resource_guard.max_requests_per_ip = 1
    cfg.api_resource_guard.max_requests_per_endpoint = 10
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        limited = client.get("/health")
        assert limited.status_code == 429
        assert limited.json()["detail"] == "ip rate limit exceeded"


def test_api_resource_guard_rejects_oversized_body(tmp_path, monkeypatch):
    from omnidesk_agent.server import create_app

    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    cfg = _isolated_config(tmp_path)
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1"]
    cfg.api_resource_guard.max_body_bytes = 8
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    app = create_app(cfg)
    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            headers={"authorization": "Bearer operator-token", "x-omnidesk-actor": "alice", "idempotency-key": "oversized"},
            json={"content": "x" * 100},
        )
        assert response.status_code == 413
        assert response.json()["detail"] == "request body too large"
