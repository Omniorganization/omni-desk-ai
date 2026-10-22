from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx
import pytest

from omnidesk_agent.config import ModelProfileConfig, ModelsConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models import providers
from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.cost_store import ModelCostStore
from omnidesk_agent.models.provider_errors import ChatGPTPlanError, classify_provider_error
from omnidesk_agent.models.provider_streaming import stream_provider
from omnidesk_agent.models.providers import ChatGPTPlanProvider, ProviderSettings
from omnidesk_agent.models.router import ModelRouter
from omnidesk_agent.models.router_streaming import GovernedStreamingRouter


def _token(**overrides: Any) -> str:
    claims = {
        "iss": "https://auth.openai.com",
        "aud": "https://api.openai.com/v1",
        "scope": "chatgpt.tokens.use.direct resource.invoke",
        "exp": int(time.time()) + 600,
        "nbf": int(time.time()) - 60,
        **overrides,
    }
    parts = ({"alg": "RS256", "typ": "JWT"}, claims)
    # This fixture tests claim rejection only; OpenAI must validate real signatures.
    encoded = [base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=") for part in parts]
    return ".".join([*encoded, "mock-signature"])


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch):
    monkeypatch.delenv("CHATGPT_PLAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PLAN_TEST_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-api-key-must-not-be-used")


def _settings(**overrides: Any) -> ProviderSettings:
    return ProviderSettings(profile_name="plan", provider="chatgpt_plan", model="gpt-6.1-sol", **overrides)


def _request(**overrides: Any) -> ModelRequest:
    return ModelRequest(system="Use only fixture content.", user="Reply PLAN_OK.", **overrides)


def _completed(text: str = "PLAN_OK") -> list[dict[str, Any]]:
    return [
        {"type": "response.output_text.delta", "response_id": "fixture-response", "delta": text},
        {"type": "response.completed", "response": {"id": "fixture-response", "status": "completed", "usage": {"input_tokens": 4, "output_tokens": 2}}},
    ]


class _SSEStream(httpx.AsyncByteStream):
    def __init__(self, events: list[dict[str, Any]], *, disconnect: bool = False):
        self.events = events
        self.disconnect = disconnect

    async def __aiter__(self):
        yield b": fixture heartbeat\n\n"
        for event in self.events:
            yield f"data: {json.dumps(event)}\n\n".encode()
        if self.disconnect:
            raise httpx.ReadError("fixture disconnected stream")
        yield b"data: [DONE]\n\n"


def _mock_transport(monkeypatch, *, events=None, status: int = 200, disconnect: bool = False, connect_error: bool = False):
    captured: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def handle(request: httpx.Request):
        captured.append(request)
        if connect_error:
            raise httpx.ConnectError("fixture connection failure", request=request)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": "fixture_rejection", "message": "fixture rejected"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "fixture-request"},
            stream=_SSEStream(_completed() if events is None else events, disconnect=disconnect),
        )

    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs))
    return captured


def _assert_nonretryable(exc: BaseException) -> None:
    info = classify_provider_error(exc)
    assert info.category
    assert info.retryable is False
    assert info.fallback_recommended is False


def test_registered_as_separate_subscription_provider():
    assert providers.PROVIDER_CLASSES["chatgpt_plan"] is ChatGPTPlanProvider
    assert providers.PROVIDER_CLASSES["openai"] is not ChatGPTPlanProvider


def test_complete_uses_official_responses_sse_and_plan_token(monkeypatch):
    token = _token()
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", token)
    captured = _mock_transport(monkeypatch)

    result = asyncio.run(ChatGPTPlanProvider(_settings()).complete(_request()))

    assert result.text == "PLAN_OK"
    assert result.provider == "chatgpt_plan"
    assert result.profile == "plan"
    assert result.usage == {"input_tokens": 4, "output_tokens": 2}
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == f"Bearer {token}"
    body = json.loads(request.content)
    assert body["model"] == "gpt-6.1-sol"
    assert body["instructions"] == _request().system
    assert body["input"] == [{"role": "user", "content": _request().user}]
    assert body["store"] is False
    assert body["stream"] is True
    assert not ({"max_output_tokens", "temperature", "system"} & body.keys())


def test_json_mode_and_custom_plan_env(monkeypatch):
    token = _token()
    monkeypatch.setenv("PLAN_TEST_TOKEN", token)
    captured = _mock_transport(monkeypatch, events=_completed('{"answer":"PLAN_OK"}'))

    result = asyncio.run(ChatGPTPlanProvider(_settings(api_key_env="PLAN_TEST_TOKEN")).complete(_request(json_mode=True)))

    assert json.loads(result.text) == {"answer": "PLAN_OK"}
    assert json.loads(captured[0].content)["text"] == {"format": {"type": "json_object"}}
    assert captured[0].headers["authorization"] == f"Bearer {token}"


def test_native_stream_exposes_deltas_and_successful_terminal_event(monkeypatch):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch, events=[
        {"type": "response.output_text.delta", "delta": "PLAN_"},
        {"type": "response.reasoning_summary_text.delta", "delta": "fixture summary"},
        *_completed("OK"),
    ])

    async def collect():
        return [delta async for delta in stream_provider(ChatGPTPlanProvider(_settings()), _request())]

    deltas = asyncio.run(collect())
    assert "".join(delta.text for delta in deltas) == "PLAN_OK"
    assert "".join(delta.reasoning for delta in deltas) == "fixture summary"
    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].usage == {"input_tokens": 4, "output_tokens": 2}
    assert all(delta.native and delta.provider == "chatgpt_plan" for delta in deltas)
    assert len(captured) == 1


@pytest.mark.parametrize("claims", [
    {"iss": "https://issuer.invalid"},
    {"aud": "https://chatgpt.com/backend-api/codex"},
    {"aud": None},
    {"scope": "resource.invoke"},
    {"scope": "chatgpt.tokens.use.direct"},
    {"scope": None},
    {"exp": 0},
    {"exp": None},
    {"exp": True},
    {"exp": "9999999999"},
    {"nbf": None},
    {"nbf": int(time.time()) + 3600},
])
def test_rejects_wrong_scope_resource_or_validity_before_http(monkeypatch, claims):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token(**claims))
    captured = _mock_transport(monkeypatch)

    with pytest.raises(ChatGPTPlanError) as failure:
        asyncio.run(ChatGPTPlanProvider(_settings()).complete(_request()))

    _assert_nonretryable(failure.value)
    assert not captured


@pytest.mark.parametrize("token", [None, "sk-fixture-api-key", "not-a-jwt", "e30.invalid.mock"])
def test_never_uses_api_key_or_accepts_arbitrary_tokens(monkeypatch, token):
    if token is not None:
        monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", token)
    captured = _mock_transport(monkeypatch)

    with pytest.raises(ChatGPTPlanError) as failure:
        asyncio.run(ChatGPTPlanProvider(_settings()).complete(_request()))

    _assert_nonretryable(failure.value)
    assert not captured


@pytest.mark.parametrize("settings", [
    {"base_url": "https://proxy.invalid/v1"},
    {"base_url": "https://chatgpt.com/backend-api/codex"},
    {"extra_headers": {"Authorization": "Bearer override"}},
    {"extra_body": {"store": True}},
    {"api_key_env": "OPENAI_API_KEY"},
])
def test_subscription_rejects_endpoint_auth_and_payload_overrides(monkeypatch, settings):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch)

    with pytest.raises((ChatGPTPlanError, ValueError)):
        asyncio.run(ChatGPTPlanProvider(_settings(**settings)).complete(_request()))

    assert not captured


def test_image_request_is_explicitly_rejected_before_http(monkeypatch):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch)

    with pytest.raises(ChatGPTPlanError) as failure:
        asyncio.run(ChatGPTPlanProvider(_settings()).complete(_request(images=["/fixture/image.png"])))

    _assert_nonretryable(failure.value)
    assert not captured


@pytest.mark.parametrize("event", [
    {"type": "response.failed", "response": {"status": "failed", "error": {"code": "fixture"}}},
    {"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}},
    {"type": "error", "code": "fixture_error", "message": "fixture rejected"},
    None,
])
def test_stream_failure_or_missing_completed_never_returns_success(monkeypatch, event):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    events = [{"type": "response.output_text.delta", "delta": "partial"}]
    if event is not None:
        events.append(event)
    captured = _mock_transport(monkeypatch, events=events)

    with pytest.raises(ChatGPTPlanError) as failure:
        asyncio.run(ChatGPTPlanProvider(_settings()).complete(_request()))

    _assert_nonretryable(failure.value)
    assert len(captured) == 1


class _BackupProvider:
    calls = 0

    def __init__(self, settings):
        self.settings = settings
        self.model = settings.model
        self.profile_name = settings.profile_name
        self.provider_name = "fixture_backup"

    async def complete(self, request):
        type(self).calls += 1
        return ModelResponse(text="FORBIDDEN_FALLBACK", provider=self.provider_name, model=self.model, profile=self.profile_name)


def _router(monkeypatch, tmp_path, *, enabled: bool = True):
    monkeypatch.setitem(providers.PROVIDER_CLASSES, "fixture_backup", _BackupProvider)
    _BackupProvider.calls = 0
    config = ModelsConfig(
        default="plan",
        profiles={
            "plan": ModelProfileConfig(provider="chatgpt_plan", model="gpt-6.1-sol", api_key_env=None, enabled=enabled),
            "backup": ModelProfileConfig(provider="fixture_backup", model="fixture", api_key_env=None),
        },
        routing={"chat": {"primary": "plan", "fallback": ["backup"], "max_retries": 2}},
    )
    return ModelRouter(config, TokenBudgetManager(tmp_path / "tokens.sqlite3"))


async def _route_call(router, *, stream: bool, request=None):
    request = request or _request(metadata={"role": "owner"})
    if stream:
        return [delta async for delta in GovernedStreamingRouter(router).stream(request)]
    return await router.complete(request)


@pytest.mark.parametrize("stream", [False, True])
def test_successful_subscription_route_records_usage_without_platform_cost(monkeypatch, tmp_path, stream):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch)
    router = _router(monkeypatch, tmp_path)

    result = asyncio.run(_route_call(router, stream=stream))

    text = "".join(delta.text for delta in result) if stream else result.text
    assert text == "PLAN_OK"
    assert len(captured) == 1
    assert _BackupProvider.calls == 0
    summary = router.cost_ledger.summary()
    assert summary["calls"] == 1
    assert summary["estimated_cost"] == 0.0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure_kind", ["auth", "quota", "connect", "disconnect", "truncated"])
def test_routers_never_retry_or_fallback_for_subscription_errors(monkeypatch, tmp_path, stream, failure_kind):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    options = {
        "auth": {"status": 401},
        "quota": {"status": 429},
        "connect": {"connect_error": True},
        "disconnect": {"events": [{"type": "response.output_text.delta", "delta": "partial"}], "disconnect": True},
        "truncated": {"events": []},
    }
    captured = _mock_transport(monkeypatch, **options[failure_kind])
    router = _router(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError):
        asyncio.run(_route_call(router, stream=stream))

    assert len(captured) == 1
    assert _BackupProvider.calls == 0
    assert router.cost_ledger.summary()["calls"] == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("blocked", ["circuit", "disabled"])
def test_subscription_circuit_or_disabled_profile_cannot_bypass_to_backup(monkeypatch, tmp_path, stream, blocked):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch)
    router = _router(monkeypatch, tmp_path, enabled=blocked != "disabled")
    if blocked == "circuit":
        router._circuit["plan"] = {"failures": 99, "opened_at": time.time()}

    with pytest.raises(RuntimeError):
        asyncio.run(_route_call(router, stream=stream))

    assert not captured
    assert _BackupProvider.calls == 0


@pytest.mark.parametrize("stream", [False, True])
def test_budget_exceeded_stops_subscription_instead_of_local_fallback(monkeypatch, tmp_path, stream):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch)
    router = _router(monkeypatch, tmp_path)
    router.providers["local"] = _BackupProvider(ProviderSettings(profile_name="local", provider="fixture_backup", model="fixture"))
    store = ModelCostStore(tmp_path / "costs.sqlite3")
    store.record(provider="fixture", model="fixture", profile="fixture", estimated_cost_usd=2.0)
    router.cfg.budget.daily_usd_limit = 1.0
    router.cfg.budget.on_exceed = "fallback_local"
    router.budget_enforcer = router._build_budget_enforcer(store)

    with pytest.raises(ChatGPTPlanError) as failure:
        asyncio.run(_route_call(router, stream=stream))

    _assert_nonretryable(failure.value)
    assert not captured
    assert _BackupProvider.calls == 0


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_subscription_profile_respects_offline_guard(monkeypatch, tmp_path, stream):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch)
    router = _router(monkeypatch, tmp_path)
    router.cfg.offline_mode = True
    request = _request(metadata={"profile": "plan", "role": "owner"})

    with pytest.raises(RuntimeError):
        asyncio.run(_route_call(router, stream=stream, request=request))

    assert not captured
    assert _BackupProvider.calls == 0


@pytest.mark.parametrize("stream", [False, True])
def test_invalid_structured_output_cannot_trigger_subscription_repair_retry(monkeypatch, tmp_path, stream):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch, events=_completed("FIXTURE_PRIVATE_JSON_OUTPUT"))
    router = _router(monkeypatch, tmp_path)
    request = _request(json_mode=True, metadata={"role": "owner", "schema_retry_max": 9})

    with pytest.raises(ChatGPTPlanError) as failure:
        asyncio.run(_route_call(router, stream=stream, request=request))

    assert "FIXTURE_PRIVATE_JSON_OUTPUT" not in str(failure.value)
    _assert_nonretryable(failure.value)
    assert len(captured) == 1
    assert _BackupProvider.calls == 0
    assert router.cost_ledger.summary()["calls"] == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_prior_cache_cannot_hide_missing_or_expired_subscription_authorization(monkeypatch, tmp_path, stream, expired):
    if expired:
        monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token(exp=0))
    captured = _mock_transport(monkeypatch)
    router = _router(monkeypatch, tmp_path)
    request = _request(metadata={"role": "owner"})
    cache_key = router.token_budget.make_cache_key("plan:gpt-6.1-sol", request.system, request.user)
    router.token_budget.put_cached(cache_key=cache_key, model="plan:gpt-6.1-sol", response="OLD_PROVIDER_CACHE")

    with pytest.raises(ChatGPTPlanError) as failure:
        asyncio.run(_route_call(router, stream=stream, request=request))

    _assert_nonretryable(failure.value)
    assert not captured
    assert _BackupProvider.calls == 0
    assert router.cost_ledger.summary()["calls"] == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("seed_cache", [False, True])
def test_repeated_subscription_calls_do_not_read_or_write_token_budget_cache(monkeypatch, tmp_path, stream, seed_cache):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch)
    router = _router(monkeypatch, tmp_path)
    request = _request(metadata={"role": "owner"})
    cache_key = router.token_budget.make_cache_key("plan:gpt-6.1-sol", request.system, request.user)
    if seed_cache:
        router.token_budget.put_cached(cache_key=cache_key, model="plan:gpt-6.1-sol", response="OLD_PROVIDER_CACHE")

    for _ in range(2):
        result = asyncio.run(_route_call(router, stream=stream, request=request))
        text = "".join(delta.text for delta in result) if stream else result.text
        assert text == "PLAN_OK"

    assert len(captured) == 2
    assert _BackupProvider.calls == 0
    assert router.cost_ledger.summary()["calls"] == 2
    assert router.token_budget.get_cached(cache_key) == ("OLD_PROVIDER_CACHE" if seed_cache else None)


@pytest.mark.parametrize("stream", [False, True])
def test_implicit_subscription_routing_enforces_operator_profile_acl(monkeypatch, tmp_path, stream):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch)
    router = _router(monkeypatch, tmp_path)
    request = _request(metadata={"role": "operator"})

    with pytest.raises(PermissionError, match="model profile denied by ACL"):
        asyncio.run(_route_call(router, stream=stream, request=request))

    assert "profile" not in request.metadata
    assert not captured
    assert _BackupProvider.calls == 0
    assert router.cost_ledger.summary()["calls"] == 0


class _FailingPrimaryProvider(_BackupProvider):
    calls = 0

    async def complete(self, request):
        type(self).calls += 1
        raise RuntimeError("fixture primary unavailable")


@pytest.mark.parametrize("stream", [False, True])
def test_subscription_used_as_fallback_is_not_recalled_after_ledger_write_failure(monkeypatch, tmp_path, stream):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch)
    router = _router(monkeypatch, tmp_path)
    _FailingPrimaryProvider.calls = 0
    router.providers["backup"] = _FailingPrimaryProvider(router.providers["backup"].settings)
    router.providers["secondary"] = _BackupProvider(ProviderSettings(profile_name="secondary", provider="fixture_backup", model="fixture"))
    router.cfg.profiles["secondary"] = ModelProfileConfig(provider="fixture_backup", model="fixture", api_key_env=None)
    router.cfg.routing["chat"] = {"primary": "backup", "fallback": ["plan", "secondary"], "max_retries": 1}
    store = ModelCostStore(tmp_path / "failed-ledger.sqlite3")
    router.cost_ledger.store = store

    def fail_write(**kwargs):
        raise OSError("fixture ledger write failure")

    monkeypatch.setattr(store, "record", fail_write)

    with pytest.raises(OSError, match="fixture ledger write failure"):
        asyncio.run(_route_call(router, stream=stream))

    assert _FailingPrimaryProvider.calls == 2
    assert len(captured) == 1
    assert _BackupProvider.calls == 0
    assert store.summary()["calls"] == 0


@pytest.mark.parametrize("code,category", [
    ("subscription_sharing_usage_limit_exceeded", "subscription_limit"),
    ("subscription_sharing_usage_unavailable", "subscription_limit"),
    ("fixture_generic_failure", "subscription_response"),
])
def test_subscription_failure_codes_are_classified_without_exposing_raw_error(monkeypatch, code, category):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch, events=[
        {"type": "response.failed", "response": {"error": {"code": code, "message": "FIXTURE_PRIVATE_ERROR"}}},
    ])

    with pytest.raises(ChatGPTPlanError) as failure:
        asyncio.run(ChatGPTPlanProvider(_settings()).complete(_request()))

    assert classify_provider_error(failure.value).category == category
    assert "FIXTURE_PRIVATE_ERROR" not in str(failure.value)
    _assert_nonretryable(failure.value)
    assert len(captured) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("blocked", ["circuit", "disabled"])
def test_blocked_subscription_in_fallback_chain_stops_before_secondary(monkeypatch, tmp_path, stream, blocked):
    monkeypatch.setenv("CHATGPT_PLAN_ACCESS_TOKEN", _token())
    captured = _mock_transport(monkeypatch)
    router = _router(monkeypatch, tmp_path, enabled=blocked != "disabled")
    _FailingPrimaryProvider.calls = 0
    router.providers["backup"] = _FailingPrimaryProvider(router.providers["backup"].settings)
    router.providers["secondary"] = _BackupProvider(ProviderSettings(profile_name="secondary", provider="fixture_backup", model="fixture"))
    router.cfg.profiles["secondary"] = ModelProfileConfig(provider="fixture_backup", model="fixture", api_key_env=None)
    router.cfg.routing["chat"] = {"primary": "backup", "fallback": ["plan", "secondary"], "max_retries": 0}
    if blocked == "circuit":
        router._circuit["plan"] = {"failures": 99, "opened_at": time.time()}

    with pytest.raises(ChatGPTPlanError) as failure:
        asyncio.run(_route_call(router, stream=stream))

    _assert_nonretryable(failure.value)
    assert _FailingPrimaryProvider.calls == 1
    assert not captured
    assert _BackupProvider.calls == 0
    assert router.cost_ledger.summary()["calls"] == 0
