from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.models.base import ModelDelta, ModelRequest, ModelResponse
from omnidesk_agent.models.providers import PROVIDER_CLASSES
from omnidesk_agent.server import create_app
from omnidesk_agent.storage.sqlite import close_all_open_connections
from scripts.verify_codex_cli_chat import isolated_config


class EphemeralTokens(dict):
    def __repr__(self):
        return "<ephemeral Gateway credentials>"


@pytest.fixture(params=["codex_cli", "claude_code_cli"])
def gateway(request, tmp_path, monkeypatch):
    """Real Gateway/auth/router/storage; only the subscription model is an offline fixture."""
    provider_name = request.param
    cfg = isolated_config(
        Path(__file__).resolve().parents[1] / "examples/config.codex-cli.yaml",
        tmp_path,
    )
    profile = cfg.models.profiles.pop("codex_cli")
    profile.provider = provider_name
    cfg.models.profiles[provider_name] = profile
    cfg.llm.provider = cfg.models.default = provider_name
    cfg.models.routing["chat"]["primary"] = provider_name
    cfg.models.routing["profile_acl"]["high_cost_profiles"] = [provider_name]
    calls: list[tuple[str, ModelRequest]] = []
    network_attempts: list[str] = []
    tokens = EphemeralTokens({role: secrets.token_urlsafe(32) for role in ("owner", "operator")})
    for role, token in tokens.items():
        monkeypatch.setenv(getattr(cfg.gateway, f"{role}_token_env"), token)
    for role in ("ADMIN", "VIEWER", "OPERATOR", "OWNER"):
        monkeypatch.delenv(f"OMNIDESK_{role}_ACTOR", raising=False)
    cfg.observability.otlp_endpoint_env = f"OMNIDESK_QA_{uuid4().hex}"

    class OfflineSubscriptionProvider:
        def __init__(self, settings):
            self.settings = settings
            self.model = settings.model
            self.profile_name = settings.profile_name
            self.provider_name = settings.provider

        async def complete(self, request):
            calls.append(("complete", request))
            return ModelResponse(
                text="5", provider=self.provider_name, model=self.model,
                profile=self.profile_name, usage={"input_tokens": 4, "output_tokens": 1},
            )

        async def stream(self, request):
            calls.append(("stream", request))
            yield ModelDelta(
                sequence=1, text="5", provider=self.provider_name,
                model=self.model, profile=self.profile_name, native=False,
            )
            yield ModelDelta(
                sequence=2, provider=self.provider_name, model=self.model,
                profile=self.profile_name, native=False, finish_reason="completed",
                usage={"input_tokens": 4, "output_tokens": 1},
            )

    async def forbid_network(_transport, request):
        network_attempts.append(str(request.url))
        raise AssertionError("Gateway tests must never make outbound HTTP requests")

    async def forbid_background(_runtime):
        raise AssertionError("Gateway tests must never start background services")

    def forbid_cache(*_args, **_kwargs):
        raise AssertionError("Subscription Gateway tests must bypass the response cache")

    monkeypatch.setitem(PROVIDER_CLASSES, provider_name, OfflineSubscriptionProvider)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbid_network)
    monkeypatch.setattr(OmniDeskRuntime, "start", forbid_background)
    app = create_app(cfg)
    monkeypatch.setattr(app.state.runtime.token_budget, "get_cached", forbid_cache)
    monkeypatch.setattr(app.state.runtime.token_budget, "put_cached", forbid_cache)
    try:
        assert app.state.otel_exporter is None
        assert cfg.models.budget.require_persistent_ledger is True
        assert app.state.runtime.model_router.cost_ledger.store is not None
        yield app, cfg, tokens, calls
    finally:
        app.state.runtime.close()
        close_all_open_connections()
        assert network_attempts == []


def headers(tokens, role, *, key=None):
    return {"Authorization": f"Bearer {tokens[role]}", "Idempotency-Key": key or str(uuid4())}


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("explicit_profile", [True, False], ids=["explicit", "default-route"])
def test_anonymous_subscription_chat_is_denied_before_model_call(gateway, endpoint, explicit_profile):
    app, cfg, _tokens, calls = gateway
    payload = {"content": "2+3?"}
    if explicit_profile:
        payload["model_profile"] = cfg.models.default

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post(
                endpoint, json=payload,
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert response.status_code == 403
        assert response.json()["detail"] == "missing or invalid admin token"

    asyncio.run(run())
    assert calls == []
    assert app.state.runtime.model_router.cost_ledger.summary()["calls"] == 0


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("explicit_profile", [True, False], ids=["explicit", "default-route"])
def test_operator_subscription_acl_returns_403_without_model_call(gateway, endpoint, explicit_profile):
    app, cfg, tokens, calls = gateway
    profile = cfg.models.default
    payload = {"content": "2+3?", "role": "owner", "approved_model_profiles": [profile]}
    if explicit_profile:
        payload["model_profile"] = profile

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post(
                endpoint, json=payload,
                headers={**headers(tokens, "operator"), "X-OmniDesk-Admin-Role": "owner", "X-OmniDesk-Actor": "owner"},
            )
        assert response.status_code == 403
        assert "text/event-stream" not in response.headers["content-type"]
        if endpoint == "/api/chat":
            assert response.json()["detail"]["code"] == "model_profile_forbidden"
        else:
            assert "model profile denied by ACL" in response.json()["detail"]

    asyncio.run(run())
    assert calls == []
    assert app.state.runtime.model_router.cost_ledger.summary()["calls"] == 0


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("explicit_profile", [True, False], ids=["explicit", "default-route"])
def test_owner_subscription_chat_replays_without_another_model_call(gateway, endpoint, explicit_profile):
    app, cfg, tokens, calls = gateway
    profile = cfg.models.default
    payload = {"content": "2+3?"}
    if explicit_profile:
        payload["model_profile"] = profile
    request_headers = headers(tokens, "owner")
    model = cfg.models.profiles[profile].model
    assert model == "gpt-6.1-sol"

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            first = await client.post(endpoint, json=payload, headers=request_headers)
            if endpoint == "/api/chat":
                assert first.status_code == 200
                result = first.json()
                assert result["ok"] is True
                answer = result["assistant_message"]
                assert answer["content"] == "5"
                assert answer["model_provider"] == profile
                assert answer["model_name"] == model
                assert answer["model_profile"] == profile
            else:
                assert first.status_code == 200
                events = []
                for block in first.text.split("\n\n"):
                    name = next((line[7:] for line in block.splitlines() if line.startswith("event: ")), None)
                    data = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), None)
                    if name and data:
                        events.append((name, json.loads(data)))
                assert not any(name == "chat.failed" for name, _ in events)
                assert sum(name == "chat.completed" for name, _ in events) == 1
                deltas = [data for name, data in events if name == "chat.delta"]
                assert "".join(data["text"] for data in deltas) == "5"
                assert all(data["provider"] == profile and data["model"] == model for data in deltas)
                assert all(data["native"] is False for data in deltas)
            assert len(calls) == 1
            assert calls[0][1].metadata["role"] == "owner"
            assert calls[0][1].metadata["actor"] == f"token:{cfg.gateway.owner_token_env}"
            ledger = app.state.runtime.model_router.cost_ledger.summary()
            assert ledger["calls"] == ledger["durable"]["calls"] == 1
            replay = await client.post(endpoint, json=payload, headers=request_headers)
        assert replay.status_code == 200
        assert replay.content == first.content

    asyncio.run(run())
    assert len(calls) == 1
    ledger = app.state.runtime.model_router.cost_ledger.summary()
    assert ledger["calls"] == ledger["durable"]["calls"] == 1
    assert ledger["durable"]["cache_hits"] == 0
