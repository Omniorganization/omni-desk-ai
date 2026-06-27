from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.integrations.bigseller.audit import BigSellerAuditLogger
from omnidesk_agent.integrations.bigseller.client import (
    BigSellerClient,
    HttpBigSellerClient,
)
from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import (
    BigSellerConfigurationError,
    BigSellerDisabledError,
    BigSellerSyncErrorQueue,
)
from omnidesk_agent.integrations.bigseller.fulfillment import (
    BigSellerFulfillmentSyncService,
)
from omnidesk_agent.integrations.bigseller.idempotency import BigSellerIdempotencyGuard
from omnidesk_agent.integrations.bigseller.inventory import (
    BigSellerInventorySyncService,
)
from omnidesk_agent.integrations.bigseller.mapper import BigSellerSkuMapper
from omnidesk_agent.integrations.bigseller.mock_adapter import MockBigSellerAdapter
from omnidesk_agent.integrations.bigseller.orders import BigSellerOrderSyncService
from omnidesk_agent.integrations.bigseller.products import (
    BigSellerProductMappingService,
)
from omnidesk_agent.integrations.bigseller.schemas import (
    BigSellerFulfillmentUpdate,
    BigSellerSyncResult,
    BigSellerWebhookEvent,
)


class BigSellerSyncWorker:
    def __init__(
        self,
        config: BigSellerConfig,
        client: BigSellerClient,
        *,
        mapper: Optional[BigSellerSkuMapper] = None,
        idempotency: Optional[BigSellerIdempotencyGuard] = None,
        audit: Optional[BigSellerAuditLogger] = None,
        errors: Optional[BigSellerSyncErrorQueue] = None,
    ):
        self.config = config
        self.client = client
        self.mapper = mapper or BigSellerSkuMapper()
        self.idempotency = idempotency or BigSellerIdempotencyGuard()
        self.audit = audit or BigSellerAuditLogger(config.audit_log_path)
        self.errors = errors or BigSellerSyncErrorQueue(max_retries=config.max_retries)
        self.orders = BigSellerOrderSyncService(
            client, self.mapper, self.idempotency, self.audit, self.errors
        )
        self.inventory = BigSellerInventorySyncService(
            client, self.mapper, self.idempotency, self.audit, self.errors
        )
        self.products = BigSellerProductMappingService(client, self.mapper)
        self.fulfillment = BigSellerFulfillmentSyncService(
            client, self.idempotency, self.audit, self.errors
        )
        self.last_sync: dict[str, Any] = {}

    def _ensure_enabled_and_ready(self) -> None:
        if not self.config.enabled:
            raise BigSellerDisabledError("BigSeller connector is disabled")
        issues = self.config.real_mode_issues()
        if issues:
            raise BigSellerConfigurationError("; ".join(issues))

    def sync_orders(self, **filters: Any) -> BigSellerSyncResult:
        self._ensure_enabled_and_ready()
        result = self.orders.sync_orders(**filters)
        self.last_sync["orders"] = result.model_dump(mode="json")
        return result

    def sync_inventory(self, **filters: Any) -> BigSellerSyncResult:
        self._ensure_enabled_and_ready()
        result = self.inventory.sync_inventory(**filters)
        self.last_sync["inventory"] = result.model_dump(mode="json")
        return result

    def sync_fulfillment_status(
        self, update: BigSellerFulfillmentUpdate
    ) -> dict[str, Any]:
        self._ensure_enabled_and_ready()
        result = self.fulfillment.sync_status(update)
        self.last_sync["fulfillment"] = result.model_dump(mode="json")
        return result.model_dump(mode="json")

    def handle_webhook(self, event: BigSellerWebhookEvent) -> dict[str, Any]:
        self._ensure_enabled_and_ready()
        event_name = event.event_type.lower()
        if event_name.startswith("order"):
            result = (
                self.sync_orders(store_id=event.store_id)
                if event.store_id
                else self.sync_orders()
            )
            return {
                "ok": True,
                "handled": "orders",
                "sync": result.model_dump(mode="json"),
            }
        if event_name.startswith("inventory") or event_name.startswith("stock"):
            result = (
                self.sync_inventory(store_id=event.store_id)
                if event.store_id
                else self.sync_inventory()
            )
            return {
                "ok": True,
                "handled": "inventory",
                "sync": result.model_dump(mode="json"),
            }
        if event_name.startswith("fulfillment"):
            if not event.external_id or not event.store_id:
                return {
                    "ok": True,
                    "handled": "ignored",
                    "reason": "missing fulfillment identifiers",
                }
            update = BigSellerFulfillmentUpdate(
                external_order_id=event.external_id,
                store_id=event.store_id,
                status=str(event.payload.get("status") or "updated"),
                tracking_number=event.payload.get("tracking_number"),
                carrier=event.payload.get("carrier"),
            )
            return {
                "ok": True,
                "handled": "fulfillment",
                "result": self.sync_fulfillment_status(update),
            }
        return {"ok": True, "handled": "ignored", "event_type": event.event_type}

    def status(self) -> dict[str, Any]:
        return {
            "config": self.config.redacted(),
            "health": self.config.health(),
            "idempotency": self.idempotency.stats(),
            "errors": self.errors.stats(),
            "last_sync": self.last_sync,
            "recent_audit": [
                event.model_dump(mode="json") for event in self.audit.recent(limit=20)
            ],
        }


class BigSellerConnectorContext:
    def __init__(self, config: BigSellerConfig, worker: BigSellerSyncWorker):
        self.config = config
        self.worker = worker

    @classmethod
    def from_env(
        cls, *, workspace_root: Path | None = None
    ) -> "BigSellerConnectorContext":
        return cls.from_config(BigSellerConfig.from_env(workspace_root=workspace_root))

    @classmethod
    def from_config(cls, config: BigSellerConfig) -> "BigSellerConnectorContext":
        client: BigSellerClient
        if config.use_mock:
            client = MockBigSellerAdapter(config)
        else:
            client = HttpBigSellerClient(config)
        worker = BigSellerSyncWorker(config, client)
        return cls(config=config, worker=worker)
