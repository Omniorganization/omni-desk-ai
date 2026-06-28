from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request

from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import (
    BigSellerConfigurationError,
    BigSellerDisabledError,
)
from omnidesk_agent.integrations.bigseller.schemas import BigSellerFulfillmentUpdate
from omnidesk_agent.integrations.bigseller.webhooks import parse_bigseller_webhook
from omnidesk_agent.integrations.bigseller.worker import BigSellerConnectorContext

AdminVerifier = Callable[[Request, str], Awaitable[object]]


def _status_code_for_error(exc: Exception) -> int:
    if isinstance(exc, BigSellerDisabledError):
        return 409
    if isinstance(exc, BigSellerConfigurationError):
        return 503
    if isinstance(exc, PermissionError):
        return 403
    if isinstance(exc, KeyError):
        return 404
    return 500


def _error_code_for_error(exc: Exception) -> str:
    if isinstance(exc, BigSellerDisabledError):
        return "BIGSELLER_DISABLED"
    if isinstance(exc, BigSellerConfigurationError):
        return "BIGSELLER_CONFIGURATION_ERROR"
    if isinstance(exc, PermissionError):
        return "BIGSELLER_REQUEST_REJECTED"
    if isinstance(exc, KeyError):
        return "BIGSELLER_NOT_FOUND"
    return "BIGSELLER_OPERATION_FAILED"


def _safe_message_for_error(exc: Exception) -> str:
    if isinstance(exc, BigSellerDisabledError):
        return "BigSeller connector is disabled"
    if isinstance(exc, BigSellerConfigurationError):
        return "BigSeller connector is not ready"
    if isinstance(exc, PermissionError):
        return "BigSeller request was rejected"
    if isinstance(exc, KeyError):
        return "BigSeller resource was not found"
    return "BigSeller operation failed"


def _safe_error_detail(request: Request, exc: Exception) -> dict[str, object]:
    trace_id = (
        request.headers.get("x-request-id")
        or getattr(getattr(request, "state", None), "request_id", "")
        or ""
    )
    return {
        "code": _error_code_for_error(exc),
        "message": _safe_message_for_error(exc),
        "trace_id": str(trace_id),
        "retryable": isinstance(exc, (BigSellerConfigurationError, BigSellerDisabledError)),
    }


def _raise_connector_error(request: Request, exc: Exception) -> None:
    raise HTTPException(
        status_code=_status_code_for_error(exc),
        detail=_safe_error_detail(request, exc),
    ) from exc


def _content_length_exceeds(request: Request, max_body_bytes: int) -> bool:
    raw = request.headers.get("content-length")
    if not raw:
        return False
    try:
        return int(raw) > max_body_bytes
    except ValueError:
        return False


def create_bigseller_router(
    *,
    admin: Optional[AdminVerifier] = None,
    workspace_root: Path | None = None,
    context: BigSellerConnectorContext | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/integrations/bigseller", tags=["bigseller"])

    async def authorize(request: Request, role: str) -> None:
        if admin is not None:
            await admin(request, role)

    def connector(request: Request) -> BigSellerConnectorContext:
        existing = getattr(request.app.state, "bigseller_connector", None)
        if existing is None:
            existing = context or BigSellerConnectorContext.from_env(
                workspace_root=workspace_root
            )
            request.app.state.bigseller_connector = existing
        return existing

    @router.get("/health")
    async def health(request: Request):
        await authorize(request, "viewer")
        ctx = connector(request)
        health_data = ctx.config.health()
        return {
            "ok": bool(health_data["ready"]) or not ctx.config.enabled,
            **health_data,
        }

    @router.post("/sync/orders")
    async def sync_orders(request: Request):
        await authorize(request, "operator")
        ctx = connector(request)
        try:
            result = ctx.worker.sync_orders()
            return {"ok": result.failed == 0, "sync": result.model_dump(mode="json")}
        except Exception as exc:
            _raise_connector_error(request, exc)

    @router.post("/sync/inventory")
    async def sync_inventory(request: Request):
        await authorize(request, "operator")
        ctx = connector(request)
        try:
            result = ctx.worker.sync_inventory()
            return {"ok": result.failed == 0, "sync": result.model_dump(mode="json")}
        except Exception as exc:
            _raise_connector_error(request, exc)

    @router.post("/sync/fulfillment")
    async def sync_fulfillment(update: BigSellerFulfillmentUpdate, request: Request):
        await authorize(request, "operator")
        ctx = connector(request)
        try:
            return {"ok": True, "result": ctx.worker.sync_fulfillment_status(update)}
        except Exception as exc:
            _raise_connector_error(request, exc)

    @router.get("/sync/status")
    async def sync_status(request: Request):
        await authorize(request, "viewer")
        ctx = connector(request)
        return {"ok": True, **ctx.worker.status()}

    @router.get("/errors")
    async def list_errors(
        request: Request,
        status: str | None = Query(
            default=None, pattern="^(retryable|dead_letter|resolved)$"
        ),
        limit: int = Query(default=50, ge=1, le=500),
    ):
        await authorize(request, "viewer")
        ctx = connector(request)
        return {"ok": True, "errors": ctx.worker.list_errors(status=status, limit=limit)}

    @router.post("/errors/{error_id}/retry")
    async def retry_error(error_id: str, request: Request):
        await authorize(request, "operator")
        ctx = connector(request)
        try:
            return {"ok": True, **ctx.worker.retry_error(error_id)}
        except Exception as exc:
            _raise_connector_error(request, exc)

    @router.post("/errors/{error_id}/resolve")
    async def resolve_error(error_id: str, request: Request):
        await authorize(request, "operator")
        ctx = connector(request)
        try:
            return {"ok": True, **ctx.worker.resolve_error(error_id)}
        except Exception as exc:
            _raise_connector_error(request, exc)

    @router.post("/webhook")
    async def webhook(request: Request):
        ctx = connector(request)
        max_body_bytes = ctx.config.webhook_max_body_bytes
        if _content_length_exceeds(request, max_body_bytes):
            ctx.worker.note_webhook_rejected()
            raise HTTPException(status_code=413, detail="BigSeller webhook payload too large")
        body = await request.body()
        if len(body) > max_body_bytes:
            ctx.worker.note_webhook_rejected()
            raise HTTPException(status_code=413, detail="BigSeller webhook payload too large")
        try:
            event = parse_bigseller_webhook(body, request.headers, ctx.config)
            return ctx.worker.handle_webhook(event)
        except PermissionError as exc:
            ctx.worker.note_webhook_rejected()
            _raise_connector_error(request, exc)
        except Exception as exc:
            _raise_connector_error(request, exc)

    return router


def register_bigseller_routes(
    app: FastAPI, cfg=None, admin: Optional[AdminVerifier] = None
) -> None:
    workspace_root = getattr(getattr(cfg, "workspace", None), "root", None)
    if workspace_root is not None:
        workspace_root = Path(workspace_root)
    config = BigSellerConfig.from_env(workspace_root=workspace_root)
    app.state.bigseller_connector = BigSellerConnectorContext.from_config(config)
    app.include_router(
        create_bigseller_router(admin=admin, workspace_root=workspace_root)
    )
