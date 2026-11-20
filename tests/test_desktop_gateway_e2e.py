"""The desktop's gateway protocol end to end, with device signatures enforced.

Production gateways accept signed desktop calls only from a challenge-verified
device. Before the enrollment flow, a fresh desktop install could never become
verified, so every heartbeat, claim and status report failed with
``device_not_challenge_verified``. This test drives the HTTP routes exactly as
the desktop does — P-256 keys, raw r||s signatures (what Rust and WebCrypto
produce), the request message format — from a fresh install through enrollment,
an owner's device-signed approval, claiming, executing an approved workspace read
and reporting its result.

It exercises the protocol, not the Tauri binary: the TypeScript and Rust pieces
are covered by apps/desktop-tauri tests (executor, enrollment, challenge
signing).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient

from omnidesk_agent.appsync import routes
from omnidesk_agent.appsync.store import task_action_hash
from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

OWNER = {"authorization": "Bearer owner-token", "x-omnidesk-actor": "olivia-owner"}
OPERATOR = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "desk-operator"}
READ = {"capability": "workspace.read_file", "scope": {"workspace": "reports", "relative_path": "q3.md"}}


class Device:
    """A device key that signs like the desktop's Rust signer (raw r||s, base64)."""

    def __init__(self, prefix: str) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.device_id = f"{prefix}_{secrets.token_hex(18)}"
        self.public_pem = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def sign(self, message: str) -> str:
        r, s = decode_dss_signature(self.key.sign(message.encode(), ec.ECDSA(hashes.SHA256())))
        return "base64:" + base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()

    def headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        nonce = secrets.token_hex(24)
        message = f"omnidesk-device-request:v1:{method}:{path}:{hashlib.sha256(body).hexdigest()}:{timestamp}:{nonce}"
        return {"x-omnidesk-device-id": self.device_id, "x-omnidesk-timestamp": timestamp, "x-omnidesk-nonce": nonce, "x-omnidesk-device-signature": self.sign(message)}


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


def _post(client: TestClient, path: str, auth: dict[str, str], payload: dict[str, Any], *, device: Device | None = None, key: str | None = None) -> Any:
    body = json.dumps(payload).encode()
    headers = {**auth, "content-type": "application/json", "idempotency-key": key or secrets.token_hex(8)}
    if device is not None:
        headers.update(device.headers("POST", path, body))
    return client.post(path, content=body, headers=headers)


def _enroll(client: TestClient, device: Device, *, device_type: str, owner: dict[str, str], actor: dict[str, str]) -> dict[str, Any]:
    pairing = secrets.token_urlsafe(12)
    started = _post(client, "/app/devices/enrollment/start", owner, {"device_type": device_type, "pairing_code": pairing})
    assert started.status_code == 200, started.text
    enrollment_id = started.json()["enrollment"]["enrollment_id"]
    completed = _post(client, f"/app/devices/enrollment/{enrollment_id}/complete", actor, {"pairing_code": pairing, "device_id": device.device_id, "public_key": device.public_pem})
    assert completed.status_code == 200 and completed.json()["enrollment"]["status"] == "paired", completed.text
    challenge = _post(client, f"/app/devices/enrollment/{enrollment_id}/challenge", actor, {"device_id": device.device_id}).json()["challenge"]
    verified = _post(client, f"/app/devices/enrollment/{enrollment_id}/verify", actor, {"challenge_id": challenge["challenge_id"], "device_id": device.device_id, "signature": device.sign(challenge["signing_message"])})
    assert verified.status_code == 200, verified.text
    return verified.json()["credential"]


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "desk-operator")
    # Production enforces device signatures on these routes; so does this gateway.
    monkeypatch.setattr(routes, "_device_signature_enforced", lambda _cfg: True)
    with TestClient(create_app(_cfg(tmp_path))) as client:
        yield client


def test_fresh_desktop_enrolls_then_runs_an_approved_action(gateway: TestClient, tmp_path: Path) -> None:
    desktop = Device("desk")
    registered = _post(gateway, "/app/devices/register", OPERATOR, {"device_id": desktop.device_id, "device_type": "desktop", "name": "Omni Desktop Runtime", "platform": "MacIntel", "public_key": desktop.public_pem, "capabilities": ["chat", "local-runtime", "workspace.read_file"]})
    assert registered.status_code == 200, registered.text

    # A fresh install: the gateway refuses its signed heartbeat until it is enrolled.
    heartbeat = {"device_id": desktop.device_id, "status": "online", "version": "1.12.7", "capabilities": ["workspace.read_file"]}
    refused = _post(gateway, "/app/runtime/desktop/heartbeat", OPERATOR, heartbeat, device=desktop)
    assert refused.status_code == 401 and "device_not_challenge_verified" in refused.text

    assert _enroll(gateway, desktop, device_type="desktop", owner=OWNER, actor=OPERATOR)["trust_level"] == "challenge_verified"
    assert _post(gateway, "/app/runtime/desktop/heartbeat", OPERATOR, heartbeat, device=desktop).status_code == 200

    # The owner's own console device signs the approval decision.
    console = Device("webadm")
    _post(gateway, "/app/devices/register", OWNER, {"device_id": console.device_id, "device_type": "web_admin", "name": "console", "platform": "browser", "public_key": console.public_pem})
    _enroll(gateway, console, device_type="web_admin", owner=OWNER, actor=OWNER)

    conversation = _post(gateway, "/app/conversations", OWNER, {"title": "reports"}).json()["conversation"]
    created = _post(gateway, f"/app/conversations/{conversation['conversation_id']}/messages", OWNER, {"content": "send me the Q3 report", "action": READ}).json()
    approval = created["approval"]
    assert approval["action_hash"] == task_action_hash(READ) and "reports/q3.md" in approval["action"]
    decided = _post(gateway, f"/app/approvals/{approval['approval_id']}/decide", OWNER, {"decision": "approved", "source_device_id": console.device_id}, device=console)
    assert decided.status_code == 200, decided.text

    claim = {"device_id": desktop.device_id, "capabilities": ["workspace.read_file"], "lease_seconds": 60}
    task = _post(gateway, "/app/runtime/desktop/claim", OPERATOR, claim, device=desktop).json()["task"]
    assert task["action"] == READ and task["action_hash"] == approval["action_hash"]

    # The desktop recomputes the hash before running anything, then reads inside its workspace.
    assert task_action_hash(task["action"]) == task["action_hash"]
    workspace = tmp_path / "OmniDesktopWorkspace" / "reports"
    workspace.mkdir(parents=True)
    (workspace / "q3.md").write_text("Q3 revenue: 42", encoding="utf-8")
    content = (workspace / task["action"]["scope"]["relative_path"]).read_text(encoding="utf-8")
    status_path = f"/app/tasks/{task['task_id']}/status"
    report = {"status": "completed", "result_summary": "workspace file read (14 characters; contents and path omitted from status)", "assigned_runtime_device_id": desktop.device_id, "result": {"content": content, "truncated": False}}
    reported = _post(gateway, status_path, OPERATOR, report, device=desktop)
    assert reported.status_code == 200, reported.text

    seen_by_owner = gateway.get(f"/app/tasks/{task['task_id']}", headers=OWNER).json()["task"]
    assert seen_by_owner["status"] == "completed" and seen_by_owner["result"]["content"] == "Q3 revenue: 42"
    seen_by_operator = gateway.get(f"/app/tasks/{task['task_id']}", headers=OPERATOR).json()["task"]
    assert "result" not in seen_by_operator


def test_signed_calls_from_another_or_replayed_device_are_refused(gateway: TestClient) -> None:
    desktop = Device("desk")
    _post(gateway, "/app/devices/register", OPERATOR, {"device_id": desktop.device_id, "device_type": "desktop", "name": "d", "platform": "p", "public_key": desktop.public_pem})
    _enroll(gateway, desktop, device_type="desktop", owner=OWNER, actor=OPERATOR)
    body = json.dumps({"device_id": desktop.device_id, "status": "online"}).encode()
    headers = {**OPERATOR, "content-type": "application/json", **desktop.headers("POST", "/app/runtime/desktop/heartbeat", body)}
    assert gateway.post("/app/runtime/desktop/heartbeat", content=body, headers=headers).status_code == 200
    replay = gateway.post("/app/runtime/desktop/heartbeat", content=body, headers=headers)
    assert replay.status_code == 401 and "nonce_replay" in replay.text
    impostor = Device("desk")
    forged = {**headers, **impostor.headers("POST", "/app/runtime/desktop/heartbeat", body), "x-omnidesk-device-id": desktop.device_id}
    rejected = gateway.post("/app/runtime/desktop/heartbeat", content=body, headers=forged)
    assert rejected.status_code == 401 and "signature_mismatch" in rejected.text
