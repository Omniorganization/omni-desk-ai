from __future__ import annotations

from typing import Any

import logging
import weakref

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnidesk_agent.appsync.chat_routes import register_first_class_chat_routes
from omnidesk_agent.appsync.projects import register_project_routes
from omnidesk_agent.appsync.routes import register_appsync_routes as _register_appsync_routes
from omnidesk_agent.appsync.store import AppSyncStore


logger = logging.getLogger(__name__)

# A request that waited too long for a row lock, a statement, or a pooled
# connection is retryable: the database is busy, not the request wrong.
DATABASE_BUSY_RETRY_AFTER_SECONDS = 2


def _database_busy_exceptions() -> tuple[type[BaseException], ...]:
    from omnidesk_agent.appsync.migrated_postgres_store import PoolTimeout

    busy: list[type[BaseException]] = [PoolTimeout]
    try:
        from psycopg import errors  # type: ignore
    except ModuleNotFoundError:
        return tuple(busy)
    return (*busy, errors.LockNotAvailable, errors.QueryCanceled)


def register_database_busy_handler(app: FastAPI) -> None:
    async def database_busy(request: Request, exc: Exception) -> JSONResponse:
        logger.warning(
            "database busy",
            extra={"path": request.url.path, "error_type": type(exc).__name__},
        )
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "database_busy", "retry_after_seconds": DATABASE_BUSY_RETRY_AFTER_SECONDS}},
            headers={"Retry-After": str(DATABASE_BUSY_RETRY_AFTER_SECONDS)},
        )

    for exception_type in _database_busy_exceptions():
        app.add_exception_handler(exception_type, database_busy)


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
    register_database_busy_handler(app)
    register_first_class_chat_routes(app, cfg, rt, metrics, admin)
    _register_appsync_routes(app, cfg, rt, metrics, admin)
    _remove_shadowed_chat_routes(app)
    register_project_routes(app, cfg, rt, metrics, admin)
    _start_retention(getattr(rt, "app_sync", None), metrics, getattr(rt, "repository_factory", None))
    _start_push_dispatch(app, getattr(rt, "app_sync", None), metrics)


def _start_push_dispatch(app: FastAPI, store: Any, metrics: Any) -> None:
    """Pushes queued for approvals, decisions and task updates are sent in the
    background when a real push provider is configured (see appsync.push)."""
    from omnidesk_agent.appsync.push import start_push_dispatcher

    dispatcher = start_push_dispatcher(store, metrics) if store is not None else None
    app.state.push_dispatcher = dispatcher
    if dispatcher is not None:
        weakref.finalize(app, dispatcher.stop)


def _start_retention(store: Any, metrics: Any, repositories: Any = None) -> None:
    """PostgreSQL stores prune their tables in the background; 0 turns it off
    (for example when a CronJob runs ``python -m omnidesk_agent.appsync.retention``).
    The core runtime state is pruned in the same pass when it is on PostgreSQL."""
    start = getattr(store, "start_retention", None)
    if not callable(start):
        return
    from omnidesk_agent.appsync.retention import retention_interval_seconds

    interval = retention_interval_seconds()
    if interval <= 0:
        return
    core = getattr(repositories, "run_core_state_retention", None)
    start(interval_seconds=interval, metrics=metrics, also={"core_state": core} if callable(core) else None)


__all__ = ["AppSyncStore", "register_appsync_routes"]
