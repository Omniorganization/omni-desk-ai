from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from omnidesk_agent.appsync.chat_routes import register_first_class_chat_routes
from omnidesk_agent.appsync.projects import register_project_routes
from omnidesk_agent.appsync.routes import register_appsync_routes as _register_appsync_routes
from omnidesk_agent.appsync.store import AppSyncStore


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
    register_project_routes(app, cfg, rt, metrics, admin)


__all__ = ["AppSyncStore", "register_appsync_routes"]
