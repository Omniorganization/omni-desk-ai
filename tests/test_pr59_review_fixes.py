from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request

from omnidesk_agent.appsync.chat_routes import register_first_class_chat_routes
from omnidesk_agent.security.chat_resource_guard import route_class


ROOT = Path(__file__).resolve().parents[1]


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        app_sync=SimpleNamespace(require_idempotency=True),
        api_resource_guard=SimpleNamespace(max_inflight_chat_requests=2),
    )


def test_first_class_chat_routes_are_registered_before_legacy_routes() -> None:
    app = FastAPI()
    runtime = SimpleNamespace(app_sync=object(), model_router=None)

    async def admin(_request: Request, _role: str):
        return SimpleNamespace(actor="operator-1", role="operator")

    register_first_class_chat_routes(app, _cfg(), runtime, None, admin)

    @app.post("/api/chat")
    async def legacy_chat():
        return {"legacy": True}

    @app.post("/api/chat/stream")
    async def legacy_stream():
        return {"legacy": True}

    chat_routes = [
        route
        for route in app.routes
        if getattr(route, "path", None) in {"/api/chat", "/api/chat/stream"}
        and "POST" in (getattr(route, "methods", None) or set())
    ]
    assert chat_routes[0].endpoint.__module__ == "omnidesk_agent.appsync.chat_routes"
    assert chat_routes[1].endpoint.__module__ == "omnidesk_agent.appsync.chat_routes"


def test_stream_route_classification_is_static_and_import_order_independent() -> None:
    assert route_class("/api/chat") == "chat"
    assert route_class("/api/chat/stream") == "chat"
    assert route_class("/app/conversations/conv-1/ask") == "chat"
    assert route_class("/agent/run") == "agent"
    assert route_class("/health") == "general"

    source = (ROOT / "omnidesk_agent/security/chat_resource_guard.py").read_text(
        encoding="utf-8"
    )
    assert "._route_class =" not in source
    assert "setattr(" not in source


def test_legacy_stream_route_replacement_module_is_removed() -> None:
    assert not (ROOT / "omnidesk_agent/appsync/streaming.py").exists()
    appsync_init = (ROOT / "omnidesk_agent/appsync/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "install_audited_stream_route" not in appsync_init
    assert "register_first_class_chat_routes" in appsync_init


def test_container_liveness_and_readiness_contracts_remain_distinct() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "urlopen('http://127.0.0.1:18789/health'" in dockerfile
    assert "Orchestrators use /ready" in dockerfile
    assert "urlopen('http://127.0.0.1:18789/ready'" not in dockerfile
