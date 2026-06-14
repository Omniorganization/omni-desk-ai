from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProviderIdempotencyCapability:
    channel: str
    mode: str  # native | best_effort | none
    request_header: Optional[str] = None
    client_request_header: Optional[str] = "X-Omnidesk-Client-Request-Id"
    duplicate_risk: bool = True


# Most social/chat providers do not expose true exactly-once sends. We still
# propagate stable client request IDs for provider logs/support and mark the
# risk explicitly so SRE can decide whether reconciliation or manual inspection
# is required after ambiguous failures.
PROVIDER_IDEMPOTENCY: dict[str, ProviderIdempotencyCapability] = {
    "telegram": ProviderIdempotencyCapability("telegram", "best_effort", "X-Omnidesk-Idempotency-Key"),
    "whatsapp_cloud": ProviderIdempotencyCapability("whatsapp_cloud", "best_effort", "X-Omnidesk-Idempotency-Key"),
    "meta_graph": ProviderIdempotencyCapability("meta_graph", "best_effort", "X-Omnidesk-Idempotency-Key"),
    "line": ProviderIdempotencyCapability("line", "best_effort", "X-Omnidesk-Idempotency-Key"),
    "wechat_official": ProviderIdempotencyCapability("wechat_official", "best_effort", "X-Omnidesk-Idempotency-Key"),
    "dingtalk": ProviderIdempotencyCapability("dingtalk", "best_effort", "X-Omnidesk-Idempotency-Key"),
    "lark": ProviderIdempotencyCapability("lark", "best_effort", "X-Omnidesk-Idempotency-Key"),
    "feishu": ProviderIdempotencyCapability("feishu", "best_effort", "X-Omnidesk-Idempotency-Key"),
    "x": ProviderIdempotencyCapability("x", "best_effort", "X-Omnidesk-Idempotency-Key"),
}


def capability_for(channel: str) -> ProviderIdempotencyCapability:
    return PROVIDER_IDEMPOTENCY.get(channel, ProviderIdempotencyCapability(channel, "none", None, None, True))


def idempotency_headers(channel: str, idempotency_key: str | None) -> dict[str, str]:
    if not idempotency_key:
        return {}
    cap = capability_for(channel)
    headers: dict[str, str] = {}
    if cap.request_header:
        headers[cap.request_header] = str(idempotency_key)
    if cap.client_request_header:
        headers[cap.client_request_header] = str(idempotency_key)
    return headers
