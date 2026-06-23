from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.config import AppConfig
from omnidesk_agent.models.base import ModelResponse
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
    cfg.app_sync.allow_websocket_query_auth = True
    return cfg


def test_tri_app_store_links_desktop_mobile_web_business_line(tmp_path):
    store = AppSyncStore(tmp_path / "app_sync.json")
    desktop = store.register_device(actor="alice", device_id="desktop-1", device_type="desktop", name="MacBook", platform="macOS", capabilities=["local-runtime"])
    mobile = store.register_device(actor="alice", device_id="mobile-1", device_type="mobile", name="iPhone", platform="iOS", capabilities=["approval", "chat"])
    web = store.register_device(actor="alice", device_id="web-1", device_type="web_admin", name="Admin", platform="web", capabilities=["governance"])
    assert desktop["device_type"] == "desktop"
    assert mobile["device_type"] == "mobile"
    assert web["device_type"] == "web_admin"

    convo = store.create_conversation(actor="alice", title="Cross-device task", source_device_id="mobile-1")
    result = store.add_message_and_task(
        actor="alice",
        conversation_id=convo["conversation_id"],
        content="Open the browser on desktop and check campaign status",
        source_device_id="mobile-1",
        requires_desktop_runtime=True,
        risk="high",
    )
    approval = result["approval"]
    assert result["task"]["status"] == "blocked"
    assert approval["status"] == "pending"
    decided = store.decide_approval(approval_id=approval["approval_id"], actor="owner", decision="approved")
    assert decided["status"] == "approved"
    assert store.get_task(result["task"]["task_id"])["status"] == "queued"
    sync = store.sync_since(0)
    assert any(event["event_type"] == "approval.decided" for event in sync["events"])


def test_appsync_routes_create_task_approval_and_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "alice")
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "viewer-token")
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app) as client:
        operator_headers = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "alice"}
        owner_headers = {"authorization": "Bearer owner-token", "x-omnidesk-actor": "owner"}
        r = client.post("/app/devices/register", headers={**operator_headers, "idempotency-key": "register-desktop-1"}, json={"device_id": "desktop-1", "device_type": "desktop", "name": "Desktop", "platform": "macOS"})
        assert r.status_code == 200, r.text
        convo = client.post("/app/conversations", headers={**operator_headers, "idempotency-key": "conversation-1"}, json={"title": "Mobile to desktop"}).json()["conversation"]
        payload = {"content": "Run a desktop workflow", "requires_desktop_runtime": True, "risk": "high"}
        created = client.post(f"/app/conversations/{convo['conversation_id']}/messages", headers={**operator_headers, "idempotency-key": "message-1"}, json=payload)
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["approval"]["status"] == "pending"
        approvals = client.get("/app/approvals?status=pending", headers=operator_headers).json()["approvals"]
        assert approvals
        decided = client.post(f"/app/approvals/{approvals[0]['approval_id']}/decide", headers={**owner_headers, "idempotency-key": "approval-1"}, json={"decision": "approved"})
        assert decided.status_code == 200, decided.text
        sync = client.get("/app/sync?since_seq=0", headers=operator_headers).json()
        assert any(event["event_type"] == "approval.decided" for event in sync["events"])


def test_shared_app_api_contract_matches_backend_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "alice")
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "viewer-token")
    root = Path(__file__).resolve().parents[1]
    contract = json.loads((root / "apps" / "shared" / "omni-app-api.contract.json").read_text())
    app = create_app(_cfg(tmp_path))
    rest_routes = {
        (method, getattr(route, "path", ""))
        for route in app.routes
        for method in getattr(route, "methods", set())
        if getattr(route, "path", None)
    }
    websocket_routes = {
        getattr(route, "path", "")
        for route in app.routes
        if "WebSocket" in type(route).__name__ and getattr(route, "path", None)
    }

    assert set(contract["surfaces"]) == {"desktop", "mobile", "web_admin"}
    for endpoint in contract["endpoints"]:
        method = endpoint["method"]
        path = endpoint["path"]
        if method == "WS":
            assert path in websocket_routes
        else:
            assert (method, path) in rest_routes
        assert endpoint["role"] in {"viewer", "operator", "owner", "gateway-protected"}


def test_tri_app_client_files_are_present():
    root = Path(__file__).resolve().parents[1]
    expected = [
        root / "apps" / "desktop-tauri" / "src" / "App.tsx",
        root / "apps" / "desktop-tauri" / "src-tauri" / "tauri.conf.json",
        root / "apps" / "mobile-flutter" / "lib" / "main.dart",
        root / "apps" / "web-admin-next" / "app" / "page.tsx",
        root / "apps" / "shared" / "omni-app-api.contract.json",
    ]
    for path in expected:
        assert path.exists(), path


def test_appsync_idempotency_and_desktop_claim_lease(tmp_path):
    store = AppSyncStore(tmp_path / "app_sync.json")
    store.register_device(actor="alice", device_id="desktop-1", device_type="desktop", name="MacBook", platform="macOS", capabilities=["local-runtime"])
    convo = store.create_conversation(actor="alice", title="Idempotent task", source_device_id="mobile-1")
    first = store.add_message_and_task(
        actor="alice",
        conversation_id=convo["conversation_id"],
        content="Run the desktop workflow once",
        source_device_id="mobile-1",
        requires_desktop_runtime=True,
        risk="high",
        idempotency_key="mobile-op-001",
    )
    second = store.add_message_and_task(
        actor="alice",
        conversation_id=convo["conversation_id"],
        content="Run the desktop workflow once",
        source_device_id="mobile-1",
        requires_desktop_runtime=True,
        risk="high",
        idempotency_key="mobile-op-001",
    )
    assert first["task"]["task_id"] == second["task"]["task_id"]

    store.decide_approval(approval_id=first["approval"]["approval_id"], actor="owner", decision="approved", idempotency_key="approve-001")
    claimed = store.claim_next_task(actor="desktop", device_id="desktop-1", lease_seconds=60, capabilities=["local-runtime"])
    assert claimed is not None
    assert claimed["task_id"] == first["task"]["task_id"]
    assert claimed["status"] == "running"
    assert claimed["claimed_by_device_id"] == "desktop-1"
    assert claimed["lease_expires_at"] is not None


def test_appsync_store_records_audited_chat_turn(tmp_path):
    store = AppSyncStore(tmp_path / "app_sync.json")
    convo = store.create_conversation(actor="alice", title="Direct model chat", source_device_id="mobile-1")
    user_message = store.add_chat_user_message(actor="alice", conversation_id=convo["conversation_id"], content="Summarize status", source_device_id="mobile-1")
    assistant_message = store.add_assistant_message(
        actor="alice",
        conversation_id=convo["conversation_id"],
        content="Status is stable.",
        provider="fake",
        model="fake-chat",
        profile="fast",
        usage={"input_tokens": 3, "output_tokens": 4},
    )
    messages = store.list_messages(convo["conversation_id"], actor="alice")
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert user_message["task_id"] is None
    assert assistant_message["model_provider"] == "fake"
    assert assistant_message["model_name"] == "fake-chat"
    assert assistant_message["usage"]["output_tokens"] == 4
    assert assistant_message["trace_id"].startswith("trace_")
    sync = store.sync_since(0)
    assert any(event["event_type"] == "conversation.ask.completed" for event in sync["events"])


class FakeChatRouter:
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            text=f"assistant:{request.user}",
            provider="fake-provider",
            model="fake-chat-model",
            profile=request.metadata.get("profile", "fast"),
            usage={"input_tokens": 2, "output_tokens": 5},
        )


def test_appsync_ask_route_uses_model_router_and_persists_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "alice")
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "viewer-token")
    app = create_app(_cfg(tmp_path))
    fake_router = FakeChatRouter()
    app.state.runtime.model_router = fake_router
    with TestClient(app) as client:
        operator_headers = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "alice"}
        convo = client.post("/app/conversations", headers={**operator_headers, "idempotency-key": "conversation-chat-1"}, json={"title": "Ask"}).json()["conversation"]
        payload = {"content": "What changed?", "model_profile": "fast", "source_device_id": "mobile-1"}
        asked = client.post(f"/app/conversations/{convo['conversation_id']}/ask", headers={**operator_headers, "idempotency-key": "ask-1"}, json=payload)
        assert asked.status_code == 200, asked.text
        body = asked.json()
        assert body["assistant_message"]["content"] == "assistant:What changed?"
        assert body["assistant_message"]["model_provider"] == "fake-provider"
        assert body["audit_trace_id"].startswith("trace_")
        assert fake_router.requests[0].task == "chat"
        assert fake_router.requests[0].metadata["actor"] == "alice"
        replay = client.post(f"/app/conversations/{convo['conversation_id']}/ask", headers={**operator_headers, "idempotency-key": "ask-1"}, json=payload)
        assert replay.status_code == 200, replay.text
        assert len(fake_router.requests) == 1
        messages = client.get(f"/app/conversations/{convo['conversation_id']}/messages", headers=operator_headers).json()["messages"]
        assert [item["role"] for item in messages] == ["user", "assistant"]


def test_api_chat_alias_uses_audited_model_router_and_creates_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    app = create_app(_cfg(tmp_path))
    fake_router = FakeChatRouter()
    app.state.runtime.model_router = fake_router
    with TestClient(app) as client:
        operator_headers = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "alice", "idempotency-key": "api-chat-1"}
        asked = client.post("/api/chat", headers=operator_headers, json={"content": "Give me release status", "model_profile": "fast"})
        assert asked.status_code == 200, asked.text
        body = asked.json()
        assert body["conversation_id"]
        assert body["assistant_message"]["content"] == "assistant:Give me release status"
        assert body["audit_trace_id"].startswith("trace_")
        assert fake_router.requests[0].task == "chat"
        stream = client.post("/api/chat/stream", headers=operator_headers, json={"content": "stream this"})
        assert stream.status_code == 501


def test_api_chat_actor_rate_limit_blocks_repeated_model_spend(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    cfg = _cfg(tmp_path)
    cfg.api_resource_guard.chat_max_requests_per_actor = 1
    cfg.api_resource_guard.max_requests_per_actor = 10
    app = create_app(cfg)
    fake_router = FakeChatRouter()
    app.state.runtime.model_router = fake_router
    with TestClient(app) as client:
        headers = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "alice", "idempotency-key": "chat-limit-1"}
        first = client.post("/api/chat", headers=headers, json={"content": "first"})
        assert first.status_code == 200, first.text
        second = client.post("/api/chat", headers={**headers, "idempotency-key": "chat-limit-2"}, json={"content": "second"})
        assert second.status_code == 429
        assert second.json()["detail"] == "chat rate limit exceeded"
        assert len(fake_router.requests) == 1


def test_appsync_ask_route_rejects_viewer_role(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "viewer-token")
    app = create_app(_cfg(tmp_path))
    app.state.runtime.model_router = FakeChatRouter()
    with TestClient(app) as client:
        operator_headers = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "alice"}
        viewer_headers = {"authorization": "Bearer viewer-token", "x-omnidesk-actor": "alice"}
        convo = client.post("/app/conversations", headers={**operator_headers, "idempotency-key": "conversation-chat-2"}, json={"title": "Ask"}).json()["conversation"]
        denied = client.post(f"/app/conversations/{convo['conversation_id']}/ask", headers={**viewer_headers, "idempotency-key": "ask-viewer"}, json={"content": "hello"})
        assert denied.status_code == 403


def test_websocket_requires_authenticated_viewer_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "viewer-token")
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app) as client:
        with client.websocket_connect("/app/ws?token=viewer-token&actor=mobile") as ws:
            message = ws.receive_json()
            assert message["ok"] is True
            assert "sync_seq" in message


def test_appsync_idempotency_rejects_payload_mismatch(tmp_path):
    from omnidesk_agent.appsync.store import IdempotencyConflict

    store = AppSyncStore(tmp_path / "app_sync.json")
    convo = store.create_conversation(actor="alice", title="Idempotency conflict")
    first = store.add_message_and_task(
        actor="alice",
        conversation_id=convo["conversation_id"],
        content="same operation",
        idempotency_key="same-key",
        idempotency_payload={"content": "same operation"},
    )
    replay = store.add_message_and_task(
        actor="alice",
        conversation_id=convo["conversation_id"],
        content="same operation",
        idempotency_key="same-key",
        idempotency_payload={"content": "same operation"},
    )
    assert replay["task"]["task_id"] == first["task"]["task_id"]
    try:
        store.add_message_and_task(
            actor="alice",
            conversation_id=convo["conversation_id"],
            content="different operation",
            idempotency_key="same-key",
            idempotency_payload={"content": "different operation"},
        )
    except IdempotencyConflict:
        pass
    else:
        raise AssertionError("expected IdempotencyConflict")


def test_device_enrollment_and_push_registration(tmp_path):
    store = AppSyncStore(tmp_path / "app_sync.json")
    enrollment = store.start_device_enrollment(actor="owner", device_type="mobile", pairing_code="12345678")
    assert enrollment["status"] == "pending"
    completed = store.complete_device_enrollment(actor="alice", enrollment_id=enrollment["enrollment_id"], pairing_code="12345678", device_id="mobile-1", public_key="pk")
    assert completed["status"] == "completed"
    store.register_device(actor="alice", device_id="mobile-1", device_type="mobile", name="Phone", platform="iOS")
    device = store.register_push_token(actor="alice", device_id="mobile-1", push_token="push-token", platform="ios")
    assert device["push_token"] == "push-token"


def test_postgres_normalized_schema_contract():
    from omnidesk_agent.appsync.postgres_store import NORMALIZED_SCHEMA_SQL

    required_tables = [
        "omnidesk_appsync_organizations",
        "omnidesk_appsync_users",
        "omnidesk_appsync_devices",
        "omnidesk_appsync_conversations",
        "omnidesk_appsync_messages",
        "omnidesk_appsync_tasks",
        "omnidesk_appsync_approvals",
        "omnidesk_appsync_notifications",
        "omnidesk_appsync_runtime_status",
        "omnidesk_appsync_device_enrollments",
        "omnidesk_appsync_idempotency_keys",
        "omnidesk_appsync_sync_events",
        "omnidesk_appsync_task_leases",
    ]
    for table in required_tables:
        assert table in NORMALIZED_SCHEMA_SQL
    assert "model_provider TEXT" in NORMALIZED_SCHEMA_SQL
    assert "usage JSONB" in NORMALIZED_SCHEMA_SQL
    assert "trace_id TEXT" in NORMALIZED_SCHEMA_SQL
    assert "FOR UPDATE SKIP LOCKED" not in NORMALIZED_SCHEMA_SQL or "omnidesk_appsync_tasks_claim_idx" in NORMALIZED_SCHEMA_SQL
