from __future__ import annotations

import asyncio

import pytest

from omnidesk_agent.config import AppConfig, SandboxConfig
from omnidesk_agent.channels.provider_errors import classify_channel_error
from omnidesk_agent.core.outbound_dispatcher import OutboundDispatcher
from omnidesk_agent.core.outbound_messages import OutboundMessageStore
from omnidesk_agent.models.cost_store import ModelCostStore
from omnidesk_agent.models.schema_retry import StructuredOutputError, validate_json_text
from omnidesk_agent.tools.shell import ShellTool
from omnidesk_agent.validation.production import validate_production_config


PINNED = "python:3.11-slim@sha256:" + '66f011380d0e49ed280c789fbd08ff0d40968ee7b665575489afa95c98196ab5'


def test_json_schema_retry_uses_full_nested_schema_validation():
    schema = {
        "type": "object",
        "required": ["action", "items"],
        "properties": {
            "action": {"enum": ["send", "queue"]},
            "items": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string", "pattern": "^[a-z]+$"}}}},
        },
        "additionalProperties": False,
    }
    assert validate_json_text('{"action":"send","items":[{"id":"abc"}]}', schema)["action"] == "send"
    with pytest.raises(StructuredOutputError):
        validate_json_text('{"action":"delete","items":[{"id":"ABC"}],"extra":1}', schema)


def test_model_cost_store_persists_and_groups(tmp_path):
    store = ModelCostStore(tmp_path / "costs.sqlite3")
    store.record(task_id="t1", run_id="r1", actor="alice", provider="openai", model="m", profile="fast", task="chat", input_tokens=10, output_tokens=5, estimated_cost_usd=0.25)
    reopened = ModelCostStore(tmp_path / "costs.sqlite3")
    summary = reopened.summary(days=7, group_by="provider")
    assert summary["calls"] == 1
    assert summary["estimated_cost_usd"] == 0.25
    assert summary["groups"]["openai"]["output_tokens"] == 5


def test_outbound_dispatcher_classifies_non_retryable_channel_errors(tmp_path):
    class AuthErrorAdapter:
        async def send_text(self, recipient: str, text: str, **kwargs):
            exc = RuntimeError("401 unauthorized")
            exc.status_code = 401
            raise exc

    async def run_case():
        store = OutboundMessageStore(tmp_path / "outbound.sqlite3", max_retries=5, base_retry_seconds=1)
        dispatcher = OutboundDispatcher(store, {"telegram": AuthErrorAdapter()})
        msg_id = store.create(channel="telegram", recipient="r", payload={"type": "text", "text": "hello"})
        assert await dispatcher.run_once() is True
        row = store.get(msg_id)
        assert row["status"] == "dead_letter"
        assert row["error_category"] == "auth_error"

    asyncio.run(run_case())
    assert classify_channel_error(RuntimeError("session window policy violation")).category == "policy_error"


def test_production_requires_remote_runner_or_digest_pinned_local_docker(monkeypatch):
    cfg = AppConfig()
    cfg.plugins.enabled = False
    cfg.channels.chrome.enabled = False
    cfg.memory_privacy.encrypt_at_rest = True
    cfg.storage.backend = "postgres"
    cfg.app_sync.backend = "postgres"
    cfg.api_resource_guard.backend = "postgres"
    cfg.sandbox.backend = "remote_docker"
    cfg.sandbox.runner_url = "http://sandbox-runner:18890"
    cfg.sandbox.docker_image = PINNED
    env = {
        "OMNIDESK_ENV": "production",
        "OMNIDESK_ADMIN_TOKEN": "x" * 40,
        "OMNIDESK_GATEWAY_SECRET": "x" * 40,
        "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
        "OMNIDESK_SANDBOX_RUNNER_TOKEN": "x" * 40,
        "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET": "x" * 40,
        "OMNIDESK_POSTGRES_DSN": "postgresql://user:pass@db/omnidesk",
        "OMNIDESK_APPSYNC_POSTGRES_DSN": "postgresql://user:pass@db/omnidesk",
    }
    assert validate_production_config(cfg, env)["ok"] is True
    cfg.sandbox.runner_url = None
    result = validate_production_config(cfg, env)
    assert "sandbox.runner_url must be configured when sandbox.backend=remote_docker" in result["issues"]
    cfg.sandbox.backend = "docker"
    cfg.sandbox.runner_url = None
    cfg.sandbox.docker_image = "python:3.11-slim"
    result = validate_production_config(cfg, env)
    assert "sandbox.docker_image must use a real sha256 digest in production" in result["issues"]


def test_shell_remote_docker_backend_uses_runner(monkeypatch, tmp_path):
    from omnidesk_agent.core.models import ToolResult
    from omnidesk_agent.tools.base import ToolContext
    from omnidesk_agent.sandbox.remote_runner import RemoteSandboxResult

    class Permissive:
        def verify(self, proposal):
            return None

    async def fake_run(self, *, argv, workspace, timeout_seconds, readonly=True):
        assert argv == ["git", "status"]
        assert readonly is True
        return RemoteSandboxResult(ok=True, exit_code=0, stdout="ok", stderr="")

    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_TOKEN", "x" * 40)
    monkeypatch.setattr("omnidesk_agent.sandbox.remote_runner.RemoteSandboxClient.run_command", fake_run)
    sandbox = SandboxConfig(backend="remote_docker", runner_url="http://runner")
    tool = ShellTool(tmp_path, AppConfig().permissions, sandbox)

    async def run_case():
        result = await tool.call("run", {"argv": ["git", "status"]}, ToolContext(source="test", actor="a", permissions=Permissive()))
        assert isinstance(result, ToolResult)
        assert result.ok is True
        assert result.data["backend"] == "remote_docker"

    asyncio.run(run_case())
