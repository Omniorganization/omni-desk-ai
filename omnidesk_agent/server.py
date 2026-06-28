from __future__ import annotations

from contextlib import asynccontextmanager
import importlib

import time
import weakref
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from omnidesk_agent import __version__
from omnidesk_agent.config import AppConfig
from omnidesk_agent.daemon import OmniDeskRuntime
import os

from omnidesk_agent.observability import (
    JsonEventLogger,
    MetricsRegistry,
    initialize_runtime_metrics,
    new_request_id,
    public_runtime_status,
)
from omnidesk_agent.observability_otel import AsyncOTLPHttpExporter, OTLPHttpExporter, make_traceparent, parse_traceparent
from omnidesk_agent.observability_tracing import trace_span
from omnidesk_agent.self_upgrade.dashboard.upgrade_dashboard import create_dashboard_router
from omnidesk_agent.server_routes.admin_routes import register_admin_routes
from omnidesk_agent.server_routes.agent_routes import register_agent_routes, register_break_glass_routes
from omnidesk_agent.server_routes.webhook_guard import WebhookGuard
from omnidesk_agent.server_routes.webhook_routes import register_webhook_routes
from omnidesk_agent.appsync import register_appsync_routes
from omnidesk_agent.security.resource_guard import ApiResourceGuard
from omnidesk_agent.validation.production import assert_production_config_safe


TRUE_VALUES = {"1", "true", "yes", "y", "on"}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in TRUE_VALUES


def _register_optional_bigseller_routes(app: FastAPI, cfg: AppConfig, admin_auth):
    """Register optional BigSeller routes without expanding core Pyright scope.

    BigSeller is an optional private-approval integration. Loading it through a
    dynamic boundary keeps the gateway's core static type gate focused on the
    runtime, security, tools, self-upgrade, server, and daemon surfaces while
    preserving runtime route registration and connector-specific tests.
    """

    module = importlib.import_module("omnidesk_agent.api.routes.bigseller")
    register = getattr(module, "register_bigseller_routes")
    register(app, cfg, admin_auth)


def _wire_runtime_metrics(rt: OmniDeskRuntime, metrics: MetricsRegistry, otel_exporter: OTLPHttpExporter | None = None) -> None:
    initialize_runtime_metrics(metrics)
    rt.metrics = metrics
    rt.job_queue.metrics = metrics
    rt.outbound_messages.metrics = metrics
    rt.orchestrator.metrics = metrics
    rt.otel_exporter = otel_exporter
    rt.orchestrator.otel_exporter = otel_exporter
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
            exporter = getattr(app.state, "otel_exporter", None)
            close_exporter = getattr(exporter, "close", None)
            if callable(close_exporter):
                close_exporter()
            await rt.aclose()

    app = FastAPI(title="OmniDesk Agent Gateway", lifespan=lifespan)
    weakref.finalize(app, rt.close)
    metrics = MetricsRegistry()
    event_logger = JsonEventLogger()
    resource_guard = ApiResourceGuard(cfg.api_resource_guard)
    otel_endpoint = os.getenv(cfg.observability.otlp_endpoint_env, "")
    otel_exporter = AsyncOTLPHttpExporter(endpoint=otel_endpoint, timeout=cfg.observability.otlp_timeout_seconds) if otel_endpoint else None
    app.state.metrics = metrics
    app.state.runtime = rt
    app.state.otel_exporter = otel_exporter
    app.state.resource_guard = resource_guard
    _wire_runtime_metrics(rt, metrics, otel_exporter)

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = request.headers.get(cfg.observability.request_id_header) or new_request_id()
        parsed_trace = parse_traceparent(request.headers.get("traceparent"))
        trace_id = parsed_trace["trace_id"] if parsed_trace else request_id.replace("-", "")[:32].ljust(32, "0")
        request.state.request_id = request_id
        started = time.time()
        release_resource_guard = None
        try:
            try:
                release_resource_guard = await resource_guard.before_request(request)
            except HTTPException as exc:
                metrics.inc("omnidesk_http_resource_guard_denials_total", method=request.method, path=request.url.path, status=exc.status_code)
                event_logger.event("api_resource_guard_denied", request_id=request_id, trace_id=trace_id, method=request.method, path=request.url.path, status=exc.status_code, reason=str(exc.detail))
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            with trace_span(
                "http.request",
                trace_id=trace_id,
                metrics=metrics,
                logger=event_logger if cfg.observability.structured_json_logs else None,
                otel_exporter=otel_exporter,
                method=request.method,
                path=request.url.path,
                request_id=request_id,
            ) as span:
                response = await call_next(request)
                metrics.inc("omnidesk_http_requests_total", method=request.method, path=request.url.path, status=getattr(response, "status_code", 0))
                response.headers[cfg.observability.request_id_header] = request_id
                response.headers["traceparent"] = make_traceparent(span.trace_id, span.span_id)
                return response
        except Exception:
            metrics.inc("omnidesk_http_errors_total", method=request.method, path=request.url.path)
            raise
        finally:
            elapsed = time.time() - started
            metrics.set("omnidesk_http_last_latency_seconds", elapsed, path=request.url.path)
            metrics.observe("omnidesk_http_request_duration_seconds", elapsed, path=request.url.path, method=request.method)
            event_logger.event("http_request", request_id=request_id, trace_id=trace_id, method=request.method, path=request.url.path, elapsed=elapsed)
            if release_resource_guard is not None:
                release_resource_guard()

    async def _admin(request: Request, role: str = "viewer"):
        decision = await rt.admin_auth.verify_request(request, required_role=role)
        if not decision.ok:
            raise HTTPException(status_code=403, detail=decision.reason)
        resource_guard.check_authenticated(request, actor=getattr(decision, "actor", "unknown"), role=getattr(decision, "role", role))
        return decision

    def _readiness_snapshot() -> dict:
        multi_instance_safe = bool(getattr(rt.storage_plan, "multi_instance_safe", False))
        checks: dict[str, object] = {
            "runtime": True,
            "storage_backend": cfg.storage.backend,
            "multi_instance_safe": multi_instance_safe,
            "multi_instance_required": bool(cfg.storage.require_multi_instance_safe),
        }
        if cfg.storage.require_multi_instance_safe:
            checks["multi_instance_requirement"] = multi_instance_safe
        try:
            caps = getattr(rt.repository_factory, "capabilities", None)
            checks["repository"] = None if caps is None else caps.__dict__
            health = getattr(rt.repository_factory, "health_check", lambda: {"ok": True})()
            checks["runtime_state"] = health
            checks["database"] = bool(health.get("ok", False)) if isinstance(health, dict) else True
        except Exception as exc:
            checks["database"] = False
            checks["database_error"] = str(exc)[:200]

        checks["sandbox_backend"] = cfg.sandbox.backend
        if cfg.sandbox.backend == "remote_docker":
            runner_url = str(cfg.sandbox.runner_url or "").rstrip("/")
            checks["sandbox_runner_configured"] = bool(runner_url)
            if runner_url and os.getenv("OMNIDESK_STRICT_READINESS_SANDBOX", "1") != "0":
                try:
                    import urllib.request
                    with urllib.request.urlopen(runner_url + "/ready", timeout=2) as response:  # nosec B310 - operator-supplied internal runner URL
                        checks["sandbox_runner"] = response.status < 500
                except Exception as exc:
                    checks["sandbox_runner"] = False
                    checks["sandbox_runner_error"] = str(exc)[:200]
        else:
            checks["sandbox_runner_configured"] = True

        required_secret_envs = [cfg.gateway.admin_token_env, cfg.gateway.shared_secret_env]
        if cfg.storage.backend == "postgres":
            required_secret_envs.append(cfg.storage.postgres_dsn_env)
        if cfg.memory_privacy.encrypt_at_rest:
            required_secret_envs.append(cfg.memory_privacy.encryption_key_env)
        if cfg.sandbox.backend == "remote_docker":
            required_secret_envs.extend([cfg.sandbox.runner_token_env, cfg.sandbox.runner_hmac_secret_env])
        if cfg.permissions.break_glass_enabled:
            required_secret_envs.append(cfg.permissions.audit_checkpoint_hmac_key_env)
        missing = [name for name in dict.fromkeys(required_secret_envs) if not os.getenv(name, "")]
        checks["secrets"] = not missing
        if missing:
            checks["missing_secrets"] = missing

        checks["plugins"] = (not cfg.plugins.enabled) or bool(cfg.capabilities.plugins.enabled)
        checks["schema_version"] = True
        critical_checks = {
            "runtime",
            "database",
            "sandbox_runner_configured",
            "sandbox_runner",
            "secrets",
            "plugins",
            "schema_version",
            "multi_instance_requirement",
        }
        ok = all(value is not False for key, value in checks.items() if key in critical_checks)
        return {"ok": ok, "checks": checks}

    dashboard_router = create_dashboard_router(rt, admin_auth=rt.admin_auth)
    if dashboard_router is not None:
        app.include_router(dashboard_router)

    @app.get("/health")
    async def health():
        return public_runtime_status(__version__)

    @app.get("/ready")
    async def ready():
        snapshot = _readiness_snapshot()
        if not snapshot["ok"]:
            raise HTTPException(status_code=503, detail={"ok": False})
        return {"ok": True}

    @app.get("/admin/ready")
    async def admin_ready(request: Request):
        await _admin(request, "viewer")
        snapshot = _readiness_snapshot()
        if not snapshot["ok"]:
            raise HTTPException(status_code=503, detail=snapshot)
        return snapshot

    register_admin_routes(app, cfg, rt, metrics, __version__, _admin)
    register_agent_routes(app, cfg, rt, approvals, _admin)
    register_appsync_routes(app, cfg, rt, metrics, _admin)
    register_break_glass_routes(app, cfg, rt, _admin)
    register_webhook_routes(app, cfg, rt, WebhookGuard(cfg, rt))
    if _env_flag("BIGSELLER_REGISTER_ROUTES"):
        _register_optional_bigseller_routes(app, cfg, _admin)
    return app
