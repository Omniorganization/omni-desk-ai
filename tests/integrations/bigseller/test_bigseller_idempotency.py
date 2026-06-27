from __future__ import annotations

from omnidesk_agent.integrations.bigseller.idempotency import BigSellerIdempotencyGuard


def test_idempotency_key_uses_external_id_store_id_and_action_type():
    guard = BigSellerIdempotencyGuard()

    assert (
        guard.claim(external_id="order-1", store_id="store-a", action_type="order.sync")
        is True
    )
    assert (
        guard.claim(external_id="order-1", store_id="store-a", action_type="order.sync")
        is False
    )
    guard.complete(external_id="order-1", store_id="store-a", action_type="order.sync")
    assert (
        guard.claim(external_id="order-1", store_id="store-a", action_type="order.sync")
        is False
    )
    assert (
        guard.claim(
            external_id="order-1", store_id="store-a", action_type="fulfillment.sync"
        )
        is True
    )
    assert (
        guard.claim(external_id="order-1", store_id="store-b", action_type="order.sync")
        is True
    )

    assert guard.stats()["total"] == 3
