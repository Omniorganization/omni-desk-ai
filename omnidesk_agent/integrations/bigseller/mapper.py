from __future__ import annotations

import re

from omnidesk_agent.integrations.bigseller.schemas import (
    BigSellerInventoryItem,
    BigSellerMappedEntity,
    BigSellerOrder,
    BigSellerProduct,
)


def _stable_id(prefix: str, store_id: str, external_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.:-]+", "-", f"{store_id}:{external_id}").strip("-")
    return f"{prefix}:{clean}"


class BigSellerSkuMapper:
    """Maps external BigSeller product/SKU/order identities to Omni IDs."""

    def __init__(self):
        self._sku_map: dict[tuple[str, str], str] = {}
        self._product_map: dict[tuple[str, str], str] = {}
        self._order_map: dict[tuple[str, str], str] = {}

    def register_sku(
        self, *, store_id: str, external_sku: str, internal_sku: str
    ) -> None:
        self._sku_map[(store_id, external_sku)] = internal_sku

    def map_sku(self, *, store_id: str, external_sku: str) -> str:
        return self._sku_map.setdefault(
            (store_id, external_sku), _stable_id("sku", store_id, external_sku)
        )

    def map_order(self, order: BigSellerOrder) -> BigSellerMappedEntity:
        internal_id = self._order_map.setdefault(
            (order.store_id, order.external_order_id),
            _stable_id("order", order.store_id, order.external_order_id),
        )
        return BigSellerMappedEntity(
            entity_type="order",
            external_id=order.external_order_id,
            store_id=order.store_id,
            internal_id=internal_id,
            metadata={"sku_count": len(order.items), "status": order.status},
        )

    def map_inventory(self, inventory: BigSellerInventoryItem) -> BigSellerMappedEntity:
        internal_sku = self.map_sku(
            store_id=inventory.store_id, external_sku=inventory.external_sku
        )
        return BigSellerMappedEntity(
            entity_type="inventory",
            external_id=inventory.external_sku,
            store_id=inventory.store_id,
            internal_id=internal_sku,
            metadata={"available": inventory.available, "reserved": inventory.reserved},
        )

    def map_product(self, product: BigSellerProduct) -> BigSellerMappedEntity:
        internal_id = self._product_map.setdefault(
            (product.store_id, product.external_product_id),
            _stable_id("product", product.store_id, product.external_product_id),
        )
        for sku in product.skus:
            self.map_sku(store_id=product.store_id, external_sku=sku)
        return BigSellerMappedEntity(
            entity_type="product",
            external_id=product.external_product_id,
            store_id=product.store_id,
            internal_id=internal_id,
            metadata={"sku_count": len(product.skus), "title": product.title},
        )
