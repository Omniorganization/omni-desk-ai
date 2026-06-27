from __future__ import annotations

from datetime import timedelta

import pytest

from omnidesk_agent.integrations.bigseller.client import HttpBigSellerClient
from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import (
    BigSellerEndpointNotConfigured,
    redact_secrets,
)
from omnidesk_agent.integrations.bigseller.mock_adapter import MockBigSellerAdapter
from omnidesk_agent.integrations.bigseller.schemas import (
    BigSellerInventoryItem,
    utc_now,
)


def test_mock_client_returns_deterministic_orders_inventory_products(tmp_path):
    config = BigSellerConfig(
        enabled=True, use_mock=True, audit_log_path=tmp_path / "audit.jsonl"
    )
    client = MockBigSellerAdapter(config)

    assert [order.external_order_id for order in client.list_orders()] == [
        "BS-ORDER-1001",
        "BS-ORDER-1002",
    ]
    assert [item.external_sku for item in client.list_inventory()] == [
        "SKU-BETTY-001",
        "SKU-YMWM127",
    ]
    assert [product.external_product_id for product in client.list_products()] == [
        "BS-PRODUCT-1",
        "BS-PRODUCT-2",
    ]


def test_mock_update_inventory_is_persistent_offline(tmp_path):
    config = BigSellerConfig(
        enabled=True, use_mock=True, audit_log_path=tmp_path / "audit.jsonl"
    )
    client = MockBigSellerAdapter(config)

    updated = client.update_inventory(
        BigSellerInventoryItem(
            store_id="MY-STORE-1", external_sku="SKU-BETTY-001", available=55
        )
    )

    assert updated.available == 55
    assert client.list_inventory()[0].available == 55


def test_real_client_business_methods_fail_closed_until_private_docs_are_added(
    tmp_path,
):
    config = BigSellerConfig(
        enabled=True,
        use_mock=False,
        base_url="https://bigseller-private.example",
        app_id="app-id",
        app_key="app-key",
        access_token="token",
        token_expires_at=utc_now() + timedelta(hours=1),
        audit_log_path=tmp_path / "audit.jsonl",
    )
    client = HttpBigSellerClient(config)

    with pytest.raises(BigSellerEndpointNotConfigured):
        client.list_orders()


def test_redaction_removes_raw_token_and_app_key_values():
    redacted = redact_secrets(
        {
            "access_token": "raw-access-token",
            "refresh_token": "raw-refresh-token",
            "nested": {"app_key": "raw-app-key"},
            "message": "authorization=Bearer abc123",
        }
    )

    assert "raw-access-token" not in str(redacted)
    assert "raw-refresh-token" not in str(redacted)
    assert "raw-app-key" not in str(redacted)
    assert "abc123" not in str(redacted)
