"""Screenshots must reach multimodal models, and a new screenshot must get a new answer.

Before this change the OpenAI, Anthropic, Azure, Ollama, Cohere and Bedrock
request bodies carried text only, Gemini refused every vision.ground call
because no upload root was bound, and the response cache keyed on text only,
so a second screenshot with the same instruction got the first one's answer.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from omnidesk_agent.config import ModelProfileConfig, ModelsConfig, PermissionConfig
from omnidesk_agent.core.token_budget import TokenBudgetConfig, TokenBudgetManager
from omnidesk_agent.models import provider_streaming, providers
from omnidesk_agent.models.base import ModelRequest
from omnidesk_agent.models.provider_streaming import stream_provider
from omnidesk_agent.models.providers import (
    AnthropicProvider,
    AzureOpenAIProvider,
    BedrockProvider,
    CohereProvider,
    GeminiProvider,
    ModelInputRejected,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderSettings,
)
from omnidesk_agent.models.router import ModelRouter
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.tools.vision import VisionGroundingTool

PNG_A = b"\x89PNG\r\n\x1a\n" + b"screen-a" * 8
PNG_B = b"\x89PNG\r\n\x1a\n" + b"screen-b" * 8
GROUNDING = json.dumps({"summary": "login form", "elements": [], "target": None, "confidence": 0.9})


class _Response:
    def __init__(self, data: dict[str, Any]):
        self._data = data
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._data


class _Client:
    posts: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url: str, **kwargs):
        type(self).posts.append((url, kwargs.get("json") or {}))
        if url.endswith("/responses"):
            return _Response({"output_text": GROUNDING})
        if "/chat/completions" in url:
            return _Response({"choices": [{"message": {"content": GROUNDING}}]})
        if url.endswith("/v1/messages"):
            return _Response({"content": [{"type": "text", "text": GROUNDING}]})
        if "generateContent" in url:
            return _Response({"candidates": [{"content": {"parts": [{"text": GROUNDING}]}}]})
        if url.endswith("/api/chat"):
            return _Response({"message": {"content": GROUNDING}})
        if url.endswith("/v2/chat"):
            return _Response({"message": {"content": [{"text": GROUNDING}]}})
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_API_KEY", "key")
    _Client.posts = []
    monkeypatch.setattr(providers.httpx, "AsyncClient", _Client)
    return _Client


@pytest.fixture
def screenshot(tmp_path) -> Path:
    path = tmp_path / "workspace" / "screenshots" / "a.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(PNG_A)
    return path


def _settings(provider: str, **kwargs) -> ProviderSettings:
    return ProviderSettings(profile_name="vision", provider=provider, model="m", api_key_env="TEST_MODEL_API_KEY", max_output_tokens=64, **kwargs)


def _request(image: Path, *, digest: str | None = None, roots: list[str] | None = None) -> ModelRequest:
    metadata: dict[str, Any] = {"allowed_image_roots": roots if roots is not None else [str(image.parent.parent)]}
    if digest is not None:
        metadata["image_sha256"] = {str(image): digest}
    return ModelRequest(system="ground", user="find the login button", task="vision", images=[str(image)], metadata=metadata)


B64_A = base64.b64encode(PNG_A).decode("ascii")


@pytest.mark.parametrize(
    ("provider_cls", "name", "extra", "find_image"),
    [
        (OpenAIResponsesProvider, "openai", {}, lambda body: body["input"][1]["content"][1]["image_url"]),
        (OpenAICompatibleProvider, "qwen", {}, lambda body: body["messages"][1]["content"][1]["image_url"]["url"]),
        (AzureOpenAIProvider, "azure_openai", {"base_url": "https://example.openai.azure.com"}, lambda body: body["messages"][1]["content"][1]["image_url"]["url"]),
        (CohereProvider, "cohere", {}, lambda body: body["messages"][1]["content"][1]["image_url"]["url"]),
        (AnthropicProvider, "anthropic", {}, lambda body: body["messages"][0]["content"][0]["source"]["data"]),
        (GeminiProvider, "gemini", {}, lambda body: body["contents"][0]["parts"][1]["inline_data"]["data"]),
        (OllamaProvider, "ollama", {}, lambda body: body["messages"][-1]["images"][0]),
    ],
)
def test_each_multimodal_provider_sends_the_image_in_its_own_format(client, screenshot, provider_cls, name, extra, find_image):
    provider = provider_cls(_settings(name, **extra))
    response = asyncio.run(provider.complete(_request(screenshot, digest=hashlib.sha256(PNG_A).hexdigest())))

    assert response.text == GROUNDING
    (_url, body), = client.posts
    sent = find_image(body)
    assert sent.endswith(B64_A)
    if sent.startswith("data:"):
        assert sent.startswith("data:image/png;base64,")


def test_text_only_requests_keep_their_plain_bodies(client):
    asyncio.run(OpenAIResponsesProvider(_settings("openai")).complete(ModelRequest(system="s", user="u")))
    asyncio.run(OpenAICompatibleProvider(_settings("qwen")).complete(ModelRequest(system="s", user="u")))
    responses_body, chat_body = (body for _, body in client.posts)
    assert responses_body["input"] == [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert chat_body["messages"] == [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_bedrock_sends_the_image_as_an_anthropic_content_block(monkeypatch, screenshot):
    sent: dict[str, Any] = {}

    class _Body:
        def read(self):
            return json.dumps({"content": [{"type": "text", "text": GROUNDING}]}).encode()

    class _Bedrock:
        def invoke_model(self, **kwargs):
            sent.update(json.loads(kwargs["body"]))
            return {"body": _Body()}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: _Bedrock()))
    asyncio.run(BedrockProvider(_settings("bedrock", region="us-east-1")).complete(_request(screenshot)))
    assert sent["messages"][0]["content"][0]["source"]["data"] == B64_A


@pytest.mark.parametrize("bound_root", [None, "elsewhere"])
def test_images_outside_a_bound_root_are_refused_before_any_request(client, screenshot, bound_root, tmp_path):
    roots: list[str] = []
    if bound_root:
        (tmp_path / bound_root).mkdir()
        roots = [str(tmp_path / bound_root)]
    with pytest.raises(ModelInputRejected):
        asyncio.run(OpenAIResponsesProvider(_settings("openai")).complete(_request(screenshot, roots=roots)))
    assert client.posts == []


def test_an_image_that_changed_after_approval_is_never_sent(client, screenshot):
    approved = hashlib.sha256(PNG_A).hexdigest()
    screenshot.write_bytes(PNG_B)
    with pytest.raises(ModelInputRejected, match="changed after it was approved"):
        asyncio.run(AnthropicProvider(_settings("anthropic")).complete(_request(screenshot, digest=approved)))
    assert client.posts == []


def test_streaming_bodies_carry_images_too(monkeypatch, screenshot):
    captured: list[dict[str, Any]] = []

    class _Stream:
        headers: dict[str, str] = {}

        def __init__(self, lines):
            self.lines = lines

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in self.lines:
                yield line

    class _StreamClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def stream(self, _method, url, **kwargs):
            captured.append(kwargs.get("json") or {})
            if "/chat/completions" in url:
                return _Stream(['data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]"])
            if url.endswith("/v1/messages"):
                return _Stream(['data: {"type":"content_block_delta","delta":{"text":"ok"}}', 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}'])
            return _Stream(['data: {"type":"response.output_text.delta","delta":"ok"}', 'data: {"type":"response.completed","response":{"usage":{}}}'])

    monkeypatch.setenv("TEST_MODEL_API_KEY", "key")
    monkeypatch.setattr(provider_streaming.httpx, "AsyncClient", _StreamClient)

    async def drain(provider) -> str:
        return "".join([delta.text async for delta in stream_provider(provider, _request(screenshot))])

    assert asyncio.run(drain(OpenAICompatibleProvider(_settings("qwen")))) == "ok"
    assert asyncio.run(drain(AnthropicProvider(_settings("anthropic")))) == "ok"
    assert asyncio.run(drain(OpenAIResponsesProvider(_settings("openai")))) == "ok"
    chat, anthropic, responses = captured
    assert chat["messages"][1]["content"][1]["image_url"]["url"].endswith(B64_A)
    assert anthropic["messages"][0]["content"][0]["source"]["data"] == B64_A
    assert responses["input"][1]["content"][1]["image_url"].endswith(B64_A)


# --------------------------------------------------------------------------- end to end through vision.ground


def _vision_runtime(tmp_path: Path, provider: str):
    profile = ModelProfileConfig(provider=provider, model="m", api_key_env="TEST_MODEL_API_KEY")
    models = ModelsConfig(default="vision", profiles={"vision": profile}, routing={"vision": {"primary": "vision", "fallback": [], "max_retries": 0}})
    router = ModelRouter(models, TokenBudgetManager(tmp_path / "token_budget.sqlite3", TokenBudgetConfig(enable_cache=True, cache_ttl_seconds=3600)))
    tools = ToolRegistry()
    tools.register(VisionGroundingTool(router, tmp_path / "workspace"))
    permissions = PermissionManager(PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.jsonl"))
    ctx = ToolContext(permissions=permissions, source="local-cli", actor="owner", run_id="run-1", plan_id="plan-1", step_index=0)
    return tools, ctx


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_vision_ground_sends_the_screenshot_and_a_new_screenshot_is_a_new_question(client, tmp_path, screenshot, provider):
    tools, ctx = _vision_runtime(tmp_path, provider)

    def ground(path: Path):
        return asyncio.run(tools.call("vision", "ground", {"image_path": str(path), "instruction": "find the login button"}, ctx))

    first = ground(screenshot)
    assert first.ok and first.data["provider"] == provider
    assert B64_A in json.dumps(client.posts[-1][1])

    # Same instruction, new screen: the model is asked again, about the new image.
    screenshot.write_bytes(PNG_B)
    second = ground(screenshot)
    assert second.ok and len(client.posts) == 2
    assert base64.b64encode(PNG_B).decode("ascii") in json.dumps(client.posts[-1][1])

    # The very same image and instruction again: served from the cache.
    screenshot.write_bytes(PNG_A)
    third = ground(screenshot)
    assert third.ok and len(client.posts) == 2


def test_cache_key_binds_image_content_and_unknown_images_are_not_cached(tmp_path):
    budget = TokenBudgetManager(tmp_path / "tb.sqlite3")
    no_image = budget.decide(model="m", system="s", user="u")
    a = budget.decide(model="m", system="s", user="u", image_digests=["a" * 64])
    b = budget.decide(model="m", system="s", user="u", image_digests=["b" * 64])
    unknown = budget.decide(model="m", system="s", user="u", image_digests=None)

    assert len({no_image.cache_key, a.cache_key, b.cache_key}) == 3
    assert unknown.allowed and unknown.cache_key is None
    # Unchanged for text-only callers, so existing cache entries stay valid.
    assert no_image.cache_key == budget.make_cache_key("m", "s", "u")
