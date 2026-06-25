from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnidesk_agent.channels.http_client import ChannelHttpClient, ChannelHttpError, set_channel_offline_mode
from omnidesk_agent.config import ModelProfileConfig, ModelsConfig, load_config
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.router import ModelRouter


class LocalPassingProvider:
    provider_name = "ollama"

    def __init__(self, settings):
        self.settings = settings
        self.model = settings.model
        self.profile_name = settings.profile_name

    async def complete(self, request):
        return ModelResponse(text=f"local:{request.metadata.get('profile')}", provider=self.provider_name, model=self.model, profile=self.profile_name)


class ExternalPassingProvider(LocalPassingProvider):
    provider_name = "openai"


def test_load_config_offline_mode_forces_local_routes(tmp_path: Path):
    config_path = tmp_path / "offline.yaml"
    config_path.write_text(
        """
runtime:
  offline_mode: true
models:
  default: fast
  routing:
    chat: {primary: fast, fallback: [local]}
""",
        encoding="utf-8",
    )

    cfg = load_config(config_path, ensure_dirs=False)

    assert cfg.runtime.offline_mode is True
    assert cfg.models.offline_mode is True
    assert cfg.models.default == "local"
    for task in ("chat", "planner", "code", "upgrade", "summarize"):
        assert cfg.models.routing[task] == {"primary": "local", "fallback": [], "max_retries": 0}
    assert cfg.capabilities.channels.enabled is False
    assert cfg.channels.gmail.enabled is False


def test_model_router_offline_mode_blocks_explicit_external_profile(monkeypatch, tmp_path: Path):
    import omnidesk_agent.models.router as router_mod

    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "ollama", LocalPassingProvider)
    monkeypatch.setitem(router_mod.PROVIDER_CLASSES, "openai", ExternalPassingProvider)
    cfg = ModelsConfig(
        offline_mode=True,
        default="local",
        profiles={
            "local": ModelProfileConfig(provider="ollama", model="qwen2.5-coder:7b", api_key_env=None, base_url="http://127.0.0.1:11434"),
            "fast": ModelProfileConfig(provider="openai", model="gpt", api_key_env=None),
        },
        routing={"chat": {"primary": "fast", "fallback": ["local"], "max_retries": 0}},
    )
    router = ModelRouter(cfg, TokenBudgetManager(tmp_path / "tokens.sqlite3"))

    response = asyncio.run(router.complete(ModelRequest(system="s", user="u", task="chat", task_id="offline-chat")))
    assert response.profile == "local"

    with pytest.raises(RuntimeError, match="offline mode forbids external model profile"):
        asyncio.run(router.complete(ModelRequest(system="s", user="u", task="chat", metadata={"profile": "fast"}, task_id="blocked")))


def test_channel_http_client_blocks_outbound_calls_in_offline_mode():
    set_channel_offline_mode(True)
    try:
        with pytest.raises(ChannelHttpError, match="offline mode forbids outbound"):
            asyncio.run(ChannelHttpClient().post("https://example.invalid", json={}))
    finally:
        set_channel_offline_mode(False)
