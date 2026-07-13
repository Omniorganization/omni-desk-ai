from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models import provider_streaming as streaming


class FakeResponse:
    def __init__(self, lines: list[str], headers: dict[str, str] | None = None):
        self.lines = lines
        self.headers = headers or {}
        self.status_checked = False

    def raise_for_status(self) -> None:
        self.status_checked = True

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class ResponseContext:
    def __init__(self, response: FakeResponse):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class FakeClient:
    def __init__(self, response: FakeResponse, captured: dict[str, Any]):
        self.response = response
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method: str, url: str, **kwargs):
        self.captured.update(method=method, url=url, kwargs=kwargs)
        return ResponseContext(self.response)


class Provider:
    def __init__(self, name: str, *, base_url: str | None = None):
        self.provider_name = name
        self.model = "model-1"
        self.profile_name = "fast"
        self.settings = SimpleNamespace(
            api_key_env="TEST_MODEL_KEY",
            base_url=base_url,
            temperature=0.2,
            max_output_tokens=64,
            extra_body={"vendor_option": True},
            extra_headers={"x-extra": "yes"},
            api_version="2024-10-21",
        )

    async def complete(self, _request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="compat",
            provider=self.provider_name,
            model=self.model,
            profile=self.profile_name,
            usage={"output_tokens": 1},
            raw={"id": "compat-id"},
        )


def install_client(monkeypatch, response: FakeResponse) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        streaming.httpx,
        "AsyncClient",
        lambda **_kwargs: FakeClient(response, captured),
    )
    monkeypatch.setattr(streaming, "env", lambda _name: "secret")
    return captured


@pytest.mark.asyncio
async def test_sse_payload_parser_ignores_heartbeat_invalid_and_done() -> None:
    response = FakeResponse(
        [
            "",
            ": heartbeat",
            "event: message",
            "data: not-json",
            "data: [1,2]",
            'data: {"ok":true}',
            "data: [DONE]",
        ]
    )
    payloads = [payload async for payload in streaming._sse_payloads(response)]
    assert payloads == [{"ok": True}]


@pytest.mark.asyncio
async def test_openai_compatible_stream_emits_text_reasoning_usage(monkeypatch) -> None:
    response = FakeResponse(
        [
            'data: {"id":"req-1","choices":[{"delta":{"content":"hello ","reasoning_content":"think"}}]}',
            'data: {"id":"req-1","choices":[{"delta":{"content":"world"},"finish_reason":"stop"}]}',
            'data: {"usage":{"prompt_tokens":3,"completion_tokens":2},"choices":[]}',
            "data: [DONE]",
        ],
        {"x-request-id": "header-request"},
    )
    captured = install_client(monkeypatch, response)
    provider = Provider("deepseek", base_url="https://deepseek.example/v1")
    request = ModelRequest("system", "user", json_mode=True)
    deltas = [item async for item in streaming.stream_provider(provider, request)]
    assert [item.text for item in deltas if item.text] == ["hello ", "world"]
    assert [item.reasoning for item in deltas if item.reasoning] == ["think"]
    assert deltas[-1].usage == {"prompt_tokens": 3, "completion_tokens": 2}
    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].provider_request_id == "req-1"
    assert captured["url"] == "https://deepseek.example/v1/chat/completions"
    body = captured["kwargs"]["json"]
    assert body["stream"] is True
    assert body["response_format"] == {"type": "json_object"}
    assert body["vendor_option"] is True
    assert captured["kwargs"]["headers"]["x-extra"] == "yes"


@pytest.mark.asyncio
async def test_openai_responses_stream_emits_reasoning_and_completion(monkeypatch) -> None:
    response = FakeResponse(
        [
            'data: {"type":"response.output_text.delta","response_id":"resp-1","delta":"answer"}',
            'data: {"type":"response.reasoning_summary_text.delta","response_id":"resp-1","delta":"reason"}',
            'data: {"type":"response.completed","response":{"id":"resp-1","usage":{"input_tokens":4,"output_tokens":2}}}',
        ],
        {"x-request-id": "header-resp"},
    )
    captured = install_client(monkeypatch, response)
    provider = Provider("openai")
    deltas = [
        item
        async for item in streaming.stream_provider(
            provider,
            ModelRequest("system", "user", json_mode=True),
        )
    ]
    assert deltas[0].text == "answer"
    assert deltas[1].reasoning == "reason"
    assert deltas[-1].usage == {"input_tokens": 4, "output_tokens": 2}
    assert deltas[-1].provider_request_id == "resp-1"
    assert captured["url"].endswith("/responses")
    assert captured["kwargs"]["json"]["text"] == {"format": {"type": "json_object"}}


@pytest.mark.asyncio
async def test_azure_stream_uses_deployment_endpoint(monkeypatch) -> None:
    response = FakeResponse(
        [
            'data: {"choices":[{"delta":{"content":"azure","reasoning_content":"r"},"finish_reason":"stop"}]}',
            'data: {"usage":{"prompt_tokens":1,"completion_tokens":1},"choices":[]}',
        ],
        {"apim-request-id": "azure-request"},
    )
    captured = install_client(monkeypatch, response)
    provider = Provider("azure_openai", base_url="https://azure.example")
    deltas = [item async for item in streaming.stream_provider(provider, ModelRequest("s", "u", json_mode=True))]
    assert deltas[0].text == "azure"
    assert deltas[0].reasoning == "r"
    assert deltas[-1].provider_request_id == "azure-request"
    assert "/openai/deployments/model-1/chat/completions?api-version=2024-10-21" in captured["url"]
    assert captured["kwargs"]["headers"]["api-key"] == "secret"


@pytest.mark.asyncio
async def test_anthropic_stream_accumulates_usage(monkeypatch) -> None:
    response = FakeResponse(
        [
            'data: {"type":"message_start","message":{"id":"msg-1","usage":{"input_tokens":4}}}',
            'data: {"type":"content_block_delta","delta":{"text":"anthropic"}}',
            'data: {"type":"content_block_delta","delta":{"thinking":"reasoning"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}',
        ],
        {"request-id": "header-message"},
    )
    captured = install_client(monkeypatch, response)
    provider = Provider("anthropic")
    deltas = [item async for item in streaming.stream_provider(provider, ModelRequest("s", "u"))]
    assert deltas[0].text == "anthropic"
    assert deltas[1].reasoning == "reasoning"
    assert deltas[-1].usage == {"input_tokens": 4, "output_tokens": 3}
    assert deltas[-1].finish_reason == "end_turn"
    assert captured["kwargs"]["headers"]["anthropic-version"] == "2024-10-21"


@pytest.mark.asyncio
async def test_gemini_sse_stream(monkeypatch) -> None:
    response = FakeResponse(
        [
            'data: {"candidates":[{"content":{"parts":[{"text":"gem"},{"text":"ini"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":2}}'
        ]
    )
    captured = install_client(monkeypatch, response)
    provider = Provider("gemini", base_url="https://gemini.example")
    deltas = [item async for item in streaming.stream_provider(provider, ModelRequest("s", "u"))]
    assert deltas[0].text == "gemini"
    assert deltas[-1].finish_reason == "STOP"
    assert deltas[-1].usage["promptTokenCount"] == 2
    assert captured["kwargs"]["params"] == {"key": "secret", "alt": "sse"}


@pytest.mark.asyncio
async def test_ollama_ndjson_stream(monkeypatch) -> None:
    response = FakeResponse(
        [
            "",
            json.dumps({"message": {"content": "local "}, "done": False}),
            json.dumps(
                {
                    "message": {"content": "answer"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 3,
                    "eval_count": 2,
                    "total_duration": 99,
                }
            ),
        ]
    )
    captured = install_client(monkeypatch, response)
    provider = Provider("ollama", base_url="http://127.0.0.1:11434")
    deltas = [item async for item in streaming.stream_provider(provider, ModelRequest("s", "u"))]
    assert [item.text for item in deltas if item.text] == ["local ", "answer"]
    assert deltas[-1].usage["eval_count"] == 2
    assert captured["url"].endswith("/api/chat")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_name", "base_url", "expected"),
    [
        ("openai", None, "Missing TEST_MODEL_KEY"),
        ("deepseek", "https://deepseek.example/v1", "Missing TEST_MODEL_KEY"),
        ("anthropic", None, "Missing TEST_MODEL_KEY"),
        ("gemini", None, "Missing TEST_MODEL_KEY"),
        ("azure_openai", "", "Missing TEST_MODEL_KEY"),
    ],
)
async def test_remote_streams_require_api_key(monkeypatch, provider_name, base_url, expected) -> None:
    monkeypatch.setattr(streaming, "env", lambda _name: "")
    provider = Provider(provider_name, base_url=base_url)
    with pytest.raises(RuntimeError, match=expected):
        _ = [item async for item in streaming.stream_provider(provider, ModelRequest("s", "u"))]


@pytest.mark.asyncio
async def test_unsupported_provider_uses_complete_fallback() -> None:
    provider = Provider("custom-provider")
    deltas = [item async for item in streaming.stream_provider(provider, ModelRequest("s", "u"))]
    assert deltas[0].text == "compat"
    assert deltas[0].native is False
    assert deltas[0].provider_request_id == "compat-id"
