from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from omnidesk_agent.integrations.bigseller.client import HttpBigSellerClient
from omnidesk_agent.integrations.bigseller.config import BigSellerConfig


ALL_ENDPOINTS = {
    "auth_code_exchange_path": "/auth/exchange",
    "refresh_token_path": "/auth/refresh",
    "orders_list_path": "/orders",
    "order_detail_path": "/orders/{external_order_id}",
    "inventory_list_path": "/inventory",
    "inventory_update_path": "/inventory/{external_sku}",
    "products_list_path": "/products",
    "product_detail_path": "/products/{external_product_id}",
    "fulfillment_sync_path": "/orders/{external_order_id}/fulfillment",
}


def real_config(**overrides: Any) -> BigSellerConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "use_mock": False,
        "base_url": "https://bigseller.example.test",
        "allowed_hosts": ["bigseller.example.test"],
        "app_id": "app-id",
        "app_key": "app-key",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "token_expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "webhook_secret": "webhook-secret",
        "state_backend": "sqlite",
        "request_signing_enabled": True,
        **ALL_ENDPOINTS,
    }
    values.update(overrides)
    return BigSellerConfig(**values)


def test_real_mode_requires_configured_business_endpoints() -> None:
    config = real_config(orders_list_path=None)

    issues = config.real_mode_issues()

    assert any("BIGSELLER_ORDERS_LIST_PATH" in issue for issue in issues)
    assert not config.health()["ready"]


def test_real_mode_rejects_memory_state_backend() -> None:
    config = real_config(state_backend="memory")

    assert any(
        "BIGSELLER_STATE_BACKEND=memory" in issue for issue in config.real_mode_issues()
    )


def test_real_mode_requires_https_allowed_host_and_signing() -> None:
    config = real_config(
        base_url="http://127.0.0.1:8080",
        allowed_hosts=[],
        request_signing_enabled=False,
    )

    issues = config.real_mode_issues()

    assert any("BIGSELLER_BASE_URL must use https://" in issue for issue in issues)
    assert any("host must not be localhost" in issue for issue in issues)
    assert any("BIGSELLER_ALLOWED_HOSTS" in issue for issue in issues)
    assert any("BIGSELLER_REQUEST_SIGNING_ENABLED=true" in issue for issue in issues)


def test_real_mode_rejects_base_url_host_outside_allowlist() -> None:
    config = real_config(
        base_url="https://evil.example.test",
        allowed_hosts=["bigseller.example.test"],
    )

    assert any(
        "not in BIGSELLER_ALLOWED_HOSTS" in issue for issue in config.real_mode_issues()
    )


def test_real_adapter_maps_order_list_response(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Response:
        status_code = 200
        content = b"{}"

        def json(self) -> dict[str, Any]:
            return {
                "data": [
                    {
                        "order_id": "BS-1001",
                        "shop_id": "MY-STORE-1",
                        "order_status": "paid",
                        "items": [
                            {"seller_sku": "SKU-1", "qty": 2, "product_name": "Item"}
                        ],
                    }
                ]
            }

    def fake_request(method: str, url: str, **kwargs: Any) -> Response:
        captured["method"] = method
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Response()

    monkeypatch.setattr(httpx, "request", fake_request)
    client = HttpBigSellerClient(real_config())

    orders = client.list_orders(store_id="MY-STORE-1")

    assert captured["method"] == "GET"
    assert captured["url"] == "https://bigseller.example.test/orders"
    assert orders[0].external_order_id == "BS-1001"
    assert orders[0].store_id == "MY-STORE-1"
    assert orders[0].items[0].external_sku == "SKU-1"
    assert orders[0].items[0].quantity == 2


def test_real_adapter_adds_configured_signature_headers(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Response:
        status_code = 200
        content = b"{}"

        def json(self) -> dict[str, Any]:
            return {"data": []}

    def fake_request(method: str, url: str, **kwargs: Any) -> Response:
        captured["headers"] = kwargs["headers"]
        return Response()

    monkeypatch.setattr(httpx, "request", fake_request)
    client = HttpBigSellerClient(
        real_config(
            request_signing_enabled=True,
            signature_header="x-test-signature",
            signature_timestamp_header="x-test-ts",
            signature_app_id_header="x-test-app-id",
        )
    )

    client.list_inventory(store_id="MY-STORE-1")

    assert captured["headers"]["x-test-app-id"] == "app-id"
    assert captured["headers"]["x-test-ts"]
    assert captured["headers"]["x-test-signature"]
