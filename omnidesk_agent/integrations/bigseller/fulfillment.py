from __future__ import annotations

import time

from omnidesk_agent.integrations.bigseller.audit import BigSellerAuditLogger
from omnidesk_agent.integrations.bigseller.client import BigSellerClient
from omnidesk_agent.integrations.bigseller.errors import BigSellerSyncErrorQueue
from omnidesk_agent.integrations.bigseller.idempotency import BigSellerIdempotencyGuard
from omnidesk_agent.integrations.bigseller.schemas import (
    BigSellerAuditEvent,
    BigSellerFulfillmentResult,
    BigSellerFulfillmentUpdate,
)


class BigSellerFulfillmentSyncService:
    action_type = "fulfillment.sync"

    def __init__(
        self,
        client: BigSellerClient,
        idempotency: BigSellerIdempotencyGuard,
        audit: BigSellerAuditLogger,
        errors: BigSellerSyncErrorQueue,
    ):
        self.client = client
        self.idempotency = idempotency
        self.audit = audit
        self.errors = errors

    def sync_status(
        self, update: BigSellerFulfillmentUpdate
    ) -> BigSellerFulfillmentResult:
        if not self.idempotency.claim(
            external_id=update.external_order_id,
            store_id=update.store_id,
            action_type=self.action_type,
        ):
            return BigSellerFulfillmentResult(
                external_order_id=update.external_order_id,
                store_id=update.store_id,
                status=update.status,
                accepted=True,
                raw={"skipped_duplicate": True},
            )
        started = time.perf_counter()
        try:
            result = self.client.sync_fulfillment_status(update)
            self.idempotency.complete(
                external_id=update.external_order_id,
                store_id=update.store_id,
                action_type=self.action_type,
            )
            self.audit.append(
                BigSellerAuditEvent(
                    event="entity.sync",
                    entity_type="fulfillment",
                    action=self.action_type,
                    status="success",
                    external_id=update.external_order_id,
                    store_id=update.store_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            return result
        except Exception as exc:
            self.idempotency.release(
                external_id=update.external_order_id,
                store_id=update.store_id,
                action_type=self.action_type,
            )
            code = getattr(exc, "code", "fulfillment_sync_failed")
            self.errors.enqueue(
                entity_type="fulfillment",
                external_id=update.external_order_id,
                store_id=update.store_id,
                action=self.action_type,
                payload=update.model_dump(mode="json"),
                error=exc,
                error_code=code,
            )
            self.audit.append(
                BigSellerAuditEvent(
                    event="entity.sync",
                    entity_type="fulfillment",
                    action=self.action_type,
                    status="failed",
                    external_id=update.external_order_id,
                    store_id=update.store_id,
                    error_code=code,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            raise
