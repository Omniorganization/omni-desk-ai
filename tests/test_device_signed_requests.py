from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app


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


def _signed_headers(*, device_id: str, secret: str, method: str, path: str, body: str, nonce: str = "nonce-1234567890abcdef") -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    message = f"omnidesk-device-request:v1:{method.upper()}:{path}:{body_hash}:{timestamp}:{nonce}".encode("utf-8")
    return {
        "x-omnidesk-device-id": device_id,
        "x-omnidesk-timestamp": timestamp,
        "x-omnidesk-nonce": nonce,
        "x-omnidesk-device-signature": hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest(),
    }


def test_production_desktop_claim_requires_verified_device_signature(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    store = app.state.runtime.app_sync
    device_id = "desk_signed_device"
    secret = "super-secret-device-key"
    store.register_device(actor="alice", device_id=device_id, device_type="desktop", name="Desktop", platform="macOS", public_key=f"legacy-hmac:{secret}")
    store.devices[device_id].credential_status = "verified"
    store.devices[device_id].trust_level = "challenge_verified"
    store._persist()
    monkeypatch.setenv("OMNIDESK_ENV", "production")

    path = "/app/runtime/desktop/claim"
    body = json.dumps({"device_id": device_id, "capabilities": ["local-runtime"], "lease_seconds": 60}, separators=(",", ":"))
    auth = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "alice", "content-type": "application/json"}

    with TestClient(app) as client:
        denied = client.post(path, headers=auth, content=body)
        assert denied.status_code == 401
        headers = {**auth, **_signed_headers(device_id=device_id, secret=secret, method="POST", path=path, body=body)}
        accepted = client.post(path, headers=headers, content=body)
        assert accepted.status_code == 200, accepted.text
        replay = client.post(path, headers=headers, content=body)
        assert replay.status_code == 401
        assert "nonce_replay" in replay.text
