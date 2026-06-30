from __future__ import annotations

import pytest

from omnidesk_agent.models import providers
from omnidesk_agent.models.base import ModelRequest
from omnidesk_agent.models.providers import (
    AnthropicProvider,
    AzureOpenAIProvider,
    CohereProvider,
    GeminiProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderSettings,
)


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeAsyncClient:
    requests = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, **kwargs):
        self.__class__.requests.append((url, kwargs))
        if url.endswith("/responses"):
            return _FakeResponse({"output_text": "responses-ok", "usage": {"total_tokens": 3}})
        if "/chat/completions" in url:
            return _FakeResponse({"choices": [{"message": {"content": "chat-ok"}}], "usage": {"total_tokens": 4}})
        if url.endswith("/v1/messages"):
            return _FakeResponse({"content": [{"type": "text", "text": "anthropic-ok"}], "usage": {"input_tokens": 1}})
        if "generateContent" in url:
            return _FakeResponse({"candidates": [{"content": {"parts": [{"text": "gemini-ok"}]}}], "usageMetadata": {"totalTokenCount": 5}})
        if url.endswith("/api/chat"):
            return _FakeResponse({"message": {"content": "ollama-ok"}})
        if url.endswith("/v2/chat"):
            return _FakeResponse({"message": {"content": [{"text": "cohere-ok"}]}, "usage": {"tokens": 2}})
        raise AssertionError(f"unexpected URL: {url}")


def _request() -> ModelRequest:
    return ModelRequest(system="system", user="user", task="chat", task_id="task-1")


def _settings(provider: str, *, model: str = "model", base_url: str | None = None) -> ProviderSettings:
    return ProviderSettings(
        profile_name=f"{provider}-profile",
        provider=provider,
        model=model,
        api_key_env="TEST_MODEL_API_KEY",
        base_url=base_url,
        max_output_tokens=32,
        temperature=0.1,
    )


@pytest.mark.asyncio
async def test_openai_responses_provider_success(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_API_KEY", "key")
    monkeypatch.setattr(providers.httpx, "AsyncClient", _FakeAsyncClient)

    response = await OpenAIResponsesProvider(_settings("openai")).complete(_request())

    assert response.text == "responses-ok"
    assert response.provider == "openai"


@pytest.mark.asyncio
async def test_openai_compatible_provider_success(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_API_KEY", "key")
    monkeypatch.setattr(providers.httpx, "AsyncClient", _FakeAsyncClient)

    response = await OpenAICompatibleProvider(_settings("openai_compatible")).complete(_request())

    assert response.text == "chat-ok"
    assert response.provider == "openai_compatible"


@pytest.mark.asyncio
async def test_azure_openai_provider_success(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_API_KEY", "key")
    monkeypatch.setattr(providers.httpx, "AsyncClient", _FakeAsyncClient)

    response = await AzureOpenAIProvider(
        _settings("azure_openai", model="deployment", base_url="https://azure.example")
    ).complete(_request())

    assert response.text == "chat-ok"
    assert response.provider == "azure_openai"


@pytest.mark.asyncio
async def test_anthropic_provider_success(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_API_KEY", "key")
    monkeypatch.setattr(providers.httpx, "AsyncClient", _FakeAsyncClient)

    response = await AnthropicProvider(_settings("anthropic")).complete(_request())

    assert response.text == "anthropic-ok"
    assert response.provider == "anthropic"


@pytest.mark.asyncio
async def test_gemini_provider_success_without_images(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_API_KEY", "key")
    monkeypatch.setattr(providers.httpx, "AsyncClient", _FakeAsyncClient)

    response = await GeminiProvider(_settings("gemini")).complete(_request())

    assert response.text == "gemini-ok"
    assert response.provider == "gemini"


@pytest.mark.asyncio
async def test_ollama_provider_success(monkeypatch):
    monkeypatch.setattr(providers.httpx, "AsyncClient", _FakeAsyncClient)

    response = await OllamaProvider(_settings("ollama", base_url="http://127.0.0.1:11434")).complete(_request())

    assert response.text == "ollama-ok"
    assert response.provider == "ollama"


@pytest.mark.asyncio
async def test_cohere_provider_success(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_API_KEY", "key")
    monkeypatch.setattr(providers.httpx, "AsyncClient", _FakeAsyncClient)

    response = await CohereProvider(_settings("cohere")).complete(_request())

    assert response.text == "cohere-ok"
    assert response.provider == "cohere"


@pytest.mark.asyncio
async def test_missing_api_key_rejects_external_provider(monkeypatch):
    monkeypatch.delenv("TEST_MODEL_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="Missing TEST_MODEL_API_KEY"):
        await OpenAIResponsesProvider(_settings("openai")).complete(_request())


@pytest.mark.asyncio
async def test_azure_requires_endpoint(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_API_KEY", "key")

    with pytest.raises(RuntimeError, match="base_url is required"):
        await AzureOpenAIProvider(_settings("azure_openai", base_url=None)).complete(_request())
