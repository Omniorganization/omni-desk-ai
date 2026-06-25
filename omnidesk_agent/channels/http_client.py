from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass, field
from typing import Any, Optional

from omnidesk_agent.channels.idempotency import idempotency_headers
from omnidesk_agent.privacy.redaction import MemoryPrivacyFilter

try:
    import httpx
except ModuleNotFoundError:  # allow channel parsing without outbound HTTP deps
    httpx = None  # type: ignore[assignment]


RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_REDACTOR = MemoryPrivacyFilter()
_OFFLINE_MODE = False


@dataclass
class ChannelHttpResult:
    status_code: int
    data: Any = None
    text: str = ""
    request_id: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)


class ChannelHttpError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, request_id: Optional[str] = None, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.response_text = response_text


def set_channel_offline_mode(enabled: bool) -> None:
    global _OFFLINE_MODE
    _OFFLINE_MODE = bool(enabled)


def _offline_mode_enabled() -> bool:
    value = os.getenv("OMNIDESK_OFFLINE_MODE", "").strip().lower()
    return _OFFLINE_MODE or value in {"1", "true", "yes", "on"}


def _require_httpx():
    if httpx is None:
        raise RuntimeError("httpx is required for outbound channel HTTP calls. Install with: python3 -m pip install httpx")
    return httpx


def _redact_error_text(value: Any) -> str:
    return _REDACTOR.redact_text(str(value))


class ChannelHttpClient:
    """Shared outbound HTTP client for channel adapters.

    Provides timeout, bounded retry, exponential backoff, 429 handling, request-id
    extraction and normalized provider errors. It deliberately does not hide
    permanent 4xx errors because those usually mean provider policy/config issues.
    """

    def __init__(self, *, timeout: float = 20.0, max_retries: int = 2, base_backoff: float = 0.25, max_backoff: float = 3.0):
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff

    async def post(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        json: Any = None,
        idempotency_key: Optional[str] = None,
        channel: str = "unknown",
    ) -> ChannelHttpResult:
        return await self.request("POST", url, headers=headers, params=params, json=json, idempotency_key=idempotency_key, channel=channel)

    async def get(self, url: str, *, headers: Optional[dict[str, str]] = None, params: Optional[dict[str, Any]] = None) -> ChannelHttpResult:
        return await self.request("GET", url, headers=headers, params=params)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        json: Any = None,
        idempotency_key: Optional[str] = None,
        channel: str = "unknown",
    ) -> ChannelHttpResult:
        if _offline_mode_enabled():
            raise ChannelHttpError("offline mode forbids outbound channel HTTP calls")
        lib = _require_httpx()
        last_error: Optional[BaseException] = None
        request_headers = dict(headers or {})
        request_headers.update(idempotency_headers(channel, idempotency_key))
        async with lib.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = await client.request(method, url, headers=request_headers, params=params, json=json)
                    request_id = self._request_id(response.headers)
                    if response.status_code < 400:
                        return ChannelHttpResult(
                            status_code=response.status_code,
                            data=self._json_or_none(response),
                            text=response.text,
                            request_id=request_id,
                            headers=dict(response.headers),
                        )
                    if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                        await asyncio.sleep(self._delay(attempt, response.headers))
                        continue
                    raise ChannelHttpError(
                        f"channel provider HTTP {response.status_code}",
                        status_code=response.status_code,
                        request_id=request_id,
                        response_text=_redact_error_text(response.text)[:2000],
                    )
                except ChannelHttpError:
                    raise
                except (lib.TimeoutException, lib.NetworkError, lib.TransportError) as exc:  # type: ignore[attr-defined]
                    last_error = exc
                    if attempt < self.max_retries:
                        await asyncio.sleep(self._delay(attempt, {}))
                        continue
                    raise ChannelHttpError(f"channel provider network error: {_redact_error_text(exc)}") from exc
        raise ChannelHttpError(f"channel provider request failed: {_redact_error_text(last_error)}")

    def _delay(self, attempt: int, headers: Any) -> float:
        retry_after = None
        try:
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
        except AttributeError:
            retry_after = None
        if retry_after:
            try:
                return min(float(retry_after), self.max_backoff)
            except ValueError:
                pass
        jitter = random.random() * 0.1
        return min(self.base_backoff * (2 ** attempt) + jitter, self.max_backoff)

    @staticmethod
    def _json_or_none(response: Any) -> Any:
        try:
            return response.json()
        except Exception:
            return None

    @staticmethod
    def _request_id(headers: Any) -> Optional[str]:
        for name in ("x-request-id", "x-fb-trace-id", "x-line-request-id", "x-tt-logid", "x-ms-request-id"):
            value = headers.get(name) if hasattr(headers, "get") else None
            if value:
                return str(value)
        return None
