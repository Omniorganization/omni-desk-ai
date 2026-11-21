"""Refused device signatures and verified state changes go on security's audit chain.

#231 stopped writing a sync event for every verified device request: desktops
heartbeat every 5-15 s, and those events grew the sync feed without bound. That
event was also the only per-device trace of a signed request (Codex, on #231).
Security decided (#278): AppSync calls ``AdminAuth.record_device_request`` for
every refused signature and for every verified request that changes state, such as
task claims and reports, approval decisions, and key and registration operations.
Heartbeats are not recorded. A state change that cannot be recorded does not happen.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

DEVICE = "desk_audited"
AUTH = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "alice", "content-type": "application/json"}


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


def _signed(key: Ed25519PrivateKey, path: str, body: str, nonce: str) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    message = f"omnidesk-device-request:v1:POST:{path}:{body_hash}:{timestamp}:{nonce}".encode("utf-8")
    return {**AUTH, "x-omnidesk-device-id": DEVICE, "x-omnidesk-timestamp": timestamp, "x-omnidesk-nonce": nonce, "x-omnidesk-device-signature": key.sign(message).hex()}


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "alice")
    app = create_app(_cfg(tmp_path))
    store = app.state.runtime.app_sync
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode("utf-8")
    store.register_device(actor="alice", device_id=DEVICE, device_type="desktop", name="Desktop", platform="macOS", public_key=public_key)
    store.devices[DEVICE].credential_status = "verified"
    store.devices[DEVICE].trust_level = "challenge_verified"
    store._persist()
    # Production enforces device signatures.
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    recorded: list[dict[str, Any]] = []
    failure: list[BaseException] = []

    def record_device_request(**entry: Any) -> None:
        if failure:
            raise failure[0]
        recorded.append(entry)

    # Security's hook (#278); AdminAuth at this commit has none, so it is attached here.
    monkeypatch.setattr(app.state.runtime.admin_auth, "record_device_request", record_device_request, raising=False)
    with TestClient(app) as client:
        yield client, store, key, recorded, failure


def test_a_refused_signature_is_recorded_with_its_reason(gateway: Any) -> None:
    client, _, _, recorded, _ = gateway
    path = "/app/runtime/desktop/claim"
    body = json.dumps({"device_id": DEVICE, "capabilities": ["local-runtime"]}, separators=(",", ":"))
    impostor = Ed25519PrivateKey.generate()
    refused = client.post(path, headers=_signed(impostor, path, body, "nonce-refused-000001"), content=body)
    assert refused.status_code == 401
    assert recorded == [{
        "verified": False, "device_id": DEVICE, "actor": "alice", "method": "POST", "path": path,
        "reason": "signature_mismatch", "device_type": "desktop", "client_host": "testclient",
    }]


def test_a_verified_claim_is_recorded_and_a_heartbeat_is_not(gateway: Any) -> None:
    client, _, key, recorded, _ = gateway
    claim = "/app/runtime/desktop/claim"
    claim_body = json.dumps({"device_id": DEVICE, "capabilities": ["local-runtime"]}, separators=(",", ":"))
    assert client.post(claim, headers=_signed(key, claim, claim_body, "nonce-claim-0000001"), content=claim_body).status_code == 200
    beat = "/app/runtime/desktop/heartbeat"
    beat_body = json.dumps({"device_id": DEVICE, "status": "online"}, separators=(",", ":"))
    assert client.post(beat, headers=_signed(key, beat, beat_body, "nonce-beat-00000001"), content=beat_body).status_code == 200
    # Liveness is covered by counters and last_seen_at; only the claim is on the record.
    assert [(entry["path"], entry["verified"], entry["reason"]) for entry in recorded] == [(claim, True, "")]


def test_a_refused_heartbeat_is_still_recorded(gateway: Any) -> None:
    client, _, _, recorded, _ = gateway
    beat = "/app/runtime/desktop/heartbeat"
    body = json.dumps({"device_id": DEVICE, "status": "online"}, separators=(",", ":"))
    assert client.post(beat, headers=_signed(Ed25519PrivateKey.generate(), beat, body, "nonce-beat-refused01"), content=body).status_code == 401
    assert [(entry["path"], entry["verified"]) for entry in recorded] == [(beat, False)]


def test_a_state_change_that_cannot_be_recorded_does_not_happen(gateway: Any) -> None:
    client, store, key, recorded, failure = gateway
    failure.append(PermissionError("audit chain cannot be extended"))  # security's AuditIntegrityError
    path = f"/app/devices/{DEVICE}/push-token"
    body = json.dumps({"push_token": "fcm-token-abcdefghijklmnop", "platform": "macos"}, separators=(",", ":"))
    refused = client.post(path, headers=_signed(key, path, body, "nonce-push-00000001"), content=body)
    assert refused.status_code == 503 and "audit log unavailable" in refused.text
    assert store.devices[DEVICE].push_token is None and recorded == []
