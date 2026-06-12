from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ChannelErrorInfo:
    category: str
    retryable: bool
    dead_letter_now: bool
    alert: bool
    status_code: Optional[int] = None


def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return int(value) if isinstance(value, int) else None


def classify_channel_error(exc: BaseException) -> ChannelErrorInfo:
    status = _status_code(exc)
    text = str(exc).lower()
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return ChannelErrorInfo("rate_limit", True, False, False, status)
    if status is not None and 500 <= status <= 599:
        return ChannelErrorInfo("provider_5xx", True, False, False, status)
    if "timeout" in text or "timed out" in text:
        return ChannelErrorInfo("timeout", True, False, False, status)
    if status in {401, 403} or "unauthorized" in text or "forbidden" in text:
        return ChannelErrorInfo("auth_error", False, True, True, status)
    if status == 400 or "invalid recipient" in text or "recipient invalid" in text:
        return ChannelErrorInfo("bad_request", False, True, False, status)
    if "unsupported" in text:
        return ChannelErrorInfo("unsupported_payload", False, True, False, status)
    if "unknown channel" in text or "adapter" in text:
        return ChannelErrorInfo("config_error", False, True, True, status)
    if "template" in text or "session window" in text or "policy" in text:
        return ChannelErrorInfo("policy_error", False, True, True, status)
    return ChannelErrorInfo("unknown", True, False, False, status)
