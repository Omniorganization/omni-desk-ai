from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from omnidesk_agent.appsync.projects import register_project_routes
from omnidesk_agent.appsync.routes import register_appsync_routes as _register_appsync_routes
from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.appsync.streaming import install_audited_stream_route


def register_appsync_routes(app: FastAPI, cfg: Any, rt: Any, metrics: Any, admin: Any) -> None:
    _register_appsync_routes(app, cfg, rt, metrics, admin)
    register_project_routes(app, cfg, rt, metrics, admin)
    install_audited_stream_route(app, cfg)


__all__ = ["AppSyncStore", "register_appsync_routes"]
