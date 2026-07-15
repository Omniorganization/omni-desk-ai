from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from omnidesk_agent.appsync.chat_routes import register_first_class_chat_routes
from omnidesk_agent.appsync.projects import register_project_routes
from omnidesk_agent.appsync.routes import register_appsync_routes as _register_appsync_routes
from omnidesk_agent.appsync.store import AppSyncStore


CANONICAL_CHAT_ROUTE_KEYS = {
    ("POST", "/app/conversations/{conversation_id}/ask"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream"),
}


def _remove_shadowed_chat_routes(app: FastAPI) -> None:
    seen: set[tuple[str, str]] = set()
    retained = []
    for route in app.router.routes:
        path = str(getattr(route, "path", ""))
        methods = set(getattr(route, "methods", set()) or set())
        keys = {(method, path) for method in methods if (method, path) in CANONICAL_CHAT_ROUTE_KEYS}
        if keys and any(key in seen for key in keys):
            continue
        retained.append(route)
        seen.update(keys)
    app.router.routes[:] = retained


def register_appsync_routes(
    app: FastAPI,
    cfg: Any,
    rt: Any,
    metrics: Any,
    admin: Any,
) -> None:
    # Canonical chat routes are registered first and therefore own all chat
    # traffic. The legacy AppSync collection remains for non-chat endpoints while
    # it is split into smaller routers in subsequent maintenance work.
    register_first_class_chat_routes(app, cfg, rt, metrics, admin)
    _register_appsync_routes(app, cfg, rt, metrics, admin)
    _remove_shadowed_chat_routes(app)
    register_project_routes(app, cfg, rt, metrics, admin)


__all__ = ["AppSyncStore", "register_appsync_routes"]
