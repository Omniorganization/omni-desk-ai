from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request

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
    return 500


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
            raise HTTPException(
                status_code=_status_code_for_error(exc), detail=str(exc)
            ) from exc

    @router.post("/sync/inventory")
    async def sync_inventory(request: Request):
        await authorize(request, "operator")
        ctx = connector(request)
        try:
            result = ctx.worker.sync_inventory()
            return {"ok": result.failed == 0, "sync": result.model_dump(mode="json")}
        except Exception as exc:
            raise HTTPException(
                status_code=_status_code_for_error(exc), detail=str(exc)
            ) from exc

    @router.post("/sync/fulfillment")
    async def sync_fulfillment(update: BigSellerFulfillmentUpdate, request: Request):
        await authorize(request, "operator")
        ctx = connector(request)
        try:
            return {"ok": True, "result": ctx.worker.sync_fulfillment_status(update)}
        except Exception as exc:
            raise HTTPException(
                status_code=_status_code_for_error(exc), detail=str(exc)
            ) from exc

    @router.get("/sync/status")
    async def sync_status(request: Request):
        await authorize(request, "viewer")
        ctx = connector(request)
        return {"ok": True, **ctx.worker.status()}

    @router.post("/webhook")
    async def webhook(request: Request):
        ctx = connector(request)
        body = await request.body()
        try:
            event = parse_bigseller_webhook(body, request.headers, ctx.config)
            return ctx.worker.handle_webhook(event)
        except PermissionError as exc:
            ctx.worker.note_webhook_rejected()
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=_status_code_for_error(exc), detail=str(exc)
            ) from exc

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
