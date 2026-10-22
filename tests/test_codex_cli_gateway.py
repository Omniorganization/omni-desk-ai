from __future__ import annotations

import asyncio
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
from scripts.verify_codex_cli_chat import checked_stream, isolated_config


class EphemeralTokens(dict):
    def __repr__(self):
        return "<ephemeral Gateway credentials>"


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """Real Gateway/auth/router/storage; only the Codex model is an offline fixture."""
    cfg = isolated_config(
        Path(__file__).resolve().parents[1] / "examples/config.codex-cli.yaml",
        tmp_path,
    )
    calls: list[tuple[str, ModelRequest]] = []
    network_attempts: list[str] = []
    tokens = EphemeralTokens({role: secrets.token_urlsafe(32) for role in ("owner", "operator")})
    for role, token in tokens.items():
        monkeypatch.setenv(getattr(cfg.gateway, f"{role}_token_env"), token)
    for role in ("ADMIN", "VIEWER", "OPERATOR", "OWNER"):
        monkeypatch.delenv(f"OMNIDESK_{role}_ACTOR", raising=False)
    cfg.observability.otlp_endpoint_env = f"OMNIDESK_QA_{uuid4().hex}"

    class OfflineCodexProvider:
        provider_name = "codex_cli"

        def __init__(self, settings):
            self.settings = settings
            self.model = settings.model
            self.profile_name = settings.profile_name

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

    monkeypatch.setitem(PROVIDER_CLASSES, "codex_cli", OfflineCodexProvider)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbid_network)
    monkeypatch.setattr(OmniDeskRuntime, "start", forbid_background)
    app = create_app(cfg)
    try:
        assert app.state.otel_exporter is None
        yield app, cfg, tokens, calls
    finally:
        app.state.runtime.close()
        close_all_open_connections()
        assert network_attempts == []


def headers(tokens, role, *, key=None):
    return {"Authorization": f"Bearer {tokens[role]}", "Idempotency-Key": key or str(uuid4())}


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
def test_anonymous_codex_chat_is_denied_before_model_call(gateway, endpoint):
    app, _cfg, _tokens, calls = gateway

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post(
                endpoint, json={"content": "2+3?", "model_profile": "codex_cli"},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert response.status_code == 403
        assert response.json()["detail"] == "missing or invalid admin token"

    asyncio.run(run())
    assert calls == []
    assert app.state.runtime.model_router.cost_ledger.summary()["calls"] == 0


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("explicit_profile", [True, False], ids=["explicit", "default-route"])
def test_operator_codex_acl_returns_403_without_model_call(gateway, endpoint, explicit_profile):
    app, _cfg, tokens, calls = gateway
    payload = {"content": "2+3?", "role": "owner", "approved_model_profiles": ["codex_cli"]}
    if explicit_profile:
        payload["model_profile"] = "codex_cli"

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
def test_owner_codex_chat_replays_without_another_model_call(gateway, endpoint):
    app, cfg, tokens, calls = gateway
    payload = {"content": "2+3?", "model_profile": "codex_cli"}
    request_headers = headers(tokens, "owner")
    model = cfg.models.profiles["codex_cli"].model
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
                assert answer["model_provider"] == "codex_cli"
                assert answer["model_name"] == model
                assert answer["model_profile"] == "codex_cli"
            else:
                assert checked_stream(first, "5", model)["native_stream"] is False
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
