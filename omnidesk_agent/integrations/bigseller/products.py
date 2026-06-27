from __future__ import annotations

from typing import Any

from omnidesk_agent.integrations.bigseller.client import BigSellerClient
from omnidesk_agent.integrations.bigseller.mapper import BigSellerSkuMapper
from omnidesk_agent.integrations.bigseller.schemas import BigSellerMappedEntity


class BigSellerProductMappingService:
    def __init__(self, client: BigSellerClient, mapper: BigSellerSkuMapper):
        self.client = client
        self.mapper = mapper

    def sync_product_mappings(self, **filters: Any) -> list[BigSellerMappedEntity]:
        return [
            self.mapper.map_product(product)
            for product in self.client.list_products(**filters)
        ]
