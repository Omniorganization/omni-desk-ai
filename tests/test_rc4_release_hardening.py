from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnidesk_agent.config import AppConfig, SandboxConfig
from omnidesk_agent.models.budget_policy import ModelBudgetEnforcer, ModelBudgetPolicy
from omnidesk_agent.models.cost_store import ModelCostStore
from omnidesk_agent.sandbox.runner_server import RunnerConfig, _verify_signature
from omnidesk_agent.self_upgrade.governance import GovernedSelfImprovement
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner
from omnidesk_agent.validation.production import _is_valid_digest_pinned_image, validate_production_config

REAL_DIGEST = '66f011380d0e49ed280c789fbd08ff0d40968ee7b665575489afa95c98196ab5'
PINNED = "python:3.11-slim@sha256:" + REAL_DIGEST


def _prod_env():
    return {
        "OMNIDESK_ENV": "production",
        "OMNIDESK_ADMIN_TOKEN": "x" * 40,
        "OMNIDESK_GATEWAY_SECRET": "x" * 40,
        "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
        "OMNIDESK_SANDBOX_RUNNER_TOKEN": "x" * 40,
    }


def test_digest_pin_validator_rejects_placeholder_and_accepts_real_shape():
    assert not _is_valid_digest_pinned_image("python:3.11-slim")
    assert not _is_valid_digest_pinned_image("python:3.11-slim@sha256:" + "0" * 64)
    assert not _is_valid_digest_pinned_image("python:3.11-slim@sha256:" + "a" * 64)
    assert not _is_valid_digest_pinned_image("python:3.11-slim@sha256:" + "g" * 64)
    assert _is_valid_digest_pinned_image(PINNED)


def test_production_rejects_zero_digest_and_shell_backend_mismatch():
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.sandbox.backend = "remote_docker"
    cfg.sandbox.runner_url = "http://runner"
    cfg.sandbox.docker_image = "python:3.11-slim@sha256:" + "0" * 64
    result = validate_production_config(cfg, _prod_env())
    assert "sandbox.docker_image must use a real sha256 digest in production" in result["issues"]
    cfg.sandbox.docker_image = PINNED
    cfg.permissions.shell_backend = "docker"
    result = validate_production_config(cfg, _prod_env())
    assert "permissions.shell_backend must match sandbox.backend when both are configured" in result["issues"]


def test_self_upgrade_wires_runtime_sandbox_config(tmp_path):
    sandbox = SandboxConfig(backend="remote_docker", runner_url="http://runner", docker_image=PINNED)
    gov = GovernedSelfImprovement(tmp_path, tmp_path, sandbox_cfg=sandbox)
    assert gov.regression_runner.runner.backend == "remote_docker"
    assert gov.security_runner.runner.backend == "remote_docker"
    assert gov.regression_runner.runner.sandbox_cfg is sandbox


@pytest.mark.asyncio
async def test_self_upgrade_sandbox_runner_supports_remote_docker(monkeypatch, tmp_path):
    from omnidesk_agent.sandbox.remote_runner import RemoteSandboxResult

    async def fake_run(self, *, argv, workspace, timeout_seconds, readonly=True):
        assert argv == ["pytest", "tests"]
        assert readonly is True
        return RemoteSandboxResult(ok=True, exit_code=0, stdout="ok")

    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_TOKEN", "x" * 40)
    monkeypatch.setattr("omnidesk_agent.sandbox.remote_runner.RemoteSandboxClient.run_command", fake_run)
    runner = SandboxRunner(tmp_path, sandbox_cfg=SandboxConfig(backend="remote_docker", runner_url="http://runner"))
    result = await runner.run(["pytest", "tests"])
    assert result.ok is True
    assert result.command.startswith("remote_docker")


def test_runner_hmac_signature_validation(monkeypatch):
    import hashlib
    import hmac
    import time

    body = json.dumps({"argv": ["pytest"]}).encode()
    ts = str(time.time())
    nonce = "n"
    secret = "s" * 40
    sig = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + nonce.encode() + b"." + body, hashlib.sha256).hexdigest()
    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET", secret)
    ok, reason = _verify_signature({"x-omnidesk-sandbox-timestamp": ts, "x-omnidesk-sandbox-nonce": nonce, "x-omnidesk-sandbox-signature": sig}, body, RunnerConfig())
    assert ok is True
    ok, reason = _verify_signature({"x-omnidesk-sandbox-timestamp": ts, "x-omnidesk-sandbox-nonce": nonce, "x-omnidesk-sandbox-signature": "sha256:bad"}, body, RunnerConfig())
    assert ok is False


def test_model_budget_enforcer_blocks_actor_over_budget(tmp_path):
    store = ModelCostStore(tmp_path / "cost.sqlite3")
    store.record(task_id="t", run_id="r", actor="alice", provider="openai", model="m", profile="fast", task="chat", input_tokens=1, output_tokens=1, estimated_cost_usd=2.0)
    decision = ModelBudgetEnforcer(store, ModelBudgetPolicy(per_actor_daily_usd_limit=2.5, on_exceed="block")).check(actor="alice", projected_cost_usd=1.0)
    assert decision.ok is False
    assert decision.action == "block"


def test_config_production_file_is_example_only():
    assert Path("deploy/docker/config.production.example.yaml").exists()
    assert not Path("deploy/docker/config.production.yaml").exists()
