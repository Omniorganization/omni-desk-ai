from __future__ import annotations

import asyncio
import json as _json
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


# Statuses worth repeating for a request that is safe to repeat: an idempotent
# method, or a send that carries a dedupe key the provider itself honours.
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
# Statuses where the provider says it did not act on the request, so even a
# send without provider-side dedupe can be repeated.
NOT_PROCESSED_STATUS_CODES = {408, 425, 429}
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
# httpx errors raised before any byte of the request reached the provider.
_NOT_SENT_ERRORS = ("ConnectError", "ConnectTimeout", "PoolTimeout", "ProxyError", "UnsupportedProtocol", "LocalProtocolError")
_TRANSPORT_ERRORS = ("TimeoutException", "NetworkError", "TransportError")
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
    """A provider call that did not succeed.

    ``request_sent`` is False only when the request provably never reached the
    provider (connection failures, offline mode), so repeating it cannot
    duplicate a message. ``category``, when set, is the classification the
    transport or adapter already knows; ``classify_channel_error`` uses it
    instead of guessing from the message.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        response_text: str = "",
        channel: str = "unknown",
        request_sent: Optional[bool] = None,
        category: Optional[str] = None,
        retry_after_seconds: Optional[float] = None,
        response_json: Any = None,
        provider_code: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.response_text = response_text
        self.channel = channel
        self.request_sent = request_sent
        self.category = category
        self.retry_after_seconds = retry_after_seconds
        self.response_json = response_json
        self.provider_code = provider_code
        self.headers = dict(headers or {})


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


def _error_types(lib: Any, names: tuple[str, ...]) -> tuple[type[BaseException], ...]:
    found = []
    for name in names:
        value = getattr(lib, name, None)
        if isinstance(value, type) and issubclass(value, BaseException):
            found.append(value)
    return tuple(found)


def _parse_json(text: str) -> Any:
    try:
        return _json.loads(text)
    except (TypeError, ValueError):
        return None


def _redacted_json(value: Any, depth: int = 0) -> Any:
    """The parsed error body with every string redacted; numbers keep their value.

    Redacting the raw text first would break the JSON (long ids look like
    phone numbers), and the error codes the classifier needs are numbers.
    """
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {str(key): _redacted_json(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted_json(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return _redact_error_text(value)[:2000]
    return value


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def retry_after_seconds(headers: Any, data: Any) -> Optional[float]:
    """The provider's own back-off hint: Retry-After, or the JSON body field.

    Telegram sends ``parameters.retry_after``, Discord ``retry_after`` (seconds)
    and Matrix ``retry_after_ms``.
    """
    value = None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        value = None
    parsed = _number(value)
    if parsed is not None:
        return parsed
    if isinstance(data, dict):
        params = data.get("parameters")
        if isinstance(params, dict) and _number(params.get("retry_after")) is not None:
            return _number(params.get("retry_after"))
        if _number(data.get("retry_after")) is not None:
            return _number(data.get("retry_after"))
        millis = _number(data.get("retry_after_ms"))
        if millis is not None:
            return millis / 1000.0
    return None


class ChannelHttpClient:
    """Shared outbound HTTP client for channel adapters.

    Provides timeouts, bounded retry with backoff, provider back-off hints,
    request-id extraction and normalized provider errors.

    A request is repeated only when repeating it cannot duplicate an external
    effect: when the request never reached the provider, when the provider
    answered 408/425/429 (it did not act), or when the request is idempotent
    (GET/PUT/..., or ``idempotent=True`` for a send carrying a dedupe key the
    provider honours). After a read timeout or a 5xx a plain send may already
    have been delivered, so the error goes straight to the caller, and the
    outbound dispatcher records it as an ambiguous send for reconciliation
    instead of sending the message again.
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
        data: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        channel: str = "unknown",
        idempotent: Optional[bool] = None,
    ) -> ChannelHttpResult:
        return await self.request(
            "POST", url, headers=headers, params=params, json=json, data=data,
            idempotency_key=idempotency_key, channel=channel, idempotent=idempotent,
        )

    async def get(self, url: str, *, headers: Optional[dict[str, str]] = None, params: Optional[dict[str, Any]] = None, channel: str = "unknown") -> ChannelHttpResult:
        return await self.request("GET", url, headers=headers, params=params, channel=channel)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        json: Any = None,
        data: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        channel: str = "unknown",
        idempotent: Optional[bool] = None,
    ) -> ChannelHttpResult:
        if _offline_mode_enabled():
            raise ChannelHttpError("offline mode forbids outbound channel HTTP calls", channel=channel, request_sent=False, category="not_sent")
        lib = _require_httpx()
        method = method.upper()
        repeatable = method in IDEMPOTENT_METHODS if idempotent is None else bool(idempotent)
        request_headers = dict(headers or {})
        request_headers.update(idempotency_headers(channel, idempotency_key))
        kwargs: dict[str, Any] = {"headers": request_headers, "params": params}
        if json is not None:
            kwargs["json"] = json
        if data is not None:
            kwargs["data"] = data
        not_sent = _error_types(lib, _NOT_SENT_ERRORS)
        transport = _error_types(lib, _TRANSPORT_ERRORS)
        timeouts = _error_types(lib, ("TimeoutException",))
        async with lib.AsyncClient(timeout=self.timeout) as client:
            attempt = 0
            while True:
                try:
                    response = await client.request(method, url, **kwargs)
                except not_sent as exc:
                    if attempt < self.max_retries:
                        await asyncio.sleep(self._delay(attempt, None))
                        attempt += 1
                        continue
                    raise ChannelHttpError(
                        f"channel provider unreachable; request was not sent: {_redact_error_text(exc)}",
                        channel=channel, request_sent=False, category="not_sent",
                    ) from exc
                except transport as exc:
                    if repeatable and attempt < self.max_retries:
                        await asyncio.sleep(self._delay(attempt, None))
                        attempt += 1
                        continue
                    timed_out = bool(timeouts) and isinstance(exc, timeouts)
                    raise ChannelHttpError(
                        f"channel provider network error after the request was sent: {_redact_error_text(exc) or type(exc).__name__}",
                        channel=channel, request_sent=True, category="timeout" if timed_out else "network_error",
                    ) from exc
                request_id = self._request_id(response.headers)
                if response.status_code < 400:
                    return ChannelHttpResult(
                        status_code=response.status_code,
                        data=self._json_or_none(response),
                        text=response.text,
                        request_id=request_id,
                        headers=dict(response.headers),
                    )
                response_text = _redact_error_text(response.text)[:2000]
                response_json = _redacted_json(_parse_json(response.text[:65536]))
                hint = retry_after_seconds(response.headers, response_json)
                may_repeat = response.status_code in NOT_PROCESSED_STATUS_CODES or (repeatable and response.status_code in RETRYABLE_STATUS_CODES)
                if may_repeat and attempt < self.max_retries and (hint is None or hint <= self.max_backoff):
                    await asyncio.sleep(self._delay(attempt, hint))
                    attempt += 1
                    continue
                raise ChannelHttpError(
                    f"channel provider HTTP {response.status_code}",
                    status_code=response.status_code,
                    request_id=request_id,
                    response_text=response_text,
                    channel=channel,
                    request_sent=True,
                    retry_after_seconds=hint,
                    response_json=response_json,
                    headers=dict(response.headers),
                )

    def _delay(self, attempt: int, hint: Optional[float]) -> float:
        if hint is not None:
            return min(hint, self.max_backoff)
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
        for name in ("x-request-id", "x-fb-trace-id", "x-line-request-id", "x-tt-logid", "x-ms-request-id", "x-acs-request-id"):
            value = headers.get(name) if hasattr(headers, "get") else None
            if value:
                return str(value)
        return None
