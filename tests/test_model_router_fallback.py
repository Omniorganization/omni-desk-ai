from __future__ import annotations

import asyncio
from pathlib import Path

from omnidesk_agent.config import ModelProfileConfig, ModelsConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.cost_store import ModelCostStore
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
