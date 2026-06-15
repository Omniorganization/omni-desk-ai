from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.observability_otel import AsyncOTLPHttpExporter, span_record_from_context
from omnidesk_agent.plugins.docker_runner import RemoteDockerPluginTool
from omnidesk_agent.plugins.registry import PluginRegistry
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.break_glass import BreakGlassStore
from omnidesk_agent.security.dual_approval import DualApprovalStore
from omnidesk_agent.server_routes.agent_routes import register_break_glass_routes
from omnidesk_agent.validation.production import validate_production_config


class Tools:
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        self.tools[tool.name] = tool


def _signed_plugin(root: Path, name: str, sandbox: str, secret: str):
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    entrypoint = plugin_dir / "plugin.py"
    entrypoint.write_text("def call(action, args):\n    return {'ok': True}\n", encoding="utf-8")
    digest = hashlib.sha256(entrypoint.read_bytes()).hexdigest()
    signature = hmac.new(secret.encode("utf-8"), digest.encode("utf-8"), hashlib.sha256).hexdigest()
    (plugin_dir / "plugin.yaml").write_text(
        "\n".join([
            f"name: {name}",
            "version: 1.0.0",
            "enabled: true",
            "trusted: true",
            f"sandbox: {sandbox}",
            "entrypoint: plugin.py",
            "permissions: [plugin.call]",
            f"sha256: {digest}",
            f"signature: {signature}",
        ]),
        encoding="utf-8",
    )


def test_dual_approval_is_enforced_before_approve_and_consume(tmp_path: Path) -> None:
    dual = DualApprovalStore(tmp_path / "dual.sqlite3")
    approvals = ApprovalStore(tmp_path / "approvals.sqlite3", dual_approval_store=dual)
    approval_id = approvals.create({"tool": "shell", "risk": "critical", "requires_dual_approval": True, "scope_hash": "abc"})

    with pytest.raises(PermissionError, match="dual approval is not satisfied"):
        approvals.decide(approval_id, "approved", {"actor": "owner-a"})

    first = dual.approve(approval_id, "owner-a")
    assert first.ready is False
    with pytest.raises(PermissionError, match="distinct"):
        dual.approve(approval_id, "owner-a")

    second = dual.approve(approval_id, "owner-b")
    assert second.ready is True
    approved = approvals.decide(approval_id, "approved", {"actor": "owner-b"})
    assert approved["status"] == "approved"
    consumed = approvals.consume_approved(approval_id, {"scope_hash": "abc"}, consumed_by_run_id="run-1")
    assert consumed["status"] == "consumed"


def test_break_glass_routes_require_enabled_policy_and_distinct_approver(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.permissions.break_glass_enabled = True

    class RT:
        break_glass_store = BreakGlassStore(tmp_path / "break.sqlite3", audit_log=tmp_path / "audit.jsonl")

    async def admin(_request: Request, _role: str = "viewer") -> None:
        return None

    app = FastAPI()
    register_break_glass_routes(app, cfg, RT(), admin)
    client = TestClient(app)

    bad = client.post("/admin/break-glass/open", json={"actor": "owner", "approved_by": "owner", "reason": "outage"})
    assert bad.status_code == 400
    opened = client.post("/admin/break-glass/open", json={"actor": "operator", "approved_by": "owner", "reason": "restore", "ttl_seconds": 60})
    assert opened.status_code == 200
    session_id = opened.json()["session"]["session_id"]
    assert client.get(f"/admin/break-glass/status/{session_id}").json()["session"]["active"] is True
    revoked = client.post(f"/admin/break-glass/revoke/{session_id}", json={"revoked_by": "owner"})
    assert revoked.status_code == 200
    assert client.get(f"/admin/break-glass/status/{session_id}").json()["session"]["active"] is False


def test_remote_docker_plugin_is_selected_when_runtime_sandbox_is_remote(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OMNIDESK_PLUGIN_SIGNING_SECRET", "secret")
    _signed_plugin(tmp_path, "remote_echo", "docker", "secret")
    cfg = AppConfig()
    cfg.sandbox.backend = "remote_docker"
    cfg.sandbox.runner_url = "http://sandbox-runner:18890"
    tools = Tools()

    result = PluginRegistry(tmp_path).load_into(tools, cfg)

    assert result == {"remote_echo": ["remote_echo"]}
    assert isinstance(tools.tools["remote_echo"], RemoteDockerPluginTool)
    assert tools.tools["remote_echo"].runner_url == "http://sandbox-runner:18890"


def test_production_example_config_passes_with_required_env(monkeypatch) -> None:
    import omnidesk_agent.config as config_mod
    if config_mod.yaml is None:
        pytest.skip("PyYAML is not available in this test environment")
    from omnidesk_agent.config import load_config

    env = {
        "OMNIDESK_ENV": "production",
        "OMNIDESK_ADMIN_TOKEN": "x" * 40,
        "OMNIDESK_GATEWAY_SECRET": "x" * 40,
        "OMNIDESK_PLUGIN_SIGNING_SECRET": "x" * 40,
        "OMNIDESK_MEMORY_ENCRYPTION_KEY": "x" * 40,
        "OMNIDESK_SANDBOX_RUNNER_TOKEN": "x" * 40,
        "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET": "x" * 40,
        "OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY": "x" * 40,
        "OMNIDESK_POSTGRES_DSN": "postgresql://omnidesk:secret@postgres:5432/omnidesk",
    }
    cfg = load_config("deploy/docker/config.production.example.yaml")
    result = validate_production_config(cfg, env)
    assert result["ok"] is True, result["issues"]


def test_async_otlp_exporter_enqueues_without_blocking() -> None:
    exporter = AsyncOTLPHttpExporter(endpoint="http://127.0.0.1:1/v1/traces", timeout=0.01, max_queue_size=1)
    span = span_record_from_context("test.span", "a" * 32, "b" * 16, None, 1.0, 2.0, "OK", {"run_id": "r1"})
    assert exporter.export(span) is True
    exporter.close()
