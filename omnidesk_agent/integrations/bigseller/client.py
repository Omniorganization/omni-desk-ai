from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

from omnidesk_agent.integrations.bigseller.auth import BigSellerTokenManager
from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import (
    BigSellerConfigurationError,
    BigSellerEndpointNotConfigured,
    BigSellerError,
    BigSellerRateLimitError,
    BigSellerUnauthorizedError,
    redact_secrets,
)
from omnidesk_agent.integrations.bigseller.rate_limit import BigSellerRateLimiter
from omnidesk_agent.integrations.bigseller.schemas import (
    BigSellerFulfillmentResult,
    BigSellerFulfillmentUpdate,
    BigSellerInventoryItem,
    BigSellerOrder,
    BigSellerProduct,
    BigSellerTokenState,
)


class BigSellerClient(ABC):
    @abstractmethod
    def exchange_auth_code(self) -> BigSellerTokenState:
        raise NotImplementedError

    @abstractmethod
    def refresh_access_token(self) -> BigSellerTokenState:
        raise NotImplementedError

    @abstractmethod
    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def list_orders(self, **filters: Any) -> list[BigSellerOrder]:
        raise NotImplementedError

    @abstractmethod
    def get_order(self, external_order_id: str, *, store_id: str) -> BigSellerOrder:
        raise NotImplementedError

    @abstractmethod
    def list_inventory(self, **filters: Any) -> list[BigSellerInventoryItem]:
        raise NotImplementedError

    @abstractmethod
    def update_inventory(self, item: BigSellerInventoryItem) -> BigSellerInventoryItem:
        raise NotImplementedError

    @abstractmethod
    def list_products(self, **filters: Any) -> list[BigSellerProduct]:
        raise NotImplementedError

    @abstractmethod
    def get_product(
        self, external_product_id: str, *, store_id: str
    ) -> BigSellerProduct:
        raise NotImplementedError

    @abstractmethod
    def sync_fulfillment_status(
        self, update: BigSellerFulfillmentUpdate
    ) -> BigSellerFulfillmentResult:
        raise NotImplementedError


class HttpBigSellerClient(BigSellerClient):
    """HTTP transport scaffold for the future private BigSeller adapter.

    This class can send a caller-supplied path, but business endpoint methods
    fail closed until the private BigSeller API docs provide endpoint paths,
    signing rules, and response field mappings.
    """

    def __init__(
        self,
        config: BigSellerConfig,
        *,
        token_manager: Optional[BigSellerTokenManager] = None,
        rate_limiter: Optional[BigSellerRateLimiter] = None,
        timeout_seconds: float = 10.0,
    ):
        self.config = config
        self.token_manager = token_manager or BigSellerTokenManager.from_config(config)
        self.rate_limiter = rate_limiter or BigSellerRateLimiter(
            per_minute=config.rate_limit_per_minute
        )
        self.timeout_seconds = timeout_seconds

    def exchange_auth_code(self) -> BigSellerTokenState:
        raise BigSellerEndpointNotConfigured(
            "BigSeller auth-code exchange endpoint is private and must be configured from official API docs"
        )

    def refresh_access_token(self) -> BigSellerTokenState:
        raise BigSellerEndpointNotConfigured(
            "BigSeller refresh-token endpoint is private and must be configured from official API docs"
        )

    def _require_real_ready(self) -> None:
        if not self.config.enabled:
            raise BigSellerConfigurationError("BigSeller connector is disabled")
        if self.config.use_mock:
            raise BigSellerConfigurationError(
                "HttpBigSellerClient cannot run while BIGSELLER_USE_MOCK=true"
            )
        issues = self.config.real_mode_issues()
        if issues:
            raise BigSellerConfigurationError("; ".join(issues))

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._require_real_ready()
        if not self.config.base_url:
            raise BigSellerConfigurationError(
                "BIGSELLER_BASE_URL is required for BigSeller real mode"
            )
        if not path.startswith("/"):
            raise BigSellerConfigurationError(
                "BigSeller request path must start with '/'"
            )

        def _send() -> Any:
            self.rate_limiter.wait()
            token = self.token_manager.ensure_access_token(self.refresh_access_token)
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Authorization", f"Bearer {token}")
            url = self.config.base_url + path
            try:
                response = httpx.request(
                    method.upper(),
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise BigSellerError(
                    f"BigSeller network request failed: {redact_secrets(str(exc))}"
                ) from exc
            if response.status_code in {401, 403}:
                raise BigSellerUnauthorizedError("BigSeller API returned unauthorized")
            if response.status_code == 429:
                raise BigSellerRateLimitError("BigSeller API rate limit exceeded")
            if response.status_code >= 400:
                raise BigSellerError(
                    f"BigSeller API request failed with status {response.status_code}"
                )
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"text": response.text}

        try:
            return _send()
        except BigSellerUnauthorizedError:
            self.token_manager.force_refresh(self.refresh_access_token)
            return _send()

    def list_orders(self, **filters: Any) -> list[BigSellerOrder]:
        raise BigSellerEndpointNotConfigured(
            "BigSeller order-list endpoint and field mapping require official private API docs"
        )

    def get_order(self, external_order_id: str, *, store_id: str) -> BigSellerOrder:
        raise BigSellerEndpointNotConfigured(
            "BigSeller order-detail endpoint and field mapping require official private API docs"
        )

    def list_inventory(self, **filters: Any) -> list[BigSellerInventoryItem]:
        raise BigSellerEndpointNotConfigured(
            "BigSeller inventory-list endpoint and field mapping require official private API docs"
        )

    def update_inventory(self, item: BigSellerInventoryItem) -> BigSellerInventoryItem:
        raise BigSellerEndpointNotConfigured(
            "BigSeller inventory-update endpoint and signature rules require official private API docs"
        )

    def list_products(self, **filters: Any) -> list[BigSellerProduct]:
        raise BigSellerEndpointNotConfigured(
            "BigSeller product-list endpoint and field mapping require official private API docs"
        )

    def get_product(
        self, external_product_id: str, *, store_id: str
    ) -> BigSellerProduct:
        raise BigSellerEndpointNotConfigured(
            "BigSeller product-detail endpoint and field mapping require official private API docs"
        )

    def sync_fulfillment_status(
        self, update: BigSellerFulfillmentUpdate
    ) -> BigSellerFulfillmentResult:
        raise BigSellerEndpointNotConfigured(
            "BigSeller fulfillment endpoint and status mapping require official private API docs"
        )
