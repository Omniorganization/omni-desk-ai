from __future__ import annotations

import hashlib
import hmac
import json
from typing import Mapping

from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import BigSellerConfigurationError
from omnidesk_agent.integrations.bigseller.schemas import BigSellerWebhookEvent


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def verify_webhook_signature(
    body: bytes, headers: Mapping[str, str], *, secret: str
) -> None:
    provided = _header(headers, "x-bigseller-signature-256") or _header(
        headers, "x-bigseller-signature"
    )
    if not provided:
        raise PermissionError("missing BigSeller webhook signature")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    normalized = provided.removeprefix("sha256=")
    if not hmac.compare_digest(normalized, expected):
        raise PermissionError("invalid BigSeller webhook signature")


def parse_bigseller_webhook(
    body: bytes, headers: Mapping[str, str], config: BigSellerConfig
) -> BigSellerWebhookEvent:
    if config.webhook_secret:
        verify_webhook_signature(body, headers, secret=config.webhook_secret)
    elif not config.use_mock:
        raise BigSellerConfigurationError(
            "BIGSELLER_WEBHOOK_SECRET is required for BigSeller real-mode webhooks"
        )
    payload = json.loads(body.decode("utf-8") or "{}")
    event_type = str(
        payload.get("event_type")
        or payload.get("type")
        or payload.get("event")
        or "unknown"
    )
    external_id = (
        payload.get("external_id")
        or payload.get("external_order_id")
        or payload.get("order_id")
        or payload.get("sku")
    )
    store_id = payload.get("store_id") or payload.get("shop_id") or payload.get("store")
    return BigSellerWebhookEvent(
        event_type=event_type,
        external_id=str(external_id) if external_id is not None else None,
        store_id=str(store_id) if store_id is not None else None,
        payload=payload,
    )
