from __future__ import annotations

import time
from typing import Any

from omnidesk_agent.integrations.bigseller.audit import BigSellerAuditLogger
from omnidesk_agent.integrations.bigseller.client import BigSellerClient
from omnidesk_agent.integrations.bigseller.errors import (
    BigSellerError,
    BigSellerSyncErrorQueue,
)
from omnidesk_agent.integrations.bigseller.idempotency import BigSellerIdempotencyGuard
from omnidesk_agent.integrations.bigseller.mapper import BigSellerSkuMapper
from omnidesk_agent.integrations.bigseller.schemas import (
    BigSellerAuditEvent,
    BigSellerOrder,
    BigSellerSyncResult,
    utc_now,
)


class BigSellerOrderSyncService:
    action_type = "order.sync"

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

    def sync_orders(self, **filters: Any) -> BigSellerSyncResult:
        started_at = utc_now()
        started = time.perf_counter()
        self.audit.append(
            BigSellerAuditEvent(
                event="sync.start",
                entity_type="order",
                action=self.action_type,
                status="started",
            )
        )
        processed = skipped = failed = queued_errors = 0
        details: list[dict[str, Any]] = []
        try:
            orders = self.client.list_orders(**filters)
        except Exception as exc:
            failed = 1
            queued_errors += 1
            code = getattr(exc, "code", "list_orders_failed")
            self.errors.enqueue(
                entity_type="order",
                external_id="bulk",
                store_id="unknown",
                action=self.action_type,
                payload={"filters": filters},
                error=exc,
                error_code=code,
            )
            self.audit.append(
                BigSellerAuditEvent(
                    event="sync.end",
                    entity_type="order",
                    action=self.action_type,
                    status="failed",
                    error_code=code,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            ended_at = utc_now()
            return BigSellerSyncResult(
                entity_type="order",
                started_at=started_at,
                ended_at=ended_at,
                total=0,
                processed=0,
                skipped=0,
                failed=failed,
                queued_errors=queued_errors,
                duration_ms=int((time.perf_counter() - started) * 1000),
                details=details,
            )

        for order in orders:
            did_process = self._process_order(order, details)
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
                entity_type="order",
                action=self.action_type,
                status=status,
                duration_ms=duration_ms,
            )
        )
        return BigSellerSyncResult(
            entity_type="order",
            started_at=started_at,
            ended_at=utc_now(),
            total=len(orders),
            processed=processed,
            skipped=skipped,
            failed=failed,
            queued_errors=queued_errors,
            duration_ms=duration_ms,
            details=details,
        )

    def _process_order(
        self, order: BigSellerOrder, details: list[dict[str, Any]]
    ) -> str:
        if not self.idempotency.claim(
            external_id=order.external_order_id,
            store_id=order.store_id,
            action_type=self.action_type,
        ):
            details.append(
                {
                    "external_id": order.external_order_id,
                    "store_id": order.store_id,
                    "status": "skipped_duplicate",
                }
            )
            return "skipped"
        started = time.perf_counter()
        try:
            mapped = self.mapper.map_order(order)
            self.idempotency.complete(
                external_id=order.external_order_id,
                store_id=order.store_id,
                action_type=self.action_type,
            )
            self.errors.resolve(
                entity_type="order",
                external_id=order.external_order_id,
                store_id=order.store_id,
                action=self.action_type,
            )
            self.audit.append(
                BigSellerAuditEvent(
                    event="entity.sync",
                    entity_type="order",
                    action=self.action_type,
                    status="success",
                    external_id=order.external_order_id,
                    store_id=order.store_id,
                    internal_id=mapped.internal_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            details.append(
                {
                    "external_id": order.external_order_id,
                    "store_id": order.store_id,
                    "internal_id": mapped.internal_id,
                    "status": "processed",
                }
            )
            return "processed"
        except Exception as exc:
            self.idempotency.release(
                external_id=order.external_order_id,
                store_id=order.store_id,
                action_type=self.action_type,
            )
            code = getattr(exc, "code", "order_sync_failed")
            if isinstance(exc, BigSellerError):
                error_code = exc.code
            else:
                error_code = code
            self.errors.enqueue(
                entity_type="order",
                external_id=order.external_order_id,
                store_id=order.store_id,
                action=self.action_type,
                payload=order.model_dump(mode="json"),
                error=exc,
                error_code=error_code,
            )
            self.audit.append(
                BigSellerAuditEvent(
                    event="entity.sync",
                    entity_type="order",
                    action=self.action_type,
                    status="failed",
                    external_id=order.external_order_id,
                    store_id=order.store_id,
                    error_code=error_code,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            details.append(
                {
                    "external_id": order.external_order_id,
                    "store_id": order.store_id,
                    "status": "failed",
                    "error_code": error_code,
                }
            )
            return "failed"
