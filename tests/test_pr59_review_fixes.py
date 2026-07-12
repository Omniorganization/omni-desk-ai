from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from omnidesk_agent.appsync.streaming import install_audited_stream_route
from omnidesk_agent.security import resource_guard as resource_guard_module


ROOT = Path(__file__).resolve().parents[1]


def _cfg(*, require_idempotency: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        app_sync=SimpleNamespace(require_idempotency=require_idempotency),
        api_resource_guard=SimpleNamespace(max_inflight_chat_requests=2),
    )


def test_stream_adapter_replaces_provisional_route_and_classifies_chat() -> None:
    app = FastAPI()

    @app.post("/api/chat")
    async def api_chat(request: Request):
        payload = await request.json()
        return {
            "conversation_id": payload.get("conversation_id") or "conv_test",
            "assistant_message": {"content": "ok"},
            "usage": {},
            "audit_trace_id": "trace_test",
        }

    @app.post("/api/chat/stream")
    async def provisional_stream():
        return {"provisional": True}

    install_audited_stream_route(app, _cfg())

    stream_routes = [
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/chat/stream"
        and "POST" in (getattr(route, "methods", None) or set())
    ]
    assert len(stream_routes) == 1
    assert stream_routes[0].endpoint.__module__ == "omnidesk_agent.appsync.streaming"
    assert resource_guard_module._route_class("/api/chat") == "chat"
    assert resource_guard_module._route_class("/api/chat/stream") == "chat"


def test_stream_rejects_invalid_writes_before_delegating_to_chat() -> None:
    app = FastAPI()
    calls = 0

    @app.post("/api/chat")
    async def api_chat(request: Request):
        nonlocal calls
        calls += 1
        payload = await request.json()
        assert payload["stream"] is False
        return {
            "conversation_id": "conv_idempotent",
            "assistant_message": {"content": "audited response"},
            "usage": {"output_tokens": 2},
            "audit_trace_id": "trace_idempotent",
        }

    @app.post("/api/chat/stream")
    async def provisional_stream():
        return {"provisional": True}

    install_audited_stream_route(app, _cfg())
    client = TestClient(app)

    missing_content = client.post(
        "/api/chat/stream",
        json={},
        headers={"idempotency-key": "stream-missing-content"},
    )
    assert missing_content.status_code == 422
    assert calls == 0

    missing_key = client.post("/api/chat/stream", json={"content": "hello"})
    assert missing_key.status_code == 428
    assert calls == 0

    response = client.post(
        "/api/chat/stream",
        json={"content": "hello"},
        headers={"idempotency-key": "stream-valid"},
    )
    assert response.status_code == 200
    assert "event: chat.started" in response.text
    assert "event: chat.completed" in response.text
    assert calls == 1


def test_container_liveness_and_readiness_contracts_remain_distinct() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "urlopen('http://127.0.0.1:18789/health'" in dockerfile
    assert "Orchestrators use /ready" in dockerfile
    assert "urlopen('http://127.0.0.1:18789/ready'" not in dockerfile
