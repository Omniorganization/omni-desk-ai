from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BigSellerTokenState(BaseModel):
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None


class BigSellerOrderItem(BaseModel):
    external_sku: str
    quantity: int = Field(ge=0)
    title: Optional[str] = None
    unit_price: Optional[float] = None


class BigSellerOrder(BaseModel):
    external_order_id: str
    store_id: str
    status: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    items: list[BigSellerOrderItem] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class BigSellerInventoryItem(BaseModel):
    store_id: str
    external_sku: str
    available: int = Field(ge=0)
    reserved: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class BigSellerProduct(BaseModel):
    external_product_id: str
    store_id: str
    title: str
    skus: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class BigSellerFulfillmentUpdate(BaseModel):
    external_order_id: str
    store_id: str
    status: str
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None


class BigSellerFulfillmentResult(BaseModel):
    external_order_id: str
    store_id: str
    status: str
    accepted: bool
    raw: dict[str, Any] = Field(default_factory=dict)


class BigSellerMappedEntity(BaseModel):
    entity_type: str
    external_id: str
    store_id: str
    internal_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BigSellerWebhookEvent(BaseModel):
    event_type: str
    external_id: Optional[str] = None
    store_id: Optional[str] = None
    event_id: Optional[str] = None
    timestamp: Optional[datetime] = None
    signature_digest: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class BigSellerAuditEvent(BaseModel):
    event: str
    entity_type: str
    action: str
    status: str
    external_id: Optional[str] = None
    store_id: Optional[str] = None
    internal_id: Optional[str] = None
    error_code: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: datetime = Field(default_factory=utc_now)


class BigSellerQueuedError(BaseModel):
    id: str
    entity_type: str
    external_id: str
    store_id: str
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["retryable", "dead_letter", "resolved"] = "retryable"
    retry_count: int = 0
    max_retries: int = 3
    error_code: str
    error_message: str
    next_retry_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BigSellerSyncResult(BaseModel):
    entity_type: str
    started_at: datetime
    ended_at: datetime
    total: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    queued_errors: int = 0
    duration_ms: int = 0
    details: list[dict[str, Any]] = Field(default_factory=list)
