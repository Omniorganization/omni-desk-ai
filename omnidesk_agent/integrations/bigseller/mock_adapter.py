from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from omnidesk_agent.integrations.bigseller.auth import BigSellerTokenManager
from omnidesk_agent.integrations.bigseller.client import BigSellerClient
from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import BigSellerUnauthorizedError
from omnidesk_agent.integrations.bigseller.rate_limit import BigSellerRateLimiter
from omnidesk_agent.integrations.bigseller.schemas import (
    BigSellerFulfillmentResult,
    BigSellerFulfillmentUpdate,
    BigSellerInventoryItem,
    BigSellerOrder,
    BigSellerOrderItem,
    BigSellerProduct,
    BigSellerTokenState,
    utc_now,
)


class MockBigSellerAdapter(BigSellerClient):
    """Deterministic offline adapter used by default for development and tests."""

    def __init__(
        self,
        config: BigSellerConfig | None = None,
        *,
        token_manager: Optional[BigSellerTokenManager] = None,
        unauthorized_once: bool = False,
    ):
        self.config = config or BigSellerConfig(enabled=True, use_mock=True)
        self.token_manager = token_manager or BigSellerTokenManager.from_config(
            self.config
        )
        self.rate_limiter = BigSellerRateLimiter(
            per_minute=self.config.rate_limit_per_minute, sleeper=lambda _: None
        )
        self.refresh_count = 0
        self.exchange_count = 0
        self._unauthorized_once = unauthorized_once
        self._orders = [
            BigSellerOrder(
                external_order_id="BS-ORDER-1001",
                store_id="MY-STORE-1",
                status="paid",
                items=[
                    BigSellerOrderItem(
                        external_sku="SKU-BETTY-001",
                        quantity=2,
                        title="Betty Sample Tee",
                        unit_price=19.9,
                    ),
                    BigSellerOrderItem(
                        external_sku="SKU-YMWM127",
                        quantity=1,
                        title="YMWM127 Bundle",
                        unit_price=29.9,
                    ),
                ],
                raw={"mock": True},
            ),
            BigSellerOrder(
                external_order_id="BS-ORDER-1002",
                store_id="MY-STORE-1",
                status="packed",
                items=[
                    BigSellerOrderItem(
                        external_sku="SKU-BETTY-001",
                        quantity=1,
                        title="Betty Sample Tee",
                        unit_price=19.9,
                    )
                ],
                raw={"mock": True},
            ),
        ]
        self._inventory = {
            ("MY-STORE-1", "SKU-BETTY-001"): BigSellerInventoryItem(
                store_id="MY-STORE-1",
                external_sku="SKU-BETTY-001",
                available=42,
                reserved=3,
                raw={"mock": True},
            ),
            ("MY-STORE-1", "SKU-YMWM127"): BigSellerInventoryItem(
                store_id="MY-STORE-1",
                external_sku="SKU-YMWM127",
                available=8,
                reserved=1,
                raw={"mock": True},
            ),
        }
        self._products = [
            BigSellerProduct(
                external_product_id="BS-PRODUCT-1",
                store_id="MY-STORE-1",
                title="Betty Sample Tee",
                skus=["SKU-BETTY-001"],
                raw={"mock": True},
            ),
            BigSellerProduct(
                external_product_id="BS-PRODUCT-2",
                store_id="MY-STORE-1",
                title="YMWM127 Bundle",
                skus=["SKU-YMWM127"],
                raw={"mock": True},
            ),
        ]

    def exchange_auth_code(self) -> BigSellerTokenState:
        self.exchange_count += 1
        token = BigSellerTokenState(
            access_token="mock-access-token-exchanged",
            refresh_token="mock-refresh-token",
            expires_at=utc_now() + timedelta(hours=1),
        )
        self.token_manager.replace(token)
        return token

    def refresh_access_token(self) -> BigSellerTokenState:
        self.refresh_count += 1
        token = BigSellerTokenState(
            access_token=f"mock-access-token-refreshed-{self.refresh_count}",
            refresh_token=self.token_manager.token_state.refresh_token
            or "mock-refresh-token",
            expires_at=utc_now() + timedelta(hours=1),
        )
        self.token_manager.replace(token)
        return token

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        def _send() -> Any:
            self.rate_limiter.wait()
            self.token_manager.ensure_access_token(self.refresh_access_token)
            if self._unauthorized_once:
                self._unauthorized_once = False
                raise BigSellerUnauthorizedError("mock unauthorized")
            normalized = path.rstrip("/")
            if normalized == "/mock/orders":
                return [order.model_dump(mode="json") for order in self._orders]
            if normalized == "/mock/inventory":
                return [
                    item.model_dump(mode="json") for item in self._inventory.values()
                ]
            if normalized == "/mock/products":
                return [product.model_dump(mode="json") for product in self._products]
            return {"ok": True, "method": method.upper(), "path": path, "mock": True}

        try:
            return _send()
        except BigSellerUnauthorizedError:
            self.token_manager.force_refresh(self.refresh_access_token)
            return _send()

    def list_orders(self, **filters: Any) -> list[BigSellerOrder]:
        self.request("GET", "/mock/orders", params=filters)
        store_id = filters.get("store_id")
        if store_id:
            return [order for order in self._orders if order.store_id == store_id]
        return list(self._orders)

    def get_order(self, external_order_id: str, *, store_id: str) -> BigSellerOrder:
        self.request("GET", f"/mock/orders/{external_order_id}")
        for order in self._orders:
            if (
                order.external_order_id == external_order_id
                and order.store_id == store_id
            ):
                return order
        raise KeyError(external_order_id)

    def list_inventory(self, **filters: Any) -> list[BigSellerInventoryItem]:
        self.request("GET", "/mock/inventory", params=filters)
        store_id = filters.get("store_id")
        items = list(self._inventory.values())
        if store_id:
            items = [item for item in items if item.store_id == store_id]
        return items

    def update_inventory(self, item: BigSellerInventoryItem) -> BigSellerInventoryItem:
        self.request(
            "POST", "/mock/inventory/update", json=item.model_dump(mode="json")
        )
        updated = item.model_copy(
            update={"updated_at": utc_now(), "raw": {"mock": True, "updated": True}}
        )
        self._inventory[(item.store_id, item.external_sku)] = updated
        return updated

    def list_products(self, **filters: Any) -> list[BigSellerProduct]:
        self.request("GET", "/mock/products", params=filters)
        store_id = filters.get("store_id")
        if store_id:
            return [
                product for product in self._products if product.store_id == store_id
            ]
        return list(self._products)

    def get_product(
        self, external_product_id: str, *, store_id: str
    ) -> BigSellerProduct:
        self.request("GET", f"/mock/products/{external_product_id}")
        for product in self._products:
            if (
                product.external_product_id == external_product_id
                and product.store_id == store_id
            ):
                return product
        raise KeyError(external_product_id)

    def sync_fulfillment_status(
        self, update: BigSellerFulfillmentUpdate
    ) -> BigSellerFulfillmentResult:
        self.request(
            "POST", "/mock/fulfillment/status", json=update.model_dump(mode="json")
        )
        return BigSellerFulfillmentResult(
            external_order_id=update.external_order_id,
            store_id=update.store_id,
            status=update.status,
            accepted=True,
            raw={
                "mock": True,
                "tracking_number": update.tracking_number,
                "carrier": update.carrier,
            },
        )
