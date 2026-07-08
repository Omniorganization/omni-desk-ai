from __future__ import annotations

import asyncio
from pathlib import Path

from omnidesk_agent.config import ModelProfileConfig, ModelsConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.cost_store import ModelCostStore
from omnidesk_agent.models.pricing import ModelPricingTable
from omnidesk_agent.models.router import ModelRouter


class FailingProvider:
    provider_name = "failing"

    def __init__(self, settings):
        self.settings = settings
        self.model = settings.model
        self.profile_name = settings.profile_name

    async def complete(self, request):
        raise RuntimeError("provider down")


class PassingProvider:
    provider_name = "passing"

    def __init__(self, settings):
        self.settings = settings
        self.model = settings.model
        self.profile_name = settings.profile_name

    async def complete(self, request):
        return ModelResponse(text=f"ok:{request.metadata.get('profile')}", provider=self.provider_name, model=self.model, profile=self.profile_name)


class OpenAIPassingProvider(PassingProvider):
    provider_name = "openai"


class DeepSeekPassingProvider(PassingProvider):
    provider_name = "deepseek"


class DashScopePassingProvider(PassingProvider):
    provider_name = "dashscope"


def test_model_router_falls_back_after_primary_failure(monkeypatch, tmp_path: Path):
    import omnidesk_agent.models.router as router_mod

    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "failing", FailingProvider)
    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "passing", PassingProvider)

    cfg = ModelsConfig(
        default="primary",
        profiles={
            "primary": ModelProfileConfig(provider="failing", model="bad", api_key_env=None),
            "backup": ModelProfileConfig(provider="passing", model="good", api_key_env=None),
        },
        routing={"chat": {"primary": "primary", "fallback": ["backup"], "max_retries": 0}},
    )
    token_budget = TokenBudgetManager(tmp_path / "tokens.sqlite3")
    router = ModelRouter(cfg, token_budget)

    response = asyncio.run(router.complete(ModelRequest(system="s", user="u", task="chat")))

    assert response.text == "ok:backup"
    assert response.profile == "backup"


def test_model_router_opens_circuit_after_failures(monkeypatch, tmp_path: Path):
    import omnidesk_agent.models.router as router_mod

    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "failing", FailingProvider)
    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "passing", PassingProvider)

    cfg = ModelsConfig(
        default="primary",
        profiles={
            "primary": ModelProfileConfig(provider="failing", model="bad", api_key_env=None),
            "backup": ModelProfileConfig(provider="passing", model="good", api_key_env=None),
        },
        routing={"chat": {"primary": "primary", "fallback": ["backup"], "max_retries": 0, "circuit_breaker": {"failure_threshold": 1, "reset_seconds": 60}}},
    )
    token_budget = TokenBudgetManager(tmp_path / "tokens.sqlite3")
    router = ModelRouter(cfg, token_budget)

    first = asyncio.run(router.complete(ModelRequest(system="s", user="u", task="chat", task_id="one")))
    second = asyncio.run(router.complete(ModelRequest(system="s2", user="u2", task="chat", task_id="two")))

    assert first.profile == "backup"
    assert second.profile == "backup"
    assert "primary" in router.status()["circuit"]

class RepairingProvider:
    provider_name = "repairing"

    def __init__(self, settings):
        self.settings = settings
        self.model = settings.model
        self.profile_name = settings.profile_name
        self.calls = []

    async def complete(self, request):
        self.calls.append(request)
        if request.metadata.get("schema_repair"):
            return ModelResponse(text='{"answer":"fixed"}', provider=self.provider_name, model=self.model, profile=self.profile_name, usage={"input_tokens": 2, "output_tokens": 3, "estimated_cost_usd": 0.01})
        return ModelResponse(text='not-json', provider=self.provider_name, model=self.model, profile=self.profile_name, usage={"input_tokens": 1, "output_tokens": 1})


def test_model_router_repairs_invalid_json_and_records_cost(monkeypatch, tmp_path: Path):
    import omnidesk_agent.models.router as router_mod

    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "repairing", RepairingProvider)
    cfg = ModelsConfig(
        default="json",
        profiles={"json": ModelProfileConfig(provider="repairing", model="json-model", api_key_env=None)},
        routing={"chat": {"primary": "json", "fallback": [], "max_retries": 0}},
    )
    token_budget = TokenBudgetManager(tmp_path / "tokens.sqlite3")
    router = ModelRouter(cfg, token_budget)

    response = asyncio.run(router.complete(ModelRequest(
        system="s",
        user="u",
        task="chat",
        json_mode=True,
        task_id="json-task",
        metadata={"json_schema": {"type": "object", "required": ["answer"]}},
    )))

    assert response.text == '{"answer":"fixed"}'
    assert router.cost_ledger.summary(task_id="json-task")["calls"] == 1
    assert router.cost_ledger.summary(task_id="json-task")["estimated_cost"] == 0.01


def test_model_router_records_request_actor_in_cost_store(monkeypatch, tmp_path: Path):
    import omnidesk_agent.models.router as router_mod

    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "passing", PassingProvider)
    cfg = ModelsConfig(
        default="chat",
        profiles={"chat": ModelProfileConfig(provider="passing", model="good", api_key_env=None)},
        routing={"chat": {"primary": "chat", "fallback": [], "max_retries": 0}},
    )
    token_budget = TokenBudgetManager(tmp_path / "tokens.sqlite3")
    cost_store = ModelCostStore(tmp_path / "costs.sqlite3")
    router = ModelRouter(cfg, token_budget, cost_store)

    response = asyncio.run(router.complete(ModelRequest(system="s", user="u", task="chat", task_id="actor-task", metadata={"actor": "alice"})))

    assert response.profile == "chat"
    by_actor = cost_store.summary(days=1, group_by="actor")
    assert by_actor["groups"]["alice"]["calls"] == 1


def test_model_router_uses_server_pricing_for_budget_precheck(monkeypatch, tmp_path: Path):
    import omnidesk_agent.models.router as router_mod

    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "openai", OpenAIPassingProvider)
    cfg = ModelsConfig(
        default="chat",
        profiles={"chat": ModelProfileConfig(provider="openai", model="gpt-5.1", api_key_env=None, max_output_tokens=6000)},
        routing={"chat": {"primary": "chat", "fallback": [], "max_retries": 0}},
    )
    cfg.budget.per_actor_daily_usd_limit = 0.05
    cfg.budget.daily_usd_limit = 10.0
    cfg.budget.monthly_usd_limit = 100.0
    cfg.budget.on_exceed = "block"
    token_budget = TokenBudgetManager(tmp_path / "tokens.sqlite3")
    cost_store = ModelCostStore(tmp_path / "costs.sqlite3")
    router = ModelRouter(cfg, token_budget, cost_store)

    request = ModelRequest(
        system="s",
        user="u",
        task="chat",
        task_id="budget-task",
        metadata={"actor": "alice", "projected_cost_usd": 0.0, "estimated_cost_usd": 0.0},
    )

    try:
        asyncio.run(router.complete(request))
    except RuntimeError as exc:
        assert "model budget exceeded" in str(exc)
        assert "actor model budget exceeded" in str(exc)
    else:
        raise AssertionError("expected server-side projected cost to block the request")


def test_external_provider_pricing_is_nonzero_for_budget_guardrails():
    pricing = ModelPricingTable()

    assert pricing.estimate(provider="deepseek", model="deepseek-v4-pro", input_tokens=1000, output_tokens=1000) > 0
    assert pricing.estimate(provider="deepseek", model="unknown", input_tokens=1000, output_tokens=1000) > 0
    assert pricing.estimate(provider="dashscope", model="qwen-plus", input_tokens=1000, output_tokens=1000) > 0
    assert pricing.estimate(provider="qwen", model="qwen-plus", input_tokens=1000, output_tokens=1000) > 0


def test_live_validation_acl_allows_deepseek_and_dashscope_profiles(tmp_path: Path):
    cfg = ModelsConfig(
        default="fast",
        profiles={
            "fast": ModelProfileConfig(provider="openai", model="gpt-5.1-mini", api_key_env=None),
            "local": ModelProfileConfig(provider="ollama", model="qwen2.5-coder:7b", api_key_env=None, base_url="http://127.0.0.1:11434"),
            "deepseek": ModelProfileConfig(provider="deepseek", model="deepseek-v4-pro", api_key_env=None),
            "qwen_dashscope": ModelProfileConfig(provider="dashscope", model="qwen-plus", api_key_env=None),
        },
        routing={
            "chat": {"primary": "deepseek", "fallback": ["fast", "local"], "max_retries": 0},
            "profile_acl": {
                "roles": {
                    "viewer": ["fast", "local"],
                    "operator": ["fast", "planner", "vision", "local", "deepseek", "qwen_dashscope"],
                    "owner": ["*"],
                },
                "high_cost_profiles": ["code"],
            },
        },
    )
    router = ModelRouter(cfg, TokenBudgetManager(tmp_path / "tokens.sqlite3"))

    assert router.route_plan("chat", {"profile": "deepseek"}).profiles == ["deepseek"]
    assert router.route_plan("chat", {"profile": "qwen_dashscope"}).profiles == ["qwen_dashscope"]


def test_model_router_records_server_estimated_cost_when_provider_omits_cost(monkeypatch, tmp_path: Path):
    import omnidesk_agent.models.router as router_mod

    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "openai", OpenAIPassingProvider)
    cfg = ModelsConfig(
        default="chat",
        profiles={"chat": ModelProfileConfig(provider="openai", model="gpt-5.1-mini", api_key_env=None, max_output_tokens=800)},
        routing={"chat": {"primary": "chat", "fallback": [], "max_retries": 0}},
    )
    token_budget = TokenBudgetManager(tmp_path / "tokens.sqlite3")
    cost_store = ModelCostStore(tmp_path / "costs.sqlite3")
    router = ModelRouter(cfg, token_budget, cost_store)

    response = asyncio.run(router.complete(ModelRequest(system="s", user="hello world", task="chat", task_id="cost-task", metadata={"actor": "alice"})))

    assert response.profile == "chat"
    summary = cost_store.summary(days=1, group_by="actor")
    assert summary["estimated_cost_usd"] > 0
    assert summary["groups"]["alice"]["estimated_cost_usd"] > 0


def test_model_router_requires_cost_store_when_persistent_ledger_is_required(tmp_path: Path):
    cfg = ModelsConfig()
    token_budget = TokenBudgetManager(tmp_path / "tokens.sqlite3")

    try:
        ModelRouter(cfg, token_budget, require_persistent_ledger=True)
    except RuntimeError as exc:
        assert "models.budget.require_persistent_ledger requires a configured model cost_store" in str(exc)
    else:
        raise AssertionError("expected missing persistent model cost store to fail closed")
