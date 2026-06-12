from __future__ import annotations

from contextlib import asynccontextmanager

import time
import weakref
from fastapi import FastAPI, HTTPException, Request

from omnidesk_agent import __version__
from omnidesk_agent.config import AppConfig
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.observability import (
    JsonEventLogger,
    MetricsRegistry,
    initialize_runtime_metrics,
    new_request_id,
    public_runtime_status,
)
from omnidesk_agent.self_upgrade.dashboard.upgrade_dashboard import create_dashboard_router
from omnidesk_agent.server_routes.admin_routes import register_admin_routes
from omnidesk_agent.server_routes.agent_routes import register_agent_routes
from omnidesk_agent.server_routes.webhook_guard import WebhookGuard
from omnidesk_agent.server_routes.webhook_routes import register_webhook_routes
from omnidesk_agent.validation.production import assert_production_config_safe


def _wire_runtime_metrics(rt: OmniDeskRuntime, metrics: MetricsRegistry) -> None:
    initialize_runtime_metrics(metrics)
    rt.metrics = metrics
    rt.job_queue.metrics = metrics
    rt.outbound_messages.metrics = metrics
    rt.orchestrator.metrics = metrics
    rt.permissions.metrics = metrics
    rt.tools.metrics = metrics
    if getattr(rt, "learning_loop", None) is not None:
        rt.learning_loop.metrics = metrics


def create_app(cfg: AppConfig) -> FastAPI:
    assert_production_config_safe(cfg)
    rt = OmniDeskRuntime(cfg)
    approvals = rt.approval_store

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await rt.start()
        try:
            yield
        finally:
            await rt.aclose()

    app = FastAPI(title="OmniDesk Agent Gateway", lifespan=lifespan)
    weakref.finalize(app, rt.close)
    metrics = MetricsRegistry()
    event_logger = JsonEventLogger()
    app.state.metrics = metrics
    app.state.runtime = rt
    _wire_runtime_metrics(rt, metrics)

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = request.headers.get(cfg.observability.request_id_header) or new_request_id()
        request.state.request_id = request_id
        started = time.time()
        try:
            response = await call_next(request)
            metrics.inc("omnidesk_http_requests_total", method=request.method, path=request.url.path, status=getattr(response, "status_code", 0))
            return response
        except Exception:
            metrics.inc("omnidesk_http_errors_total", method=request.method, path=request.url.path)
            raise
        finally:
            elapsed = time.time() - started
            metrics.set("omnidesk_http_last_latency_seconds", elapsed, path=request.url.path)
            event_logger.event("http_request", request_id=request_id, method=request.method, path=request.url.path, elapsed=elapsed)

    async def _admin(request: Request, role: str = "viewer") -> None:
        decision = await rt.admin_auth.verify_request(request, required_role=role)
        if not decision.ok:
            raise HTTPException(status_code=403, detail=decision.reason)

    dashboard_router = create_dashboard_router(rt, admin_auth=rt.admin_auth)
    if dashboard_router is not None:
        app.include_router(dashboard_router)

    @app.get("/health")
    async def health():
        return public_runtime_status(__version__)

    register_admin_routes(app, cfg, rt, metrics, __version__, _admin)
    register_agent_routes(app, cfg, rt, approvals, _admin)
    register_webhook_routes(app, cfg, rt, WebhookGuard(cfg, rt))
    return app
