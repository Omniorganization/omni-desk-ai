from __future__ import annotations

from pathlib import Path
import time
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
    create_bigseller_error_queue,
)
from omnidesk_agent.integrations.bigseller.fulfillment import (
    BigSellerFulfillmentSyncService,
)
from omnidesk_agent.integrations.bigseller.idempotency import (
    BigSellerIdempotencyGuard,
    create_bigseller_idempotency_guard,
)
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
        self.idempotency = idempotency or create_bigseller_idempotency_guard(config)
        self.audit = audit or BigSellerAuditLogger(config.audit_log_path)
        self.errors = errors or create_bigseller_error_queue(config)
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
        self.metrics: dict[str, int] = {
            "bigseller_sync_orders_total": 0,
            "bigseller_sync_inventory_total": 0,
            "bigseller_sync_fulfillment_total": 0,
            "bigseller_webhook_received_total": 0,
            "bigseller_webhook_rejected_total": 0,
            "bigseller_webhook_duplicate_total": 0,
            "bigseller_dead_letter_total": 0,
        }
        self.last_durations_ms: dict[str, int] = {}

    def _inc(self, metric: str, amount: int = 1) -> None:
        self.metrics[metric] = self.metrics.get(metric, 0) + amount

    def _observe_duration(self, name: str, started: float) -> None:
        self.last_durations_ms[name] = int((time.time() - started) * 1000)

    def note_webhook_rejected(self) -> None:
        self._inc("bigseller_webhook_rejected_total")

    def _ensure_enabled_and_ready(self) -> None:
        if not self.config.enabled:
            raise BigSellerDisabledError("BigSeller connector is disabled")
        issues = self.config.real_mode_issues()
        if issues:
            raise BigSellerConfigurationError("; ".join(issues))

    def sync_orders(self, **filters: Any) -> BigSellerSyncResult:
        self._ensure_enabled_and_ready()
        started = time.time()
        result = self.orders.sync_orders(**filters)
        self._inc("bigseller_sync_orders_total")
        self._observe_duration("orders", started)
        self.last_sync["orders"] = result.model_dump(mode="json")
        self._inc("bigseller_dead_letter_total", self.errors.stats()["dead_letter"])
        return result

    def sync_inventory(self, **filters: Any) -> BigSellerSyncResult:
        self._ensure_enabled_and_ready()
        started = time.time()
        result = self.inventory.sync_inventory(**filters)
        self._inc("bigseller_sync_inventory_total")
        self._observe_duration("inventory", started)
        self.last_sync["inventory"] = result.model_dump(mode="json")
        self._inc("bigseller_dead_letter_total", self.errors.stats()["dead_letter"])
        return result

    def sync_fulfillment_status(
        self, update: BigSellerFulfillmentUpdate
    ) -> dict[str, Any]:
        self._ensure_enabled_and_ready()
        started = time.time()
        result = self.fulfillment.sync_status(update)
        self._inc("bigseller_sync_fulfillment_total")
        self._observe_duration("fulfillment", started)
        self.last_sync["fulfillment"] = result.model_dump(mode="json")
        return result.model_dump(mode="json")

    def _claim_webhook_event(self, event: BigSellerWebhookEvent) -> bool:
        if not event.event_id:
            return True
        return self.idempotency.claim(
            external_id=event.event_id,
            store_id=event.store_id or "__unknown_store__",
            action_type="webhook_event",
        )

    def _complete_webhook_event(self, event: BigSellerWebhookEvent) -> None:
        if event.event_id:
            self.idempotency.complete(
                external_id=event.event_id,
                store_id=event.store_id or "__unknown_store__",
                action_type="webhook_event",
            )

    def _release_webhook_event(self, event: BigSellerWebhookEvent) -> None:
        if event.event_id:
            self.idempotency.release(
                external_id=event.event_id,
                store_id=event.store_id or "__unknown_store__",
                action_type="webhook_event",
            )

    def handle_webhook(self, event: BigSellerWebhookEvent) -> dict[str, Any]:
        self._ensure_enabled_and_ready()
        self._inc("bigseller_webhook_received_total")
        if not self._claim_webhook_event(event):
            self._inc("bigseller_webhook_duplicate_total")
            return {
                "ok": True,
                "handled": "duplicate",
                "event_id": event.event_id,
            }
        try:
            event_name = event.event_type.lower()
            if event_name.startswith("order"):
                result = (
                    self.sync_orders(store_id=event.store_id)
                    if event.store_id
                    else self.sync_orders()
                )
                self._complete_webhook_event(event)
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
                self._complete_webhook_event(event)
                return {
                    "ok": True,
                    "handled": "inventory",
                    "sync": result.model_dump(mode="json"),
                }
            if event_name.startswith("fulfillment"):
                if not event.external_id or not event.store_id:
                    self._complete_webhook_event(event)
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
                result = self.sync_fulfillment_status(update)
                self._complete_webhook_event(event)
                return {
                    "ok": True,
                    "handled": "fulfillment",
                    "result": result,
                }
            self._complete_webhook_event(event)
            return {"ok": True, "handled": "ignored", "event_type": event.event_type}
        except Exception:
            self._release_webhook_event(event)
            raise

    def status(self) -> dict[str, Any]:
        return {
            "config": self.config.redacted(),
            "health": self.config.health(),
            "idempotency": self.idempotency.stats(),
            "errors": self.errors.stats(),
            "metrics": self.metrics,
            "last_durations_ms": self.last_durations_ms,
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
