from __future__ import annotations

import time
from typing import Any

from omnidesk_agent.integrations.bigseller.audit import BigSellerAuditLogger
from omnidesk_agent.integrations.bigseller.client import BigSellerClient
from omnidesk_agent.integrations.bigseller.errors import BigSellerSyncErrorQueue
from omnidesk_agent.integrations.bigseller.idempotency import BigSellerIdempotencyGuard
from omnidesk_agent.integrations.bigseller.mapper import BigSellerSkuMapper
from omnidesk_agent.integrations.bigseller.schemas import (
    BigSellerAuditEvent,
    BigSellerInventoryItem,
    BigSellerSyncResult,
    utc_now,
)


class BigSellerInventorySyncService:
    action_type = "inventory.sync"

    def __init__(
        self,
        client: BigSellerClient,
        mapper: BigSellerSkuMapper,
        idempotency: BigSellerIdempotencyGuard,
        audit: BigSellerAuditLogger,
        errors: BigSellerSyncErrorQueue,
    ):
        self.client = client
        self.mapper = mapper
        self.idempotency = idempotency
        self.audit = audit
        self.errors = errors

    def sync_inventory(self, **filters: Any) -> BigSellerSyncResult:
        started_at = utc_now()
        started = time.perf_counter()
        self.audit.append(
            BigSellerAuditEvent(
                event="sync.start",
                entity_type="inventory",
                action=self.action_type,
                status="started",
            )
        )
        processed = skipped = failed = queued_errors = 0
        details: list[dict[str, Any]] = []
        try:
            inventory_items = self.client.list_inventory(**filters)
        except Exception as exc:
            code = getattr(exc, "code", "list_inventory_failed")
            self.errors.enqueue(
                entity_type="inventory",
                external_id="bulk",
                store_id="unknown",
                action=self.action_type,
                payload={"filters": filters},
                error=exc,
                error_code=code,
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.audit.append(
                BigSellerAuditEvent(
                    event="sync.end",
                    entity_type="inventory",
                    action=self.action_type,
                    status="failed",
                    error_code=code,
                    duration_ms=duration_ms,
                )
            )
            return BigSellerSyncResult(
                entity_type="inventory",
                started_at=started_at,
                ended_at=utc_now(),
                failed=1,
                queued_errors=1,
                duration_ms=duration_ms,
            )

        for item in inventory_items:
            did_process = self._process_inventory(item, details)
            if did_process == "processed":
                processed += 1
            elif did_process == "skipped":
                skipped += 1
            else:
                failed += 1
                queued_errors += 1

        duration_ms = int((time.perf_counter() - started) * 1000)
        status = "completed" if failed == 0 else "partial"
        self.audit.append(
            BigSellerAuditEvent(
                event="sync.end",
                entity_type="inventory",
                action=self.action_type,
                status=status,
                duration_ms=duration_ms,
            )
        )
        return BigSellerSyncResult(
            entity_type="inventory",
            started_at=started_at,
            ended_at=utc_now(),
            total=len(inventory_items),
            processed=processed,
            skipped=skipped,
            failed=failed,
            queued_errors=queued_errors,
            duration_ms=duration_ms,
            details=details,
        )

    def _process_inventory(
        self, item: BigSellerInventoryItem, details: list[dict[str, Any]]
    ) -> str:
        if not self.idempotency.claim(
            external_id=item.external_sku,
            store_id=item.store_id,
            action_type=self.action_type,
        ):
            details.append(
                {
                    "external_id": item.external_sku,
                    "store_id": item.store_id,
                    "status": "skipped_duplicate",
                }
            )
            return "skipped"
        started = time.perf_counter()
        try:
            mapped = self.mapper.map_inventory(item)
            self.idempotency.complete(
                external_id=item.external_sku,
                store_id=item.store_id,
                action_type=self.action_type,
            )
            self.errors.resolve(
                entity_type="inventory",
                external_id=item.external_sku,
                store_id=item.store_id,
                action=self.action_type,
            )
            self.audit.append(
                BigSellerAuditEvent(
                    event="entity.sync",
                    entity_type="inventory",
                    action=self.action_type,
                    status="success",
                    external_id=item.external_sku,
                    store_id=item.store_id,
                    internal_id=mapped.internal_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            details.append(
                {
                    "external_id": item.external_sku,
                    "store_id": item.store_id,
                    "internal_id": mapped.internal_id,
                    "status": "processed",
                }
            )
            return "processed"
        except Exception as exc:
            self.idempotency.release(
                external_id=item.external_sku,
                store_id=item.store_id,
                action_type=self.action_type,
            )
            code = getattr(exc, "code", "inventory_sync_failed")
            self.errors.enqueue(
                entity_type="inventory",
                external_id=item.external_sku,
                store_id=item.store_id,
                action=self.action_type,
                payload=item.model_dump(mode="json"),
                error=exc,
                error_code=code,
            )
            self.audit.append(
                BigSellerAuditEvent(
                    event="entity.sync",
                    entity_type="inventory",
                    action=self.action_type,
                    status="failed",
                    external_id=item.external_sku,
                    store_id=item.store_id,
                    error_code=code,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            details.append(
                {
                    "external_id": item.external_sku,
                    "store_id": item.store_id,
                    "status": "failed",
                    "error_code": code,
                }
            )
            return "failed"
