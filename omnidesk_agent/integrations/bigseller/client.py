# pyright: reportGeneralTypeIssues=false, reportOptionalMemberAccess=false
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from typing import Any, Optional, cast
import urllib.parse

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
    BigSellerOrderItem,
    BigSellerProduct,
    BigSellerTokenState,
    utc_now,
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
    """Configurable HTTP transport for approved BigSeller API environments.

    Endpoint paths remain operator-supplied because BigSeller API access is
    private-approval based. Real mode fails closed unless all production sync
    endpoints, durable state, credentials, and webhook controls are configured.
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

    @staticmethod
    def _pick(payload: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key not in payload:
                continue
            value = payload[key]
            if value is not None and value != "":
                return value
        return None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str) and value.strip():
            normalized = value.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return utc_now()

    @staticmethod
    def _body_bytes(kwargs: dict[str, Any]) -> bytes:
        if "json" in kwargs:
            return json.dumps(
                kwargs["json"],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        content = kwargs.get("content") or kwargs.get("data")
        if content is None:
            return b""
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        )

    @staticmethod
    def _format_path(path: str, **values: Any) -> str:
        safe_values = {
            key: urllib.parse.quote(str(value), safe="")
            for key, value in values.items()
            if value is not None
        }
        try:
            return path.format(**safe_values)
        except KeyError as exc:
            raise BigSellerConfigurationError(
                f"BigSeller endpoint path missing placeholder value: {exc}"
            ) from exc

    def _endpoint(self, key: str, path: str | None) -> str:
        if not path:
            raise BigSellerEndpointNotConfigured(
                f"BigSeller endpoint path for {key} is not configured"
            )
        return path

    def _require_real_ready(self) -> None:
        if not self.config.enabled:
            raise BigSellerConfigurationError("BigSeller connector is disabled")
        if self.config.use_mock:
            raise BigSellerConfigurationError(
                "HttpBigSellerClient cannot run while BIGSELLER_USE_MOCK=true"
            )
        issues = self.config.real_mode_issues()
        if not issues:
            return
        endpoint_issues = [issue for issue in issues if "_PATH" in issue]
        non_endpoint_issues = [issue for issue in issues if "_PATH" not in issue]
        if non_endpoint_issues:
            raise BigSellerConfigurationError("; ".join(issues))
        raise BigSellerEndpointNotConfigured("; ".join(endpoint_issues))

    def _signed_headers(
        self, *, method: str, path: str, body: bytes, headers: dict[str, str]
    ) -> dict[str, str]:
        if not self.config.request_signing_enabled:
            return headers
        app_key = self.config.app_key or ""
        app_id = self.config.app_id or ""
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        canonical = "\n".join(
            [timestamp, method.upper(), path, body.decode("utf-8", errors="replace")]
        )
        signature = hmac.new(
            app_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        headers.setdefault(self.config.signature_timestamp_header, timestamp)
        headers.setdefault(self.config.signature_app_id_header, app_id)
        headers.setdefault(self.config.signature_header, signature)
        return headers

    def _send_request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool,
        allow_refresh: bool,
        **kwargs: Any,
    ) -> Any:
        self._require_real_ready()
        if not self.config.base_url:
            raise BigSellerConfigurationError(
                "BIGSELLER_BASE_URL is required for BigSeller real mode"
            )
        if not path.startswith("/"):
            raise BigSellerConfigurationError("BigSeller request path must start with '/'")

        def _send() -> Any:
            self.rate_limiter.wait()
            send_kwargs = dict(kwargs)
            headers = dict(send_kwargs.pop("headers", {}) or {})
            headers.setdefault("accept", "application/json")
            if authenticated:
                token = self.token_manager.ensure_access_token(self.refresh_access_token)
                headers.setdefault("Authorization", f"Bearer {token}")
            headers = self._signed_headers(
                method=method,
                path=path,
                body=self._body_bytes(send_kwargs),
                headers=headers,
            )
            url = self.config.base_url + path
            try:
                response = httpx.request(
                    method.upper(),
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    **send_kwargs,
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
            if not authenticated or not allow_refresh:
                raise
            self.token_manager.force_refresh(self.refresh_access_token)
            return _send()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._send_request(
            method, path, authenticated=True, allow_refresh=True, **kwargs
        )

    def _items(self, payload: Any, root_key: str | None = None) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        keys: list[str] = [root_key] if root_key else []
        keys.extend(["data", "items", "list", "rows", "records", "result"])
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = self._items(value)
                if nested:
                    return nested
        return [payload]

    def _first_dict(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                return cast(dict[str, Any], data)
            return cast(dict[str, Any], payload)
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return cast(dict[str, Any], payload[0])
        return {}

    def _parse_token_response(self, payload: Any) -> BigSellerTokenState:
        data = self._first_dict(payload)
        expires_at = None
        raw_expires_at = self._pick(
            data, "expires_at", "expire_at", "expiresAt", "expireTime"
        )
        raw_expires_in = self._pick(data, "expires_in", "expire_in", "expiresIn")
        if raw_expires_at is not None:
            expires_at = self._parse_datetime(raw_expires_at)
        elif raw_expires_in is not None:
            try:
                expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=int(raw_expires_in)
                )
            except (TypeError, ValueError):
                expires_at = None
        return BigSellerTokenState(
            access_token=self._pick(data, "access_token", "accessToken", "token"),
            refresh_token=self._pick(data, "refresh_token", "refreshToken")
            or self.token_manager.token_state.refresh_token,
            expires_at=expires_at,
        )

    def exchange_auth_code(self) -> BigSellerTokenState:
        path = self._endpoint("auth_code_exchange", self.config.auth_code_exchange_path)
        payload = self._send_request(
            "POST",
            path,
            authenticated=False,
            allow_refresh=False,
            json={
                "app_id": self.config.app_id,
                "app_key": self.config.app_key,
                "auth_code": self.config.auth_code,
            },
        )
        token_state = self._parse_token_response(payload)
        self.token_manager.replace(token_state)
        return token_state

    def refresh_access_token(self) -> BigSellerTokenState:
        if not self.token_manager.token_state.refresh_token and not self.config.refresh_token:
            raise BigSellerConfigurationError("BigSeller refresh token is not configured")
        path = self._endpoint("refresh_token", self.config.refresh_token_path)
        payload = self._send_request(
            "POST",
            path,
            authenticated=False,
            allow_refresh=False,
            json={
                "app_id": self.config.app_id,
                "app_key": self.config.app_key,
                "refresh_token": self.token_manager.token_state.refresh_token
                or self.config.refresh_token,
            },
        )
        token_state = self._parse_token_response(payload)
        self.token_manager.replace(token_state)
        return token_state

    def _map_order(
        self, row: dict[str, Any], *, fallback_store_id: str | None = None
    ) -> BigSellerOrder:
        items: list[BigSellerOrderItem] = []
        raw_items_value = row.get("items") or row.get("order_items") or row.get("sku_list") or []
        if isinstance(raw_items_value, dict):
            raw_items_list = self._items(raw_items_value)
        elif isinstance(raw_items_value, list):
            raw_items_list = raw_items_value
        else:
            raw_items_list = []
        for item in raw_items_list:
            if not isinstance(item, dict):
                continue
            unit_price_value = self._pick(item, "unit_price", "price", "sale_price")
            try:
                unit_price = float(unit_price_value) if unit_price_value is not None else None
            except (TypeError, ValueError):
                unit_price = None
            items.append(
                BigSellerOrderItem(
                    external_sku=str(
                        self._pick(item, "external_sku", "sku", "sku_id", "seller_sku")
                        or "unknown"
                    ),
                    quantity=max(
                        0, int(self._pick(item, "quantity", "qty", "count") or 0)
                    ),
                    title=self._pick(item, "title", "name", "product_name"),
                    unit_price=unit_price,
                )
            )
        return BigSellerOrder(
            external_order_id=str(
                self._pick(
                    row, "external_order_id", "order_id", "orderNo", "order_sn", "id"
                )
            ),
            store_id=str(
                self._pick(row, "store_id", "shop_id", "store", "storeId")
                or fallback_store_id
                or "__unknown_store__"
            ),
            status=str(
                self._pick(row, "status", "order_status", "orderStatus") or "unknown"
            ),
            created_at=self._parse_datetime(
                self._pick(row, "created_at", "create_time", "createdTime")
            ),
            updated_at=self._parse_datetime(
                self._pick(row, "updated_at", "update_time", "updatedTime")
            ),
            items=items,
            raw=row,
        )

    def _map_inventory(
        self, row: dict[str, Any], *, fallback_store_id: str | None = None
    ) -> BigSellerInventoryItem:
        return BigSellerInventoryItem(
            store_id=str(
                self._pick(row, "store_id", "shop_id", "store", "storeId")
                or fallback_store_id
                or "__unknown_store__"
            ),
            external_sku=str(
                self._pick(row, "external_sku", "sku", "sku_id", "seller_sku")
                or "unknown"
            ),
            available=max(
                0,
                int(self._pick(row, "available", "available_stock", "stock", "qty") or 0),
            ),
            reserved=max(
                0,
                int(self._pick(row, "reserved", "reserved_stock", "locked_stock") or 0),
            ),
            updated_at=self._parse_datetime(
                self._pick(row, "updated_at", "update_time", "updatedTime")
            ),
            raw=row,
        )

    def _map_product(
        self, row: dict[str, Any], *, fallback_store_id: str | None = None
    ) -> BigSellerProduct:
        raw_skus = row.get("skus") or row.get("sku_list") or row.get("items") or []
        skus: list[str] = []
        if isinstance(raw_skus, list):
            for item in raw_skus:
                if isinstance(item, dict):
                    sku = self._pick(item, "external_sku", "sku", "sku_id", "seller_sku")
                else:
                    sku = item
                if sku is not None:
                    skus.append(str(sku))
        return BigSellerProduct(
            external_product_id=str(
                self._pick(row, "external_product_id", "product_id", "productId", "id")
            ),
            store_id=str(
                self._pick(row, "store_id", "shop_id", "store", "storeId")
                or fallback_store_id
                or "__unknown_store__"
            ),
            title=str(self._pick(row, "title", "name", "product_name") or "untitled"),
            skus=skus,
            raw=row,
        )

    def list_orders(self, **filters: Any) -> list[BigSellerOrder]:
        path = self._endpoint("orders_list", self.config.orders_list_path)
        payload = self.request(
            "GET", path, params={k: v for k, v in filters.items() if v is not None}
        )
        root_key = self.config.response_root_keys.get("orders")
        return [
            self._map_order(row, fallback_store_id=filters.get("store_id"))
            for row in self._items(payload, root_key)
            if isinstance(row, dict)
        ]

    def get_order(self, external_order_id: str, *, store_id: str) -> BigSellerOrder:
        path = self._format_path(
            self._endpoint("order_detail", self.config.order_detail_path),
            external_order_id=external_order_id,
            order_id=external_order_id,
            store_id=store_id,
        )
        payload = self.request("GET", path, params={"store_id": store_id})
        return self._map_order(self._first_dict(payload), fallback_store_id=store_id)

    def list_inventory(self, **filters: Any) -> list[BigSellerInventoryItem]:
        path = self._endpoint("inventory_list", self.config.inventory_list_path)
        payload = self.request(
            "GET", path, params={k: v for k, v in filters.items() if v is not None}
        )
        root_key = self.config.response_root_keys.get("inventory")
        return [
            self._map_inventory(row, fallback_store_id=filters.get("store_id"))
            for row in self._items(payload, root_key)
            if isinstance(row, dict)
        ]

    def update_inventory(self, item: BigSellerInventoryItem) -> BigSellerInventoryItem:
        path = self._format_path(
            self._endpoint("inventory_update", self.config.inventory_update_path),
            external_sku=item.external_sku,
            sku=item.external_sku,
            store_id=item.store_id,
        )
        payload = self.request("POST", path, json=item.model_dump(mode="json"))
        return self._map_inventory(
            self._first_dict(payload) or item.model_dump(mode="json"),
            fallback_store_id=item.store_id,
        )

    def list_products(self, **filters: Any) -> list[BigSellerProduct]:
        path = self._endpoint("products_list", self.config.products_list_path)
        payload = self.request(
            "GET", path, params={k: v for k, v in filters.items() if v is not None}
        )
        root_key = self.config.response_root_keys.get("products")
        return [
            self._map_product(row, fallback_store_id=filters.get("store_id"))
            for row in self._items(payload, root_key)
            if isinstance(row, dict)
        ]

    def get_product(
        self, external_product_id: str, *, store_id: str
    ) -> BigSellerProduct:
        path = self._format_path(
            self._endpoint("product_detail", self.config.product_detail_path),
            external_product_id=external_product_id,
            product_id=external_product_id,
            store_id=store_id,
        )
        payload = self.request("GET", path, params={"store_id": store_id})
        return self._map_product(self._first_dict(payload), fallback_store_id=store_id)

    def sync_fulfillment_status(
        self, update: BigSellerFulfillmentUpdate
    ) -> BigSellerFulfillmentResult:
        path = self._format_path(
            self._endpoint("fulfillment_sync", self.config.fulfillment_sync_path),
            external_order_id=update.external_order_id,
            order_id=update.external_order_id,
            store_id=update.store_id,
        )
        payload = self.request("POST", path, json=update.model_dump(mode="json"))
        data = self._first_dict(payload)
        return BigSellerFulfillmentResult(
            external_order_id=str(data.get("external_order_id") or update.external_order_id),
            store_id=str(data.get("store_id") or update.store_id),
            status=str(data.get("status") or update.status),
            accepted=bool(data.get("accepted", True)),
            message=data.get("message"),
            raw=data or update.model_dump(mode="json"),
        )
