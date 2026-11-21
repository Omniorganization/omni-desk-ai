from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProviderIdempotencyCapability:
    channel: str
    mode: str  # native | none
    # Header the provider itself uses to deduplicate a repeated send.
    request_header: Optional[str] = None
    # Correlation header for OmniDesk's own bridges/relays; providers ignore it.
    client_request_header: Optional[str] = None
    duplicate_risk: bool = True
    mechanism: str = ""


# ``native`` means the provider deduplicates a repeated send that carries the
# same key, so the outbox may retry after a timeout or 5xx. Everything else has
# no exactly-once send: an ambiguous failure must be reconciled, not resent.
PROVIDER_IDEMPOTENCY: dict[str, ProviderIdempotencyCapability] = {
    "line": ProviderIdempotencyCapability("line", "native", "X-Line-Retry-Key", duplicate_risk=False, mechanism="X-Line-Retry-Key (UUID, 24h)"),
    "matrix": ProviderIdempotencyCapability("matrix", "native", duplicate_risk=False, mechanism="PUT /send/{eventType}/{txnId}"),
    "lark": ProviderIdempotencyCapability("lark", "native", duplicate_risk=False, mechanism="im/v1/messages body uuid (1h)"),
    "feishu": ProviderIdempotencyCapability("feishu", "native", duplicate_risk=False, mechanism="im/v1/messages body uuid (1h)"),
    # Discord's nonce + enforce_nonce only deduplicates within a few minutes,
    # which covers transport retries but not the outbox's retry schedule.
    "discord": ProviderIdempotencyCapability("discord", "none", mechanism="nonce + enforce_nonce (minutes; transport retries only)"),
    "signal": ProviderIdempotencyCapability("signal", "none", client_request_header="X-Omnidesk-Client-Request-Id"),
    "imessage": ProviderIdempotencyCapability("imessage", "none", client_request_header="X-Omnidesk-Client-Request-Id"),
}
for _name in (
    "telegram", "whatsapp_cloud", "meta_graph", "wechat_official", "dingtalk", "x", "slack",
    "google_chat", "microsoft_teams", "teams", "qq",
):
    PROVIDER_IDEMPOTENCY[_name] = ProviderIdempotencyCapability(_name, "none")

_RETRY_KEY_NAMESPACE = uuid.UUID("5f0f6c3e-6f0e-4f4e-9a57-6d6e6964657a")


def capability_for(channel: str) -> ProviderIdempotencyCapability:
    return PROVIDER_IDEMPOTENCY.get(channel, ProviderIdempotencyCapability(channel, "none", None, None, True))


def provider_retry_key(idempotency_key: str) -> str:
    """A stable UUID for providers that require one (LINE's X-Line-Retry-Key)."""
    try:
        return str(uuid.UUID(str(idempotency_key)))
    except ValueError:
        return str(uuid.uuid5(_RETRY_KEY_NAMESPACE, str(idempotency_key)))


def idempotency_headers(channel: str, idempotency_key: str | None) -> dict[str, str]:
    if not idempotency_key:
        return {}
    cap = capability_for(channel)
    headers: dict[str, str] = {}
    if cap.request_header:
        headers[cap.request_header] = provider_retry_key(idempotency_key) if cap.request_header == "X-Line-Retry-Key" else str(idempotency_key)
    if cap.client_request_header:
        headers[cap.client_request_header] = str(idempotency_key)
    return headers
