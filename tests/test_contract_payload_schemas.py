"""The gateway's real answers meet the contract's payload schemas, route by route.

The contract used to list routes, roles and surfaces only. Now every route a
client calls has the request and response schema the clients rely on (consumer
driven: what they send, what they read). This test drives each of those routes
on a real gateway, the way the clients call them, and validates every request
body it sends and every response it gets against the contract. A gateway change
that drops or retypes a field a client reads fails here. The clients check the
same requirements at run time (scripts/generate_contract_clients.py).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import secrets
import time
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient

from omnidesk_agent.appsync.contract import CONTRACT_HEADER, CONTRACT_VERSION
from omnidesk_agent.config import AppConfig
from omnidesk_agent.models.base import ModelResponse
from omnidesk_agent.server import create_app

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("generate_contract_clients", ROOT / "scripts" / "generate_contract_clients.py")
assert spec and spec.loader
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)

CONTRACT = generator.load_contract(ROOT)
OWNER = {"authorization": "Bearer owner-token", "x-omnidesk-actor": "olivia-owner"}
OPERATOR = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "desk-operator"}


def _endpoint(method: str, template: str) -> dict[str, Any]:
    return next(e for e in CONTRACT["endpoints"] if e["method"] == method and e["path"] == template)


def _schema(method: str, template: str, part: str) -> dict[str, Any]:
    return generator.resolve(_endpoint(method, template)[part], CONTRACT.get("schemas", {}))


class FakeChatRouter:
    async def complete(self, request: Any) -> ModelResponse:
        return ModelResponse(text=f"assistant:{request.user}", provider="fake-provider", model="fake-chat-model", profile="fast", usage={"input_tokens": 2, "output_tokens": 5})


class Key:
    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public_pem = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def sign(self, message: str) -> str:
        r, s = decode_dss_signature(self.key.sign(message.encode(), ec.ECDSA(hashes.SHA256())))
        return "base64:" + base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


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


class Contracted:
    """Calls the gateway like a client and checks both bodies against the contract."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.checked: set[tuple[str, str]] = set()

    def call(self, method: str, template: str, path: str, auth: dict[str, str], body: dict[str, Any] | None = None, *, extra: dict[str, str] | None = None) -> Any:
        endpoint = _endpoint(method, template)
        if body is not None:
            jsonschema.validate(body, _schema(method, template, "request"))
        headers = {**auth, CONTRACT_HEADER: CONTRACT_VERSION, "idempotency-key": secrets.token_hex(8), **(extra or {})}
        response = self.client.request(method, path, headers=headers, json=body)
        assert response.status_code == 200, f"{method} {path}: {response.status_code} {response.text}"
        assert response.headers[CONTRACT_HEADER] == CONTRACT_VERSION
        if "response" in endpoint:
            jsonschema.validate(response.json(), _schema(method, template, "response"))
        self.checked.add((method, template))
        return response


def test_every_client_route_has_request_and_response_schemas() -> None:
    for endpoint in CONTRACT["endpoints"]:
        if not endpoint.get("client_surfaces"):
            continue
        route = f"{endpoint['method']} {endpoint['path']}"
        assert "response" in endpoint or endpoint.get("response_content_type") == "text/event-stream", route
        if endpoint["method"] in {"POST", "PATCH"}:
            assert "request" in endpoint, route
        for part in ("request", "response"):
            if part in endpoint:
                jsonschema.Draft202012Validator.check_schema(generator.resolve(endpoint[part], CONTRACT.get("schemas", {})))


def test_the_gateway_meets_the_contract_on_every_client_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "desk-operator")
    app = create_app(_cfg(tmp_path))
    app.state.runtime.model_router = FakeChatRouter()
    with TestClient(app) as client:
        gw = Contracted(client)

        def enroll(key: Key, device_id: str, device_type: str, actor: dict[str, str]) -> None:
            pairing = secrets.token_urlsafe(12)
            started = gw.call("POST", "/app/devices/enrollment/start", "/app/devices/enrollment/start", OWNER, {"device_type": device_type, "pairing_code": pairing}).json()
            enrollment_id = started["enrollment"]["enrollment_id"]
            base = f"/app/devices/enrollment/{enrollment_id}"
            gw.call("POST", "/app/devices/enrollment/{enrollment_id}/complete", f"{base}/complete", actor, {"pairing_code": pairing, "device_id": device_id, "public_key": key.public_pem})
            challenge = gw.call("POST", "/app/devices/enrollment/{enrollment_id}/challenge", f"{base}/challenge", actor, {"device_id": device_id}).json()["challenge"]
            gw.call("POST", "/app/devices/enrollment/{enrollment_id}/verify", f"{base}/verify", actor, {"challenge_id": challenge["challenge_id"], "device_id": device_id, "signature": key.sign(challenge["signing_message"])})

        # Devices: a desktop and the owner's phone, enrolled as the clients do.
        desktop, desktop_id = Key(), f"desk_{secrets.token_hex(18)}"
        gw.call("POST", "/app/devices/register", "/app/devices/register", OPERATOR, {"device_id": desktop_id, "device_type": "desktop", "name": "Omni Desktop Runtime", "platform": "MacIntel", "public_key": desktop.public_pem, "capabilities": ["chat", "local-runtime", "workspace.read_file"]})
        enroll(desktop, desktop_id, "desktop", OPERATOR)
        phone, phone_id = Key(), f"mob_{secrets.token_hex(18)}"
        gw.call("POST", "/app/devices/register", "/app/devices/register", OWNER, {"device_id": phone_id, "device_type": "mobile", "name": "phone", "platform": "android", "public_key": phone.public_pem, "push_token": None})
        enroll(phone, phone_id, "mobile", OWNER)
        gw.call("POST", "/app/devices/{device_id}/push-token", f"/app/devices/{phone_id}/push-token", OWNER, {"push_token": "fcm-target-1", "token_type": "fcm_token", "platform": "android"})
        approval_key = ed25519.Ed25519PrivateKey.generate()
        approval_public = approval_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        gw.call("POST", "/app/devices/{device_id}/approval-key", f"/app/devices/{phone_id}/approval-key", OWNER, {"public_key": "base64:" + base64.b64encode(approval_public).decode()})

        # Projects.
        project = gw.call("POST", "/app/projects", "/app/projects", OPERATOR, {"name": "Q3", "description": "reports", "metadata": {}, "source_device_id": desktop_id}).json()["project"]
        gw.call("GET", "/app/projects", "/app/projects", OPERATOR)
        gw.call("PATCH", "/app/projects/{project_id}", f"/app/projects/{project['project_id']}", OPERATOR, {"name": "Q3 reports"})
        gw.call("DELETE", "/app/projects/{project_id}", f"/app/projects/{project['project_id']}", OPERATOR)

        # Chat.
        conversation = gw.call("POST", "/app/conversations", "/app/conversations", OWNER, {"title": "reports", "source_device_id": phone_id}).json()["conversation"]
        conversation_path = f"/app/conversations/{conversation['conversation_id']}"
        gw.call("GET", "/app/conversations", "/app/conversations", OWNER)
        gw.call("POST", "/app/conversations/{conversation_id}/ask", f"{conversation_path}/ask", OWNER, {"content": "What changed?", "model_profile": "fast", "stream": False, "source_device_id": phone_id})
        gw.call("GET", "/app/conversations/{conversation_id}/messages", f"{conversation_path}/messages", OWNER)
        stream_request = {"conversation_id": conversation["conversation_id"], "content": "And now?", "model_profile": "fast"}
        jsonschema.validate(stream_request, _schema("POST", "/api/chat/stream", "request"))
        stream = client.post("/api/chat/stream", headers={**OWNER, CONTRACT_HEADER: CONTRACT_VERSION, "idempotency-key": secrets.token_hex(8), "accept": "text/event-stream"}, json=stream_request)
        assert stream.status_code == 200 and stream.headers["content-type"].startswith("text/event-stream")
        events = [line.removeprefix("event: ") for line in stream.text.splitlines() if line.startswith("event: ")]
        assert "chat.completed" in events, events
        gw.checked.add(("POST", "/api/chat/stream"))

        # An executable desktop task: requested from the phone, approved, claimed, reported.
        action = {"capability": "workspace.read_file", "scope": {"workspace": "reports", "relative_path": "q3.md"}}
        gw.call("POST", "/app/conversations/{conversation_id}/messages", f"{conversation_path}/messages", OWNER, {"content": "send me the Q3 report", "requires_desktop_runtime": True, "risk": "high", "action": action})
        snapshot = gw.call("GET", "/app/bootstrap", "/app/bootstrap", OWNER).json()
        approval_id = snapshot["pending_approvals"][0]["approval_id"]
        gw.call("GET", "/app/approvals", "/app/approvals?status=pending", OWNER)
        gw.call("GET", "/app/notifications", "/app/notifications?audience=mobile", OWNER)
        # Signed with the phone's approval key, as the mobile app does once it registered one.
        timestamp, nonce = str(int(time.time() * 1000)), secrets.token_hex(24)
        signature = approval_key.sign(f"omnidesk-approval-decision:v1:{approval_id}:approved:{timestamp}:{nonce}".encode())
        approval_headers = {"x-omnidesk-approval-timestamp": timestamp, "x-omnidesk-approval-nonce": nonce, "x-omnidesk-approval-signature": "base64:" + base64.b64encode(signature).decode()}
        gw.call("POST", "/app/approvals/{approval_id}/decide", f"/app/approvals/{approval_id}/decide", OWNER, {"decision": "approved", "reason": "fine", "source_device_id": phone_id}, extra=approval_headers)
        gw.call("POST", "/app/runtime/desktop/heartbeat", "/app/runtime/desktop/heartbeat", OPERATOR, {"device_id": desktop_id, "status": "online", "version": "1.12.7", "capabilities": ["workspace.read_file"]})
        task = gw.call("POST", "/app/runtime/desktop/claim", "/app/runtime/desktop/claim", OPERATOR, {"device_id": desktop_id, "capabilities": ["workspace.read_file"], "lease_seconds": 120}).json()["task"]
        gw.call("POST", "/app/tasks/{task_id}/status", f"/app/tasks/{task['task_id']}/status", OPERATOR, {"status": "completed", "result_summary": "workspace file read", "assigned_runtime_device_id": desktop_id, "result": {"content": "Q3", "truncated": False}})
        # Nothing left to claim: the contract allows a null task.
        gw.call("POST", "/app/runtime/desktop/claim", "/app/runtime/desktop/claim", OPERATOR, {"device_id": desktop_id, "capabilities": ["workspace.read_file"], "lease_seconds": 120})
        gw.call("GET", "/app/sync", "/app/sync?since_seq=0", OPERATOR)

        # Web Admin's admin routes.
        gw.call("GET", "/admin/session/identity", "/admin/session/identity", OWNER)
        gw.call("GET", "/admin/channels/ecosystem", "/admin/channels/ecosystem", OWNER)

        client_routes = {(e["method"], e["path"]) for e in CONTRACT["endpoints"] if e.get("client_surfaces")}
        assert client_routes - gw.checked == set()


def test_a_response_missing_a_field_clients_read_fails_validation() -> None:
    # The schemas carry the fields the clients read; losing one is caught.
    with pytest.raises(jsonschema.ValidationError, match="conversation_id"):
        jsonschema.validate({"conversation": {"title": "t"}}, _schema("POST", "/app/conversations", "response"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"task": {"task_id": 5, "title": "t", "action": {}}}, _schema("POST", "/app/runtime/desktop/claim", "response"))


def test_the_generated_client_modules_are_current() -> None:
    assert generator.main([str(ROOT), "--check"]) == 0
    assert CONTRACT["contract_version"] == CONTRACT_VERSION
    responses, _ = generator.tables(CONTRACT)
    assert ("task", ["object", "null"]) in responses["POST /app/runtime/desktop/claim"]
    assert ("pending_approvals[].approval_id", ["string"]) in responses["GET /app/bootstrap"]
    assert json.loads(json.dumps(responses))  # tables are plain JSON
