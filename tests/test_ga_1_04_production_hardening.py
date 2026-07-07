from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app
from omnidesk_agent.validation.production import validate_production_config

PINNED = (
    "python:3.11-slim@sha256:"
    + "66f011380d0e49ed280c789fbd08ff0d40968ee7b665575489afa95c98196ab5"
)


def _native_version(project_version: str) -> str:
    base = project_version.split("+", 1)[0]
    return base if len(base.split(".")) >= 3 else f"{base}.0"


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
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    return cfg


def test_web_admin_csp_and_session_cookie_are_ga_hardened() -> None:
    next_config = Path("apps/web-admin-next/next.config.mjs").read_text(
        encoding="utf-8"
    )
    session = Path("apps/web-admin-next/lib/session.ts").read_text(encoding="utf-8")
    login = Path("apps/web-admin-next/app/api/session/login/route.ts").read_text(
        encoding="utf-8"
    )
    assert "unsafe-eval" not in next_config
    assert "unsafe-inline" not in next_config
    assert "object-src 'none'" in next_config
    assert "require-trusted-types-for 'script'" in next_config
    assert "trusted-types default" in next_config
    assert "connect-src 'self' https:" not in next_config
    assert "connect-src 'self';" in next_config
    assert "img-src 'self' data:" not in next_config
    assert "__Host-omni_session_token" in session
    assert "maxAge" in login
    assert "verifyGatewayIdentity" in login
    assert "payload.actor" not in login
    assert "payload.role" not in login
    page = Path("apps/web-admin-next/app/page.tsx").read_text(encoding="utf-8")
    api = Path("apps/web-admin-next/lib/api.ts").read_text(encoding="utf-8")
    assert "web-admin-console" not in page
    assert "web-admin-console" not in api


def test_helm_chart_requires_pipeline_injected_app_digest() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    version_match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert version_match
    version = version_match.group(1)
    chart_version = _native_version(version)
    chart = Path("deploy/kubernetes/helm/omnidesk/Chart.yaml").read_text(
        encoding="utf-8"
    )
    values = Path("deploy/kubernetes/helm/omnidesk/values.yaml").read_text(
        encoding="utf-8"
    )
    deployment = Path(
        "deploy/kubernetes/helm/omnidesk/templates/deployment.yaml"
    ).read_text(encoding="utf-8")
    configmap = Path(
        "deploy/kubernetes/helm/omnidesk/templates/configmap.yaml"
    ).read_text(encoding="utf-8")
    assert f"version: {chart_version}" in chart
    assert f"appVersion: {version}" in chart
    assert 'digest: "" # required' in values
    assert 'required "image.digest is required' in deployment
    assert "app_sync:" in configmap
    assert "backend: postgres" in configmap


def test_production_validator_blocks_query_token_and_non_postgres_appsync(
    monkeypatch,
) -> None:
    cfg = AppConfig()
    cfg.gateway.public_base_url = "https://omnidesk.example.test"
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.sandbox.backend = "remote_docker"
    cfg.sandbox.runner_url = "http://sandbox-runner:18890"
    cfg.sandbox.docker_image = PINNED
    cfg.storage.backend = "postgres"
    cfg.storage.require_multi_instance_safe = True
    cfg.app_sync.backend = "json"
    cfg.app_sync.allow_websocket_query_auth = True
    env = {
        "OMNIDESK_ENV": "production",
        "OMNIDESK_ADMIN_TOKEN": "x" * 40,
        "OMNIDESK_GATEWAY_SECRET": "x" * 40,
        "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
        "OMNIDESK_SANDBOX_RUNNER_TOKEN": "x" * 40,
        "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET": "x" * 40,
        "OMNIDESK_POSTGRES_DSN": "postgresql://user:pass@db/omni",
        "OMNIDESK_APPSYNC_POSTGRES_DSN": "postgresql://user:pass@db/omni",
    }
    result = validate_production_config(cfg, env)
    assert (
        "app_sync.allow_websocket_query_auth must be false in production"
        in result["issues"]
    )
    assert (
        "app_sync.backend must be postgres when storage.require_multi_instance_safe=true"
        in result["issues"]
    )


def test_production_device_registration_requires_public_key_and_random_device_id(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "viewer-token")
    monkeypatch.setenv("OMNIDESK_GATEWAY_SECRET", "g" * 40)
    monkeypatch.setenv("OMNIDESK_ADMIN_TOKEN", "a" * 40)
    monkeypatch.setenv("OMNIDESK_MEMORY_ENCRYPTION_KEY", "m" * 40)
    cfg = _cfg(tmp_path)
    cfg.memory_privacy.encrypt_at_rest = True
    denied = validate_production_config(cfg)
    assert (
        "api_resource_guard.backend must be postgres in production" in denied["issues"]
    )
    monkeypatch.setattr(
        "omnidesk_agent.server.assert_production_config_safe", lambda _cfg: None
    )
    app = create_app(cfg)
    headers = {
        "authorization": "Bearer operator-token",
        "x-omnidesk-actor": "alice",
        "idempotency-key": "device-reg",
    }
    with TestClient(app) as client:
        missing = client.post(
            "/app/devices/register",
            headers=headers,
            json={
                "device_id": "desktop-1",
                "device_type": "desktop",
                "name": "Desktop",
                "platform": "macOS",
            },
        )
        assert missing.status_code == 422
        web_missing = client.post(
            "/app/devices/register",
            headers={**headers, "idempotency-key": "web-device-reg"},
            json={
                "device_id": "web-admin-console",
                "device_type": "web_admin",
                "name": "Web Admin",
                "platform": "nextjs",
            },
        )
        assert web_missing.status_code == 422
        web_predictable = client.post(
            "/app/devices/register",
            headers={**headers, "idempotency-key": "web-device-reg-2"},
            json={
                "device_id": "web-admin-console",
                "device_type": "web_admin",
                "name": "Web Admin",
                "platform": "nextjs",
                "public_key": "base64:" + "a" * 44,
            },
        )
        assert web_predictable.status_code == 422
        predictable = client.post(
            "/app/devices/register",
            headers={**headers, "idempotency-key": "device-reg-2"},
            json={
                "device_id": "desktop-1",
                "device_type": "desktop",
                "name": "Desktop",
                "platform": "macOS",
                "public_key": "base64:" + "a" * 44,
            },
        )
        assert predictable.status_code == 422
        ok = client.post(
            "/app/devices/register",
            headers={**headers, "idempotency-key": "device-reg-3"},
            json={
                "device_id": "desk_1234567890abcdef1234567890abcdef",
                "device_type": "desktop",
                "name": "Desktop",
                "platform": "macOS",
                "public_key": "base64:" + "a" * 44,
            },
        )
        assert ok.status_code == 200, ok.text


def test_appsync_device_registration_uses_unified_production_detection(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OMNIDESK_ENV", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "viewer-token")
    monkeypatch.setenv("OMNIDESK_GATEWAY_SECRET", "g" * 40)
    monkeypatch.setenv("OMNIDESK_ADMIN_TOKEN", "a" * 40)
    monkeypatch.setenv("OMNIDESK_MEMORY_ENCRYPTION_KEY", "m" * 40)
    cfg = _cfg(tmp_path)
    cfg.memory_privacy.encrypt_at_rest = True
    monkeypatch.setattr(
        "omnidesk_agent.server.assert_production_config_safe", lambda _cfg: None
    )
    app = create_app(cfg)
    headers = {
        "authorization": "Bearer operator-token",
        "idempotency-key": "app-env-device-reg",
    }

    with TestClient(app) as client:
        response = client.post(
            "/app/devices/register",
            headers=headers,
            json={
                "device_id": "desktop-1",
                "device_type": "desktop",
                "name": "Desktop",
                "platform": "macOS",
            },
        )

    assert response.status_code == 422
    assert "public_key is required" in response.text


def test_admin_session_identity_returns_token_bound_actor(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "alice")
    app = create_app(_cfg(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/admin/session/identity",
            headers={
                "authorization": "Bearer operator-token",
                "x-omnidesk-actor": "system",
            },
        )

    assert response.status_code == 200
    assert response.json()["actor"] == "alice"
    assert response.json()["role"] == "operator"


def test_native_apps_generate_per_install_device_identity() -> None:
    desktop = Path("apps/desktop-tauri/src/deviceIdentity.ts").read_text(
        encoding="utf-8"
    )
    web_admin = Path("apps/web-admin-next/lib/device-identity.ts").read_text(
        encoding="utf-8"
    )
    mobile = Path("apps/mobile-flutter/lib/device_identity.dart").read_text(
        encoding="utf-8"
    )
    mobile_pubspec = Path("apps/mobile-flutter/pubspec.yaml").read_text(
        encoding="utf-8"
    )
    assert "crypto.subtle.generateKey" in desktop
    assert "omni.devicePrivateKeyJwk.v2" in desktop
    assert "crypto.subtle.generateKey" in web_admin
    assert "signWebAdminDeviceRequest" in web_admin
    assert "web-admin-console" not in web_admin
    assert "Ed25519" in mobile
    assert "omni.device_private_key.v2" in mobile
    assert "cryptography:" in mobile_pubspec


def test_desktop_release_workflows_use_committed_cargo_lockfile() -> None:
    tri_app = Path(".github/workflows/tri-app-quality.yml").read_text(encoding="utf-8")
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    combined = tri_app + release
    assert "cargo generate-lockfile" not in combined
    assert "cargo check --locked --manifest-path src-tauri/Cargo.toml" in combined


def test_ga_release_gate_script_is_wired_into_release_workflows() -> None:
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    tri_app = Path(".github/workflows/tri-app-quality.yml").read_text(encoding="utf-8")
    script = Path("scripts/check_ga_release_gate.py").read_text(encoding="utf-8")
    assert "python scripts/check_ga_release_gate.py ." in release
    assert "python scripts/check_ga_release_gate.py ." in tri_app
    assert 'BASE_EXTERNAL_EVIDENCE_GATE = "check_external_ga_evidence.py"' in script
    assert 'COMPLETE_REAL_GA_GATE = "check_real_ga_complete.py"' in script
    assert 'REAL_GA_STATUS = "blocked_missing_external_evidence"' in script
