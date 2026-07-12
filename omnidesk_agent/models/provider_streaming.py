from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from omnidesk_agent.models.base import ModelDelta, ModelRequest
from omnidesk_agent.models.providers import _image_part_from_path, env, msgs


def _delta(
    provider: Any,
    sequence: int,
    *,
    text: str = "",
    reasoning: str = "",
    usage: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    provider_request_id: str | None = None,
    native: bool = True,
) -> ModelDelta:
    return ModelDelta(
        sequence=sequence,
        provider=str(getattr(provider, "provider_name", "unknown")),
        model=str(getattr(provider, "model", "unknown")),
        profile=str(getattr(provider, "profile_name", "unknown")),
        text=text,
        reasoning=reasoning,
        usage=usage,
        finish_reason=finish_reason,
        provider_request_id=provider_request_id,
        native=native,
    )


async def _sse_payloads(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith(":") or not stripped.startswith("data:"):
            continue
        raw = stripped[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("type") or "") == "error":
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            error_type = str(error.get("type") or "provider_stream_error")
            message = str(error.get("message") or "provider stream returned an error event")
            raise RuntimeError(f"{error_type}: {message}")
        yield payload


async def _openai_compatible_stream(
    provider: Any,
    request: ModelRequest,
) -> AsyncIterator[ModelDelta]:
    settings = provider.settings
    key = env(settings.api_key_env)
    if not key:
        raise RuntimeError(f"Missing {settings.api_key_env}")
    base = (settings.base_url or "https://api.openai.com/v1").rstrip("/")
    body: dict[str, Any] = {
        "model": provider.model,
        "messages": msgs(request.system, request.user),
        "temperature": settings.temperature,
        "max_tokens": settings.max_output_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.json_mode:
        body["response_format"] = {"type": "json_object"}
    if settings.extra_body:
        body.update(settings.extra_body)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if settings.extra_headers:
        headers.update(settings.extra_headers)

    sequence = 1
    final_usage: dict[str, Any] | None = None
    final_reason: str | None = None
    request_id: str | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, read=None)) as client:
        async with client.stream(
            "POST",
            f"{base}/chat/completions",
            headers=headers,
            json=body,
        ) as response:
            response.raise_for_status()
            request_id = response.headers.get("x-request-id") or response.headers.get(
                "request-id"
            )
            async for payload in _sse_payloads(response):
                request_id = str(payload.get("id") or request_id or "") or None
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    final_usage = usage
                choices = payload.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] if isinstance(choices[0], dict) else {}
                delta = (
                    choice.get("delta")
                    if isinstance(choice.get("delta"), dict)
                    else {}
                )
                text = delta.get("content") or ""
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                finish = choice.get("finish_reason")
                if finish:
                    final_reason = str(finish)
                if text or reasoning:
                    yield _delta(
                        provider,
                        sequence,
                        text=str(text),
                        reasoning=str(reasoning),
                        provider_request_id=request_id,
                    )
                    sequence += 1
    yield _delta(
        provider,
        sequence,
        usage=final_usage or {},
        finish_reason=final_reason or "stop",
        provider_request_id=request_id,
    )


async def _openai_responses_stream(
    provider: Any,
    request: ModelRequest,
) -> AsyncIterator[ModelDelta]:
    settings = provider.settings
    key = env(settings.api_key_env)
    if not key:
        raise RuntimeError(f"Missing {settings.api_key_env}")
    base = (settings.base_url or "https://api.openai.com/v1").rstrip("/")
    body: dict[str, Any] = {
        "model": provider.model,
        "input": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ],
        "max_output_tokens": settings.max_output_tokens,
        "stream": True,
    }
    if request.json_mode:
        body["text"] = {"format": {"type": "json_object"}}

    sequence = 1
    usage: dict[str, Any] | None = None
    request_id: str | None = None
    finish_reason: str | None = None
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, read=None)) as client:
        async with client.stream(
            "POST",
            f"{base}/responses",
            headers=headers,
            json=body,
        ) as response:
            response.raise_for_status()
            request_id = response.headers.get("x-request-id")
            async for payload in _sse_payloads(response):
                event_type = str(payload.get("type") or "")
                response_obj = (
                    payload.get("response")
                    if isinstance(payload.get("response"), dict)
                    else {}
                )
                request_id = str(
                    response_obj.get("id")
                    or payload.get("response_id")
                    or request_id
                    or ""
                ) or None
                if event_type in {
                    "response.output_text.delta",
                    "response.refusal.delta",
                }:
                    text = str(payload.get("delta") or "")
                    if text:
                        yield _delta(
                            provider,
                            sequence,
                            text=text,
                            provider_request_id=request_id,
                        )
                        sequence += 1
                elif event_type in {
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                }:
                    reasoning = str(payload.get("delta") or "")
                    if reasoning:
                        yield _delta(
                            provider,
                            sequence,
                            reasoning=reasoning,
                            provider_request_id=request_id,
                        )
                        sequence += 1
                elif event_type in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                }:
                    raw_usage = response_obj.get("usage")
                    if isinstance(raw_usage, dict):
                        usage = raw_usage
                    finish_reason = (
                        "stop"
                        if event_type == "response.completed"
                        else event_type.removeprefix("response.")
                    )
    yield _delta(
        provider,
        sequence,
        usage=usage or {},
        finish_reason=finish_reason or "stop",
        provider_request_id=request_id,
    )


async def _azure_stream(
    provider: Any,
    request: ModelRequest,
) -> AsyncIterator[ModelDelta]:
    settings = provider.settings
    key = env(settings.api_key_env)
    endpoint = (settings.base_url or "").rstrip("/")
    if not key:
        raise RuntimeError(f"Missing {settings.api_key_env}")
    if not endpoint:
        raise RuntimeError("Azure OpenAI base_url is required")
    version = settings.api_version or "2024-10-21"
    body: dict[str, Any] = {
        "messages": msgs(request.system, request.user),
        "temperature": settings.temperature,
        "max_tokens": settings.max_output_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {
        "api-key": key,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    url = (
        f"{endpoint}/openai/deployments/{provider.model}/chat/completions"
        f"?api-version={version}"
    )
    sequence = 1
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    request_id: str | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, read=None)) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            response.raise_for_status()
            request_id = response.headers.get("x-request-id") or response.headers.get(
                "apim-request-id"
            )
            async for payload in _sse_payloads(response):
                raw_usage = payload.get("usage")
                if isinstance(raw_usage, dict):
                    usage = raw_usage
                choices = payload.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] if isinstance(choices[0], dict) else {}
                delta = (
                    choice.get("delta")
                    if isinstance(choice.get("delta"), dict)
                    else {}
                )
                text = str(delta.get("content") or "")
                reasoning = str(delta.get("reasoning_content") or "")
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                if text or reasoning:
                    yield _delta(
                        provider,
                        sequence,
                        text=text,
                        reasoning=reasoning,
                        provider_request_id=request_id,
                    )
                    sequence += 1
    yield _delta(
        provider,
        sequence,
        usage=usage or {},
        finish_reason=finish_reason or "stop",
        provider_request_id=request_id,
    )


async def _anthropic_stream(
    provider: Any,
    request: ModelRequest,
) -> AsyncIterator[ModelDelta]:
    settings = provider.settings
    key = env(settings.api_key_env)
    if not key:
        raise RuntimeError(f"Missing {settings.api_key_env}")
    base = (settings.base_url or "https://api.anthropic.com").rstrip("/")
    body = {
        "model": provider.model,
        "max_tokens": settings.max_output_tokens,
        "temperature": settings.temperature,
        "system": request.system,
        "messages": [{"role": "user", "content": request.user}],
        "stream": True,
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": settings.api_version or "2023-06-01",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    sequence = 1
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    request_id: str | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, read=None)) as client:
        async with client.stream(
            "POST",
            f"{base}/v1/messages",
            headers=headers,
            json=body,
        ) as response:
            response.raise_for_status()
            request_id = response.headers.get("request-id")
            async for payload in _sse_payloads(response):
                event_type = str(payload.get("type") or "")
                if event_type == "message_start":
                    message = (
                        payload.get("message")
                        if isinstance(payload.get("message"), dict)
                        else {}
                    )
                    request_id = str(message.get("id") or request_id or "") or None
                    raw_usage = message.get("usage")
                    if isinstance(raw_usage, dict):
                        usage.update(raw_usage)
                elif event_type == "content_block_delta":
                    delta = (
                        payload.get("delta")
                        if isinstance(payload.get("delta"), dict)
                        else {}
                    )
                    text = str(delta.get("text") or "")
                    reasoning = str(delta.get("thinking") or "")
                    if text or reasoning:
                        yield _delta(
                            provider,
                            sequence,
                            text=text,
                            reasoning=reasoning,
                            provider_request_id=request_id,
                        )
                        sequence += 1
                elif event_type == "message_delta":
                    delta = (
                        payload.get("delta")
                        if isinstance(payload.get("delta"), dict)
                        else {}
                    )
                    raw_usage = payload.get("usage")
                    if isinstance(raw_usage, dict):
                        usage.update(raw_usage)
                    if delta.get("stop_reason"):
                        finish_reason = str(delta["stop_reason"])
    yield _delta(
        provider,
        sequence,
        usage=usage,
        finish_reason=finish_reason or "stop",
        provider_request_id=request_id,
    )


async def _gemini_stream(
    provider: Any,
    request: ModelRequest,
) -> AsyncIterator[ModelDelta]:
    settings = provider.settings
    key = env(settings.api_key_env)
    if not key:
        raise RuntimeError(f"Missing {settings.api_key_env}")
    parts: list[dict[str, Any]] = [
        {"text": f"{request.system}\n\n{request.user}"}
    ]
    for image in request.images:
        parts.append(_image_part_from_path(str(image), request.metadata))
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": settings.temperature,
            "maxOutputTokens": settings.max_output_tokens,
        },
    }
    base = (
        settings.base_url or "https://generativelanguage.googleapis.com"
    ).rstrip("/")
    url = f"{base}/v1beta/models/{provider.model}:streamGenerateContent"
    sequence = 1
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, read=None)) as client:
        async with client.stream(
            "POST",
            url,
            params={"key": key, "alt": "sse"},
            json=body,
        ) as response:
            response.raise_for_status()
            async for payload in _sse_payloads(response):
                raw_usage = payload.get("usageMetadata")
                if isinstance(raw_usage, dict):
                    usage = raw_usage
                candidates = payload.get("candidates") or []
                if not candidates:
                    continue
                candidate = candidates[0] if isinstance(candidates[0], dict) else {}
                if candidate.get("finishReason"):
                    finish_reason = str(candidate["finishReason"])
                content = (
                    candidate.get("content")
                    if isinstance(candidate.get("content"), dict)
                    else {}
                )
                text = "".join(
                    str(part.get("text") or "")
                    for part in content.get("parts", [])
                    if isinstance(part, dict)
                )
                if text:
                    yield _delta(provider, sequence, text=text)
                    sequence += 1
    yield _delta(
        provider,
        sequence,
        usage=usage or {},
        finish_reason=finish_reason or "stop",
    )


async def _ollama_stream(
    provider: Any,
    request: ModelRequest,
) -> AsyncIterator[ModelDelta]:
    settings = provider.settings
    base = (settings.base_url or "http://127.0.0.1:11434").rstrip("/")
    body = {
        "model": provider.model,
        "stream": True,
        "messages": msgs(request.system, request.user),
        "options": {
            "temperature": settings.temperature,
            "num_predict": settings.max_output_tokens,
        },
    }
    sequence = 1
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, read=None)) as client:
        async with client.stream("POST", f"{base}/api/chat", json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                message = (
                    payload.get("message")
                    if isinstance(payload.get("message"), dict)
                    else {}
                )
                text = str(message.get("content") or "")
                if text:
                    yield _delta(provider, sequence, text=text)
                    sequence += 1
                if payload.get("done"):
                    finish_reason = str(payload.get("done_reason") or "stop")
                    usage = {
                        "prompt_eval_count": payload.get("prompt_eval_count", 0),
                        "eval_count": payload.get("eval_count", 0),
                        "total_duration": payload.get("total_duration", 0),
                    }
    yield _delta(
        provider,
        sequence,
        usage=usage,
        finish_reason=finish_reason or "stop",
    )


async def _compatibility_stream(
    provider: Any,
    request: ModelRequest,
) -> AsyncIterator[ModelDelta]:
    response = await provider.complete(request)
    yield _delta(
        provider,
        1,
        text=response.text,
        usage=response.usage or {},
        finish_reason="stop",
        provider_request_id=str((response.raw or {}).get("id") or "") or None,
        native=False,
    )


async def stream_provider(
    provider: Any,
    request: ModelRequest,
) -> AsyncIterator[ModelDelta]:
    """Use a provider-native transport where one is supported."""

    name = str(getattr(provider, "provider_name", ""))
    if name == "openai":
        async for item in _openai_responses_stream(provider, request):
            yield item
        return
    if name in {
        "openai_compatible",
        "deepseek",
        "qwen",
        "dashscope",
        "groq",
        "mistral",
        "xai",
        "openrouter",
        "together",
        "fireworks",
        "perplexity",
        "moonshot",
        "kimi",
        "zhipu",
        "baidu_qianfan",
    }:
        async for item in _openai_compatible_stream(provider, request):
            yield item
        return
    if name == "azure_openai":
        async for item in _azure_stream(provider, request):
            yield item
        return
    if name == "anthropic":
        async for item in _anthropic_stream(provider, request):
            yield item
        return
    if name == "gemini":
        async for item in _gemini_stream(provider, request):
            yield item
        return
    if name == "ollama":
        async for item in _ollama_stream(provider, request):
            yield item
        return
    async for item in _compatibility_stream(provider, request):
        yield item
