"""The desktop's webview can call the gateway, and no other web page gains anything.

The desktop UI runs in a Tauri webview (origin ``tauri://localhost`` or
``http(s)://tauri.localhost``) and calls the gateway with ``fetch``. The webview
enforces CORS. The gateway sent no CORS headers, so the browser preflight of
every desktop call (they all carry ``authorization``) was answered 405 and the
webview refused the request. The Python protocol tests could not notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

SIGNED_HEADERS = "authorization,content-type,idempotency-key,x-omnidesk-actor,x-omnidesk-device-id,x-omnidesk-timestamp,x-omnidesk-nonce,x-omnidesk-device-signature"


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1", "::1"]
    return cfg


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "desk-operator")
    monkeypatch.delenv("OMNIDESK_DESKTOP_WEBVIEW_ORIGINS", raising=False)
    with TestClient(create_app(_cfg(tmp_path))) as test_client:
        yield test_client


def _preflight(client: TestClient, origin: str, path: str = "/app/runtime/desktop/heartbeat") -> object:
    return client.options(path, headers={"origin": origin, "access-control-request-method": "POST", "access-control-request-headers": SIGNED_HEADERS})


@pytest.mark.parametrize("origin", ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"])
def test_the_desktop_webview_may_send_its_signed_calls(client: TestClient, origin: str) -> None:
    response = _preflight(client, origin)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    allowed = {item.strip() for item in response.headers["access-control-allow-headers"].lower().split(",")}
    assert set(SIGNED_HEADERS.split(",")) <= allowed
    # The browser must never attach cookies or HTTP auth on its own.
    assert "access-control-allow-credentials" not in response.headers


def test_the_desktop_webview_can_read_the_answer(client: TestClient) -> None:
    response = client.get("/app/bootstrap", headers={"origin": "tauri://localhost", "authorization": "Bearer operator-token", "x-omnidesk-actor": "desk-operator"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"


def test_other_web_pages_get_no_cors_grant(client: TestClient) -> None:
    assert _preflight(client, "https://evil.example").status_code == 400
    response = client.get("/app/bootstrap", headers={"origin": "https://evil.example", "authorization": "Bearer operator-token", "x-omnidesk-actor": "desk-operator"})
    assert "access-control-allow-origin" not in response.headers


def test_auth_is_unchanged_for_the_webview(client: TestClient) -> None:
    # CORS lets the webview read answers; it grants nothing.
    without_origin = client.get("/app/bootstrap")
    from_webview = client.get("/app/bootstrap", headers={"origin": "tauri://localhost"})
    assert without_origin.status_code in {401, 403}
    assert from_webview.status_code == without_origin.status_code


def test_a_dev_origin_can_be_added_but_not_a_wildcard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIDESK_DESKTOP_WEBVIEW_ORIGINS", "http://127.0.0.1:5173")
    with TestClient(create_app(_cfg(tmp_path))) as dev:
        assert _preflight(dev, "http://127.0.0.1:5173").headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    for bad in ("*", "https://example.com/path", "https://*.example.com", "javascript:alert(1)"):
        monkeypatch.setenv("OMNIDESK_DESKTOP_WEBVIEW_ORIGINS", bad)
        with pytest.raises(RuntimeError, match="OMNIDESK_DESKTOP_WEBVIEW_ORIGINS"):
            create_app(_cfg(tmp_path))


def test_the_webview_can_read_retry_after(client: TestClient) -> None:
    # Retry-After is not CORS-safelisted: without this the desktop cannot back
    # off on 429 or on a 503 database_busy.
    response = client.get("/app/bootstrap", headers={"origin": "tauri://localhost", "authorization": "Bearer operator-token", "x-omnidesk-actor": "desk-operator"})
    assert "retry-after" in response.headers["access-control-expose-headers"].lower()


def test_production_allows_only_https_or_loopback_dev_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnidesk_agent.appsync import desktop_webview_origins

    monkeypatch.setenv("OMNIDESK_DESKTOP_WEBVIEW_ORIGINS", "https://desk.example.com,http://127.0.0.1:5173,http://localhost:1420")
    assert desktop_webview_origins(production=True)[-3:] == ["https://desk.example.com", "http://127.0.0.1:5173", "http://localhost:1420"]
    monkeypatch.setenv("OMNIDESK_DESKTOP_WEBVIEW_ORIGINS", "http://192.168.1.5:5173")
    with pytest.raises(RuntimeError, match="production allows only https or loopback"):
        desktop_webview_origins(production=True)
    # Development keeps any bare origin.
    assert desktop_webview_origins(production=False)[-1] == "http://192.168.1.5:5173"
