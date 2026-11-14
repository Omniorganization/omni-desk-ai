from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from omnidesk_agent.privacy.redaction import MemoryPrivacyFilter, strip_url_query

_REDACTOR = MemoryPrivacyFilter()


@dataclass(frozen=True)
class ProviderErrorInfo:
    category: str
    retryable: bool
    fallback_recommended: bool
    status_code: Optional[int] = None


SUBSCRIPTION_PROVIDERS = frozenset({"chatgpt_plan", "codex_cli"})


class ChatGPTPlanError(RuntimeError):
    """Subscription failures must stop rather than retry or use a paid profile."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def classify_provider_error(exc: BaseException) -> ProviderErrorInfo:
    """Normalize provider failures for retry/fallback/cost attribution."""
    if isinstance(exc, ChatGPTPlanError):
        return ProviderErrorInfo(exc.category, False, False)
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


def safe_error_text(exc: Optional[BaseException]) -> str:
    """Provider error text for messages that end up in logs, job rows or APIs."""
    return _REDACTOR.redact_secrets(strip_url_query(str(exc)))
