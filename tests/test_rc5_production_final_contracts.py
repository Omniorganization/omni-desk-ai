from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest

from omnidesk_agent.config import AppConfig, DEFAULT_SANDBOX_IMAGE, ModelsConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.cost_store import ModelCostStore
from omnidesk_agent.models.router import ModelRouter
from omnidesk_agent.sandbox.runner_server import (
    RunnerConfig,
    _runtime_ready,
    _verify_signature,
    _workspace_from_payload,
)
from omnidesk_agent.validation.production import _is_valid_digest_pinned_image, validate_production_config

VALID_DIGEST = "66f011380d0e49ed280c789fbd08ff0d40968ee7b665575489afa95c98196ab5"
PINNED = "python:3.11-slim@sha256:" + VALID_DIGEST
PLACEHOLDER = "python:3.11-slim@sha256:" + ("0123456789abcdef" * 4)


def test_dockerfiles_use_digest_pinned_default_build_arg():
    assert Path("Dockerfile").read_text().startswith(f"ARG PYTHON_BASE_IMAGE={DEFAULT_SANDBOX_IMAGE}\n")
    assert "0123456789abcdef" not in Path("Dockerfile").read_text()
    assert Path("deploy/sandbox-runner/Dockerfile").read_text().startswith(f"ARG PYTHON_BASE_IMAGE={DEFAULT_SANDBOX_IMAGE}\n")
    assert DEFAULT_SANDBOX_IMAGE in Path("deploy/docker/docker-compose.yml").read_text()
    assert "vars.PYTHON_BASE_IMAGE_DIGEST || env.PYTHON_BASE_IMAGE_DIGEST" in Path(".github/workflows/docker-scan.yml").read_text()


def test_digest_validator_rejects_pattern_placeholder():
    assert not _is_valid_digest_pinned_image(PLACEHOLDER)
    assert _is_valid_digest_pinned_image(PINNED)


def test_runner_hmac_rejects_replayed_nonce(monkeypatch):
    body = json.dumps({"argv": ["pytest"]}).encode()
    ts = str(time.time())
    nonce = "nonce-replay-test"
    secret = "s" * 40
    sig = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + nonce.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = {"x-omnidesk-sandbox-timestamp": ts, "x-omnidesk-sandbox-nonce": nonce, "x-omnidesk-sandbox-signature": sig}
    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET", secret)
    ok, reason = _verify_signature(headers, body, RunnerConfig())
    assert ok is True
    ok, reason = _verify_signature(headers, body, RunnerConfig())
    assert ok is False
    assert "replayed" in reason


def test_runner_workspace_must_be_under_allowed_root(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    inside = root / "job1"
    inside.mkdir()
    cfg = RunnerConfig(allowed_workspace_root=root, allow_workspace_paths=True)
    workspace, tmp = _workspace_from_payload({"workspace": str(inside)}, cfg)
    assert workspace == inside.resolve()
    assert tmp is None
    with pytest.raises(ValueError, match="outside the allowed"):
        _workspace_from_payload({"workspace": str(tmp_path / "outside")}, cfg)


def test_runner_ready_reports_missing_runtime(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    ok, reason = _runtime_ready(RunnerConfig(container_runtime="definitely-missing-runtime"))
    assert ok is False
    assert "not found" in reason


class _BudgetProvider:
    model = "m"
    provider_name = "fake"

    def __init__(self):
        self.settings = type("S", (), {"max_output_tokens": 32})()

    async def complete(self, request):
        return ModelResponse(text="ok", provider="fake", model="m", profile=request.metadata.get("profile", "fast"), usage={"estimated_cost_usd": 1.0})


@pytest.mark.asyncio
async def test_model_router_blocks_when_budget_exceeded(tmp_path):
    cfg = ModelsConfig()
    cfg.budget.daily_usd_limit = 1.0
    cfg.budget.on_exceed = "block"
    store = ModelCostStore(tmp_path / "cost.sqlite3")
    store.record(task_id="old", provider="fake", model="m", profile="fast", task="chat", estimated_cost_usd=1.0)
    router = ModelRouter(cfg, TokenBudgetManager(tmp_path / "tokens.sqlite3"), cost_store=store)
    router.providers = {"fast": _BudgetProvider()}
    with pytest.raises(RuntimeError, match="model budget exceeded"):
        await router.complete(ModelRequest(system="s", user="u", task="chat", metadata={"projected_cost_usd": 0.01}))


def test_production_config_rejects_placeholder_digest_shape():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.sandbox.backend = "remote_docker"
    cfg.sandbox.runner_url = "http://runner"
    cfg.sandbox.docker_image = PLACEHOLDER
    env = {
        "OMNIDESK_ENV": "production",
        "OMNIDESK_ADMIN_TOKEN": "x" * 40,
        "OMNIDESK_GATEWAY_SECRET": "x" * 40,
        "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
        "OMNIDESK_SANDBOX_RUNNER_TOKEN": "x" * 40,
    }
    result = validate_production_config(cfg, env)
    assert "sandbox.docker_image must use a real sha256 digest in production" in result["issues"]
