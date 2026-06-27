from __future__ import annotations

from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.worker import BigSellerConnectorContext


def test_inventory_sync_processes_inventory_and_is_idempotent(tmp_path):
    config = BigSellerConfig(
        enabled=True, use_mock=True, audit_log_path=tmp_path / "audit.jsonl"
    )
    context = BigSellerConnectorContext.from_config(config)

    first = context.worker.sync_inventory()
    second = context.worker.sync_inventory()

    assert first.total == 2
    assert first.processed == 2
    assert first.failed == 0
    assert second.skipped == 2
    assert context.worker.status()["idempotency"]["completed"] == 2


def test_product_sku_mapping_service_maps_mock_products(tmp_path):
    config = BigSellerConfig(
        enabled=True, use_mock=True, audit_log_path=tmp_path / "audit.jsonl"
    )
    context = BigSellerConnectorContext.from_config(config)

    mapped = context.worker.products.sync_product_mappings()

    assert [item.entity_type for item in mapped] == ["product", "product"]
    assert mapped[0].internal_id == "product:MY-STORE-1:BS-PRODUCT-1"
