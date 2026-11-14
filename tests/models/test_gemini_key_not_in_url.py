"""M12: the Gemini API key must not travel in the URL or leak through error text."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnidesk_agent.config import ModelProfileConfig, ModelsConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models.base import ModelRequest
from omnidesk_agent.models.provider_errors import safe_error_text
from omnidesk_agent.models.router import ModelRouter
from omnidesk_agent.models.router_streaming import GovernedStreamingRouter
from omnidesk_agent.privacy.redaction import MemoryPrivacyFilter, strip_url_query

GEMINI_KEY = "AIza" + "S" * 35


def _install_transport(monkeypatch: pytest.MonkeyPatch, status: int) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    body = b'{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}'

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=body if status == 200 else b'{"error":{"status":"PERMISSION_DENIED"}}')

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    return seen


def _router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModelRouter:
    monkeypatch.setenv("TEST_GEMINI_KEY", GEMINI_KEY)
    cfg = ModelsConfig(
        default="gemini",
        profiles={"gemini": ModelProfileConfig(provider="gemini", model="gemini-test", api_key_env="TEST_GEMINI_KEY")},
        routing={"chat": {"primary": "gemini", "fallback": [], "max_retries": 0}},
    )
    return ModelRouter(cfg, TokenBudgetManager(tmp_path / "tokens.sqlite3"))


@pytest.mark.asyncio
async def test_gemini_complete_sends_key_as_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_transport(monkeypatch, 200)
    response = await _router(tmp_path, monkeypatch).complete(ModelRequest(system="s", user="u", task="chat"))
    assert response.text == "ok"
    assert seen[0].headers["x-goog-api-key"] == GEMINI_KEY
    assert GEMINI_KEY not in str(seen[0].url) and "key=" not in str(seen[0].url)


@pytest.mark.asyncio
async def test_gemini_http_error_does_not_leak_key_through_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_transport(monkeypatch, 403)
    with pytest.raises(RuntimeError) as exc:
        await _router(tmp_path, monkeypatch).complete(ModelRequest(system="s", user="u", task="chat"))
    # This text is what the worker stores in job_queue.last_error.
    assert "All model profiles failed" in str(exc.value)
    assert GEMINI_KEY not in str(exc.value)
    assert isinstance(exc.value.__cause__, httpx.HTTPStatusError)
    assert GEMINI_KEY not in str(exc.value.__cause__)
    assert all(GEMINI_KEY not in str(request.url) for request in seen)


@pytest.mark.asyncio
async def test_gemini_stream_error_does_not_leak_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_transport(monkeypatch, 403)
    stream = GovernedStreamingRouter(_router(tmp_path, monkeypatch)).stream(ModelRequest(system="s", user="u", task="chat"))
    with pytest.raises(RuntimeError) as exc:
        async for _ in stream:
            pass
    assert GEMINI_KEY not in str(exc.value)
    native = next(request for request in seen if "streamGenerateContent" in str(request.url))
    assert native.headers["x-goog-api-key"] == GEMINI_KEY
    assert dict(native.url.params) == {"alt": "sse"}


class LeakyProvider:
    """A provider whose transport error quotes a URL with a key in its query."""

    provider_name = "leaky"

    def __init__(self, settings):
        self.settings = settings
        self.model = settings.model
        self.profile_name = settings.profile_name

    async def complete(self, request):
        raise httpx.HTTPStatusError(
            f"Client error '403 Forbidden' for url 'https://provider.example/v1/generate?key={GEMINI_KEY}&alt=sse'",
            request=httpx.Request("POST", "https://provider.example/v1/generate"),
            response=httpx.Response(403),
        )


def _leaky_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModelRouter:
    import omnidesk_agent.models.router as router_mod

    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "leaky", LeakyProvider)
    cfg = ModelsConfig(
        default="leaky",
        profiles={"leaky": ModelProfileConfig(provider="leaky", model="m", api_key_env=None)},
        routing={"chat": {"primary": "leaky", "fallback": [], "max_retries": 0}},
    )
    return ModelRouter(cfg, TokenBudgetManager(tmp_path / "tokens.sqlite3"))


@pytest.mark.asyncio
async def test_router_error_text_strips_url_queries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError) as exc:
        await _leaky_router(tmp_path, monkeypatch).complete(ModelRequest(system="s", user="u", task="chat"))
    assert "last_error=Client error '403 Forbidden' for url 'https://provider.example/v1/generate'" in str(exc.value)
    assert GEMINI_KEY not in str(exc.value)


@pytest.mark.asyncio
async def test_streaming_router_error_text_strips_url_queries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = GovernedStreamingRouter(_leaky_router(tmp_path, monkeypatch)).stream(ModelRequest(system="s", user="u", task="chat"))
    with pytest.raises(RuntimeError) as exc:
        async for _ in stream:
            pass
    assert "native_error=Client error '403 Forbidden' for url 'https://provider.example/v1/generate'" in str(exc.value)
    assert GEMINI_KEY not in str(exc.value)


def test_safe_error_text_drops_url_query_and_userinfo() -> None:
    error = httpx.HTTPStatusError(
        "Client error '403 Forbidden' for url "
        f"'https://user:pw@generativelanguage.googleapis.com/v1beta/models/m:generateContent?key={GEMINI_KEY}&alt=sse#frag'",
        request=httpx.Request("POST", "https://example.invalid"),
        response=httpx.Response(403),
    )
    text = safe_error_text(error)
    assert text == "Client error '403 Forbidden' for url 'https://generativelanguage.googleapis.com/v1beta/models/m:generateContent'"
    assert safe_error_text(None) == "None"
    # A bare key outside any URL is still caught by the Google key pattern.
    assert GEMINI_KEY not in safe_error_text(RuntimeError(f"bad key {GEMINI_KEY}"))


def test_strip_url_query_keeps_host_and_path() -> None:
    assert strip_url_query("see https://api.example/v1/x?token=t&b=2 and wss://h/p#f") == "see https://api.example/v1/x and wss://h/p"
    assert strip_url_query("no urls here ?key=value") == "no urls here ?key=value"


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        (f"POST https://generativelanguage.googleapis.com/v1beta/models/m:generateContent?key={GEMINI_KEY}", GEMINI_KEY),
        ("GET https://host/cb?state=s&code=one-time-code", "one-time-code"),
        ("https://host/x?alt=sse&api-key=abc123def", "abc123def"),
        ("https://host/x?sig=s1gnature&x=1", "s1gnature"),
        ("x-goog-api-key: plainheadervalue", "plainheadervalue"),
        (f"leaked {GEMINI_KEY} in text", GEMINI_KEY),
        ("bad key AIza" + "A" * 34 + "- here", "AIza" + "A" * 34 + "-"),
        ("https://api.telegram.org/bot123456789:AAEabcdefghijklmnopqrstuvwxyz012345/sendMessage", "AAEabcdefghijklmnopqrstuvwxyz012345"),
    ],
)
def test_redaction_covers_query_string_and_provider_keys(raw: str, secret: str) -> None:
    redactor = MemoryPrivacyFilter()
    assert secret not in redactor.redact_text(raw)
    assert secret not in redactor.redact_secrets(raw)


def test_redact_secrets_leaves_non_secret_text_alone() -> None:
    text = "primary key = id; call +1 415 555 0100 at 1761100000.123 ?alt=sse&page=2"
    assert MemoryPrivacyFilter().redact_secrets(text) == text
