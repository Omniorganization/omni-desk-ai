from __future__ import annotations

from datetime import datetime, timezone
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


def _parse_timestamp(raw: str) -> datetime:
    value = raw.strip()
    if not value:
        raise PermissionError("missing BigSeller webhook timestamp")
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except ValueError:
        pass
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def verify_webhook_timestamp(
    headers: Mapping[str, str], *, replay_window_seconds: int
) -> datetime:
    raw = (
        _header(headers, "x-bigseller-timestamp")
        or _header(headers, "x-bigseller-request-timestamp")
        or _header(headers, "x-request-timestamp")
    )
    timestamp = _parse_timestamp(raw)
    now = datetime.now(timezone.utc)
    drift_seconds = abs((now - timestamp).total_seconds())
    if drift_seconds > replay_window_seconds:
        raise PermissionError("stale BigSeller webhook timestamp")
    return timestamp


def verify_webhook_signature(
    body: bytes, headers: Mapping[str, str], *, secret: str
) -> str:
    provided = _header(headers, "x-bigseller-signature-256") or _header(
        headers, "x-bigseller-signature"
    )
    if not provided:
        raise PermissionError("missing BigSeller webhook signature")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    normalized = provided.removeprefix("sha256=")
    if not hmac.compare_digest(normalized, expected):
        raise PermissionError("invalid BigSeller webhook signature")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_bigseller_webhook(
    body: bytes, headers: Mapping[str, str], config: BigSellerConfig
) -> BigSellerWebhookEvent:
    signature_digest = None
    timestamp = None
    if config.webhook_secret:
        signature_digest = verify_webhook_signature(
            body, headers, secret=config.webhook_secret
        )
        if not config.use_mock:
            timestamp = verify_webhook_timestamp(
                headers,
                replay_window_seconds=config.webhook_replay_window_seconds,
            )
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
    event_id = (
        payload.get("event_id")
        or payload.get("webhook_id")
        or payload.get("id")
        or _header(headers, "x-bigseller-event-id")
        or _header(headers, "x-event-id")
    )
    if not config.use_mock and not event_id:
        raise PermissionError("missing BigSeller webhook event id")
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
        event_id=str(event_id) if event_id else None,
        timestamp=timestamp,
        signature_digest=signature_digest,
        payload=payload,
    )
