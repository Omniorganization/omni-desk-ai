from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ProviderErrorInfo:
    category: str
    retryable: bool
    fallback_recommended: bool
    status_code: Optional[int] = None


def classify_provider_error(exc: BaseException) -> ProviderErrorInfo:
    """Normalize provider failures for retry/fallback/cost attribution."""
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    text = str(exc).lower()
    if status_code == 429 or "rate limit" in text or "quota" in text:
        return ProviderErrorInfo("rate_limited", True, True, status_code)
    if status_code in {408, 409, 425, 500, 502, 503, 504} or "timeout" in text:
        return ProviderErrorInfo("transient", True, True, status_code)
    if status_code in {401, 403} or "missing" in text and "api" in text:
        return ProviderErrorInfo("auth", False, True, status_code)
    if status_code and 400 <= int(status_code) < 500:
        return ProviderErrorInfo("bad_request", False, True, status_code)
    return ProviderErrorInfo("unknown", True, True, status_code)
