from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.provider_streaming import stream_provider
from omnidesk_agent.models.router import RoutePlan
from omnidesk_agent.models.router_streaming import GovernedStreamingRouter


class FakeProvider:
    provider_name = "test_provider"
    model = "test-model"
    profile_name = "fast"
    settings = SimpleNamespace(max_output_tokens=64)

    async def complete(self, _request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="fallback text",
            provider=self.provider_name,
            model=self.model,
            profile=self.profile_name,
            usage={"output_tokens": 2},
            raw={"id": "provider-request-1"},
        )


@pytest.mark.asyncio
async def test_unsupported_provider_uses_explicit_non_native_fallback() -> None:
    deltas = [item async for item in stream_provider(FakeProvider(), ModelRequest("s", "u"))]
    assert len(deltas) == 1
    assert deltas[0].text == "fallback text"
    assert deltas[0].finish_reason == "stop"
    assert deltas[0].native is False
    assert deltas[0].provider_request_id == "provider-request-1"


@dataclass
class FakeDecision:
    allowed: bool = True
    cache_key: str = "cache-key"
    truncated_system: str | None = None
    truncated_user: str | None = None
    estimated_input_tokens: int = 3
    budget_overridden: bool = False
    reason: str = "within-budget"


class FakeTokenBudget:
    def decide(self, **_kwargs):
        return FakeDecision()

    def get_cached(self, _key: str):
        return None

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text.split()))

    def record_call(self, **kwargs):
        self.recorded = kwargs

    def put_cached(self, **kwargs):
        self.cached = kwargs


class FakeLedger:
    def record(self, **kwargs):
        self.recorded = kwargs


class FakePricing:
    def estimate(self, **_kwargs) -> float:
        return 0.001


class FakeRouter:
    def __init__(self):
        self.providers = {"fast": FakeProvider()}
        self.token_budget = FakeTokenBudget()
        self.cost_ledger = FakeLedger()
        self.pricing_table = FakePricing()
        self.error_counts: dict[str, int] = {}
        self.cfg = SimpleNamespace(max_output_tokens=64)
        self.successes: list[str] = []

    def route_plan(self, _task: str, _metadata):
        return RoutePlan(profiles=["fast"], max_retries=0)

    def _offline_forbids_profile(self, _profile, _provider):
        return False

    def _circuit_open(self, _profile, _plan):
        return False

    def _check_budget(self, _request, *, profile, provider):
        assert profile == "fast"
        assert provider is self.providers["fast"]
        return None

    def _record_failure(self, _profile):
        raise AssertionError("failure was not expected")

    def _record_success(self, profile):
        self.successes.append(profile)


@pytest.mark.asyncio
async def test_streaming_router_preserves_budget_ledger_and_cache_contracts() -> None:
    router = FakeRouter()
    adapter = GovernedStreamingRouter(router)  # type: ignore[arg-type]
    request = ModelRequest(
        system="system",
        user="hello",
        task="chat",
        task_id="task-stream-1",
        metadata={"actor": "operator-1"},
    )

    deltas = [item async for item in adapter.stream(request)]

    assert "".join(item.text for item in deltas) == "fallback text"
    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].usage is not None
    assert deltas[-1].usage["actor"] == "operator-1"
    assert deltas[-1].usage["estimated_cost_usd"] == 0.001
    assert router.successes == ["fast"]
    assert router.token_budget.recorded["task_id"] == "task-stream-1"
    assert router.cost_ledger.recorded["profile"] == "fast"
    assert router.token_budget.cached["response"] == "fallback text"
