from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from omnidesk_agent.config import AppConfig, ApiResourceGuardConfig, ChromeConfig, PermissionConfig, SandboxConfig, UIBridgeConfig
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.observability.metrics import MetricsRegistry
from omnidesk_agent.security.chat_resource_guard import ChatAwareApiResourceGuard
from omnidesk_agent.security.permissions import PermissionDecision, PermissionManager
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.resource_guard import ApiResourceGuard
from omnidesk_agent.server_routes.webhook_guard import WebhookGuard
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.tools.computer import ComputerTool
from omnidesk_agent.tools.files import FilesTool
from omnidesk_agent.tools.gmail_tool import GmailTool
from omnidesk_agent.tools.pr_tool import PullRequestTool
from omnidesk_agent.tools.shell import ShellTool
from omnidesk_agent.tools.ui_bridge_tool import UIBridgeTool
from omnidesk_agent.tools.vision import VisionGroundingTool
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.tools.test_tool import TestTool
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


def test_next_dependency_cannot_restore_known_vulnerable_versions():
    root = Path(__file__).resolve().parents[1] / "apps/web-admin-next"
    manifest = json.loads((root / "package.json").read_text())
    lock = json.loads((root / "package-lock.json").read_text())
    assert manifest["dependencies"]["next"] == "^16.3.6"
    assert lock["packages"][""]["dependencies"]["next"] == "^16.3.6"
    for name, metadata in lock["packages"].items():
        if name == "node_modules/next" or name.startswith("node_modules/@next/"):
            assert tuple(int(part) for part in metadata["version"].split(".")) >= (16, 3, 6)


@pytest.mark.asyncio
async def test_oauth_callback_proposal_contains_only_secret_digests(tmp_path):
    store = ApprovalStore(tmp_path / "approvals.sqlite3")
    permissions = PermissionManager(PermissionConfig(approval_mode="remote_approval", audit_log=tmp_path / "audit.jsonl"), store)
    exchange = Mock(return_value={"access_token": "test-token"})
    tool = GmailTool(SimpleNamespace(cfg=SimpleNamespace(enabled=True), oauth=SimpleNamespace(exchange_code=exchange)))
    ctx = ToolContext(permissions=permissions, actor="actor-a")
    args = {"code": "one-time-private-code", "state": "one-time-private-state", "redirect_uri": "http://localhost/callback"}
    with pytest.raises(ApprovalRequired) as exc:
        await tool.call("auth_callback", args, ctx)
    exchange.assert_not_called()
    pending = store.get(exc.value.approval_id)
    assert pending is not None
    persisted = json.dumps(pending) + json.dumps(exc.value.proposal) + permissions.audit_log.read_text()
    assert args["code"] not in persisted
    assert args["state"] not in persisted
    assert exc.value.proposal["args"]["state_sha256"] == hashlib.sha256(args["state"].encode()).hexdigest()
    assert exc.value.proposal["args"]["code_sha256"] == hashlib.sha256(args["code"].encode()).hexdigest()
    permissions.allow_approved_proposal(exc.value.proposal)
    for key in ("state", "code"):
        with pytest.raises(ApprovalRequired) as changed:
            await tool.call("auth_callback", {**args, key: args[key] + "-changed"}, ctx)
        assert changed.value.proposal["scope_hash"] != exc.value.proposal["scope_hash"]
    exchange.assert_not_called()
    assert (await tool.call("auth_callback", args, ctx)).ok
    exchange.assert_called_once_with(args["code"], args["redirect_uri"], args["state"], actor="actor-a")


def request(chunks: list[bytes], *, method="POST", headers=None, receive=None) -> Request:
    index = 0

    async def receive_chunk():
        nonlocal index
        chunk = chunks[index]
        index += 1
        return {"type": "http.request", "body": chunk, "more_body": index < len(chunks)}

    req = Request({"type": "http", "method": method, "scheme": "http", "path": "/api/chat/stream", "server": ("test", 80), "client": ("192.0.2.1", 1), "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}, receive or receive_chunk)
    req.state.received_count = lambda: index
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_type", [ApiResourceGuard, ChatAwareApiResourceGuard])
@pytest.mark.parametrize("method", ["POST", "GET", "HEAD", "OPTIONS"])
async def test_chunked_body_stops_at_limit_and_releases(guard_type, method):
    guard = guard_type(ApiResourceGuardConfig(max_body_bytes=3))
    req = request([b"ab", b"cd", b"unread"], method=method, headers={"content-length": "1"})
    with pytest.raises(HTTPException) as exc:
        await guard.before_request(req)
    assert exc.value.status_code == 413
    assert req.state.received_count() == 2
    assert guard.snapshot().inflight_total == 0
    valid = request([b"a", b"bc"])
    release = await guard.before_request(valid)
    assert await valid.body() == b"abc"
    release()
    release()
    assert guard.snapshot().inflight_total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_type", [ApiResourceGuard, ChatAwareApiResourceGuard])
async def test_admission_precedes_body_reads(guard_type):
    guard = guard_type(ApiResourceGuardConfig(max_requests_per_ip=0))
    unread = AsyncMock(side_effect=AssertionError("must not receive"))
    with pytest.raises(HTTPException) as exc:
        await guard.before_request(request([], receive=unread))
    assert exc.value.status_code == 429
    unread.assert_not_called()
    guard = guard_type(ApiResourceGuardConfig(max_inflight_requests=0))
    with pytest.raises(HTTPException) as exc:
        await guard.before_request(request([], receive=unread))
    assert exc.value.status_code == 429
    unread.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_type", [ApiResourceGuard, ChatAwareApiResourceGuard])
async def test_body_timeout_and_cancellation_release_admission(guard_type):
    async def slow():
        await asyncio.Event().wait()

    guard = guard_type(ApiResourceGuardConfig(body_read_timeout_seconds=0.01))
    with pytest.raises(HTTPException) as exc:
        await guard.before_request(request([], receive=slow))
    assert exc.value.status_code == 408
    assert guard.snapshot().inflight_total == 0

    async def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await guard.before_request(request([], receive=cancelled))
    assert guard.snapshot().inflight_total == 0
    req = request([b'{"ok":', b'true}'])
    release = await guard.before_request(req)
    assert await req.json() == {"ok": True}
    release()


def test_metrics_are_bounded_without_losing_cumulative_histograms():
    metrics = MetricsRegistry(max_series=8, max_samples_per_series=4)
    for _ in range(1000):
        metrics.observe("duration", 1, path="/health")
    assert metrics.histograms['duration{path="/health"}'] == [1] * 4
    snapshot = metrics.snapshot()
    assert snapshot["histogram_totals"]['duration{path="/health"}']["count"] == 1000
    rendered = metrics.render_prometheus()
    assert 'duration_count{path="/health"} 1000' in rendered
    assert 'duration_sum{path="/health"} 1000.0' in rendered
    assert 'duration_bucket{le="1.0",path="/health"} 1000' in rendered
    for index in range(1000):
        metrics.inc("requests", path=f"/{index}")
        metrics.set("latency", index, path=f"/{index}")
        metrics.observe("duration", index, path=f"/{index}")
    assert len(metrics.counters) + len(metrics.gauges) + len(metrics.histograms) == 8
    assert metrics.dropped_series_updates > 0
    metrics.observe("duration", float("nan"))
    metrics.inc("oversized", label="x" * 3000)
    assert not any(key.startswith("oversized") for key in metrics.counters)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["telegram", "whatsapp", "whatsapp_cloud", "wechat", "wechat_official", "teams", "microsoft_teams", "unknown"])
@pytest.mark.parametrize("signatures", [True, False])
async def test_disabled_webhook_never_reads_or_calls_adapter(channel, signatures):
    cfg = AppConfig()
    cfg.gateway.require_webhook_signatures = signatures
    runtime = SimpleNamespace(webhook_security=Mock())
    adapter = Mock()
    req = SimpleNamespace(body=AsyncMock(side_effect=AssertionError("must not read disabled body")))
    with pytest.raises(HTTPException) as exc:
        await WebhookGuard(cfg, runtime).guard(channel, adapter, req)
    assert exc.value.status_code == 403
    req.body.assert_not_called()
    assert not adapter.mock_calls
    assert not runtime.webhook_security.mock_calls


@pytest.mark.asyncio
async def test_enabled_webhook_preserves_authentication_and_replay_guard():
    cfg = AppConfig()
    cfg.channels.telegram.enabled = True
    runtime = SimpleNamespace(webhook_security=Mock())
    adapter = Mock()
    adapter.extract_envelope.return_value = SimpleNamespace(source_key="u", message_id="m", timestamp=None)
    req = SimpleNamespace(body=AsyncMock(return_value=b'{}'), headers={}, query_params={})
    guard = WebhookGuard(cfg, runtime)
    adapter.verify_request.side_effect = PermissionError("invalid signature")
    with pytest.raises(HTTPException):
        await guard.guard("telegram", adapter, req)
    runtime.webhook_security.guard.assert_not_called()
    adapter.verify_request.side_effect = None
    body, _ = await guard.guard("telegram", adapter, req)
    assert body == b'{}'
    runtime.webhook_security.guard.assert_called_once()


@pytest.mark.asyncio
async def test_file_capability_precedes_permissions_and_preserves_reads(tmp_path):
    permissions = Mock()
    permissions.verify.return_value = PermissionDecision(True)
    ctx = ToolContext(permissions=permissions)
    tool = FilesTool(tmp_path)
    assert "write_text" not in tool.spec().actions
    assert "files.write" not in tool.spec().permissions
    with pytest.raises(PermissionError):
        await tool.call("write_text", {"path": "new/file", "text": "bad"}, ctx)
    permissions.verify.assert_not_called()
    assert not (tmp_path / "new").exists()
    writable = FilesTool(tmp_path, allow_write=True)
    assert (await writable.call("write_text", {"path": "file", "text": "good"}, ctx)).ok
    assert (await tool.call("read_text", {"path": "file"}, ctx)).data["text"] == "good"
    assert (await tool.call("list", {}, ctx)).ok


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["files", "shell", "browser", "gmail", "oauth", "pr", "ui", "vision"])
@pytest.mark.parametrize("mode", ["dry_run", "deny"])
async def test_direct_tools_do_not_execute_false_decisions(kind, mode, tmp_path):
    perms = Mock()
    perms.verify.return_value = PermissionDecision(False, mode)
    ctx = ToolContext(permissions=perms)
    tripwire = AsyncMock(side_effect=AssertionError("unexpected effect"))
    if kind == "files":
        tool, action, args = FilesTool(tmp_path, allow_write=True), "write_text", {"path": "bad", "text": "bad"}
    elif kind == "shell":
        tool = ShellTool(tmp_path, PermissionConfig(shell_backend="argv"))
        tool._allowed = lambda argv: True
        tool._runtime_backend_allowed = lambda argv: (True, "")
        action, args = "run", {"argv": ["echo", "bad"]}
    elif kind == "browser":
        tool = BrowserTool(ChromeConfig(enabled=True, allowed_origins=["https://allowed.test"]))
        tool._tabs = tripwire
        action, args = "list_tabs", {}
    elif kind in {"gmail", "oauth"}:
        adapter = SimpleNamespace(cfg=SimpleNamespace(enabled=True, readonly=False, allow_send=True), send_email=tripwire, oauth=SimpleNamespace(exchange_code=Mock(side_effect=AssertionError("unexpected exchange"))))
        tool = GmailTool(adapter)
        action, args = ("send_email", {"to": "user@test", "body": "bad"}) if kind == "gmail" else ("auth_callback", {"code": "code", "redirect_uri": "http://localhost", "state": "state"})
    elif kind == "pr":
        tool = PullRequestTool(tmp_path)
        tool._run = Mock(side_effect=AssertionError("unexpected process"))
        action, args = "create", {"title": "bad", "head": "ai/test"}
    elif kind == "ui":
        tool = UIBridgeTool(UIBridgeConfig(enabled=True), SimpleNamespace(call=tripwire))
        action, args = "click", {"x": 1, "y": 2}
    else:
        image = tmp_path / "image.png"
        image.write_bytes(b"test")
        tool = VisionGroundingTool(SimpleNamespace(complete=tripwire))
        action, args = "ground", {"image_path": str(image)}
    if mode == "dry_run":
        result = await tool.call(action, args, ctx)
        assert not result.ok and result.data["dry_run"]
    else:
        with pytest.raises(PermissionError):
            await tool.call(action, args, ctx)
    tripwire.assert_not_called()
    assert not (tmp_path / "bad").exists()
    assert ctx.permissions is perms


@pytest.mark.asyncio
async def test_real_policy_dry_run_never_creates_file(tmp_path):
    permissions = PermissionManager(PermissionConfig(default_mode="dry_run", audit_log=tmp_path / "audit.log"))
    result = await FilesTool(tmp_path, allow_write=True).call("write_text", {"path": "bad", "text": "bad"}, ToolContext(permissions=permissions))
    assert not result.ok
    assert not (tmp_path / "bad").exists()
    assert '"event": "dry_run"' in (tmp_path / "audit.log").read_text()


def test_helm_startup_and_writable_paths_are_explicit():
    root = Path(__file__).resolve().parents[1]
    deployment = (root / "deploy/kubernetes/helm/omnidesk/templates/deployment.yaml").read_text()
    config = (root / "deploy/kubernetes/helm/omnidesk/templates/configmap.yaml").read_text()
    assert 'command: ["omnidesk"]' in deployment
    assert '"serve", "--host", "0.0.0.0"' in deployment
    assert "root: /data/workspace" in config
    assert "skills_dirs: [/data/skills]" in config
    assert "plugins_dirs: [/data/plugins]" in config
    assert "readOnlyRootFilesystem: true" in deployment


@pytest.mark.asyncio
@pytest.mark.parametrize("action,args", [("screenshot", {}), ("click", {"x": 1, "y": 2}), ("move", {"x": 1, "y": 2}), ("type_text", {"text": "bad"}), ("hotkey", {"keys": ["enter"]})])
@pytest.mark.parametrize("mode", ["dry_run", "deny"])
async def test_public_computer_helpers_enforce_returned_denials(action, args, mode, tmp_path):
    permissions = Mock()
    permissions.verify.return_value = PermissionDecision(False, mode)
    tool = ComputerTool(tmp_path)
    args = {**args, "expected_result": "test effect"}
    if mode == "dry_run":
        assert not (await getattr(tool, action)(args, ToolContext(permissions=permissions))).ok
    else:
        with pytest.raises(PermissionError):
            await getattr(tool, action)(args, ToolContext(permissions=permissions))


def test_original_metric_retention_control():
    metrics = MetricsRegistry()
    for _ in range(1000):
        metrics.observe("probe", 1)
    assert len(metrics.histograms["probe"]) <= 64


@pytest.mark.asyncio
async def test_original_dry_run_control(tmp_path):
    permissions = Mock()
    permissions.verify.return_value = PermissionDecision(False, "dry_run")
    tool = PullRequestTool(tmp_path)
    tool._run = Mock(side_effect=AssertionError("dry-run launched a process"))
    result = await tool.call("create", {"head": "ai/probe", "title": "probe"}, ToolContext(permissions=permissions))
    assert not result.ok
    tool._run.assert_not_called()


def test_original_readonly_capability_control(tmp_path):
    assert "write_text" not in FilesTool(tmp_path).spec().actions


@pytest.mark.asyncio
async def test_original_chunked_body_control():
    guard = ChatAwareApiResourceGuard(ApiResourceGuardConfig(max_body_bytes=3))
    req = request([b"ab", b"cd", b"unread"])
    with pytest.raises(HTTPException) as exc:
        await guard.before_request(req)
    assert exc.value.status_code == 413
    assert req.state.received_count() == 2


@pytest.mark.asyncio
async def test_original_disabled_webhook_control():
    cfg = AppConfig()
    adapter = Mock()
    adapter.extract_envelope.return_value = SimpleNamespace(source_key="u", message_id="m", timestamp=None)
    req = SimpleNamespace(body=AsyncMock(return_value=b'{}'), headers={}, query_params={})
    runtime = SimpleNamespace(webhook_security=Mock())
    with pytest.raises(HTTPException) as exc:
        await WebhookGuard(cfg, runtime).guard("telegram", adapter, req)
    assert exc.value.status_code == 403
    runtime.webhook_security.guard.assert_not_called()


def test_original_runtime_test_sandbox_control(tmp_path):
    cfg = AppConfig()
    for name in type(cfg.capabilities).model_fields:
        getattr(cfg.capabilities, name).enabled = False
    cfg.capabilities.test.enabled = True
    cfg.gateway.host = "0.0.0.0"
    cfg.workspace.root = tmp_path
    cfg.sandbox = SandboxConfig(backend="remote_docker", runner_url="http://runner")
    runtime = SimpleNamespace(cfg=cfg, tools=ToolRegistry())
    OmniDeskRuntime._register_builtin_tools(runtime)
    runner = runtime.tools.get("test").tester.runner
    assert runner.backend == "remote_docker"
    assert runner.sandbox_cfg is cfg.sandbox
    assert runner.require_isolation


@pytest.mark.asyncio
@pytest.mark.parametrize("signal,value", [("OMNIDESK_ENV", "production"), ("APP_ENV", "prod"), ("ENV", "production"), ("OMNIDESK_REQUIRE_PRODUCTION_GUARDS", "true"), ("KUBERNETES_SERVICE_HOST", "cluster")])
async def test_shared_test_runner_never_spawns_production_argv(signal, value, monkeypatch, tmp_path):
    for name in ["OMNIDESK_ENV", "APP_ENV", "ENV", "OMNIDESK_REQUIRE_PRODUCTION_GUARDS", "KUBERNETES_SERVICE_HOST"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(signal, value)
    spawn = AsyncMock(side_effect=AssertionError("production host spawn"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runner = SandboxRunner(tmp_path)
    result = await runner.run("pytest")
    assert not result.ok and result.exit_code == 126
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_shared_test_runner_retains_development_and_configured_remote(monkeypatch, tmp_path):
    for name in ["OMNIDESK_ENV", "APP_ENV", "ENV", "OMNIDESK_REQUIRE_PRODUCTION_GUARDS", "KUBERNETES_SERVICE_HOST"]:
        monkeypatch.delenv(name, raising=False)
    proc = SimpleNamespace(communicate=AsyncMock(return_value=(b"passed", None)), returncode=0)
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert (await SandboxRunner(tmp_path).run("pytest")).ok
    spawn.assert_awaited_once()
    spawn.reset_mock()
    assert not (await SandboxRunner(tmp_path, require_isolation=True).run("pytest")).ok
    spawn.assert_not_called()
    cfg = SandboxConfig(backend="remote_docker", runner_url="http://runner")
    remote = AsyncMock(return_value=SimpleNamespace(ok=True, exit_code=0, stdout="passed", stderr=""))
    monkeypatch.setattr("omnidesk_agent.sandbox.remote_runner.RemoteSandboxClient.run_command", remote)
    assert (await TestTool(tmp_path, cfg, require_isolation=True).tester.run("pytest")).ok
    remote.assert_awaited_once()
    spawn.assert_not_called()
    assert not (await TestTool(tmp_path, SandboxConfig(backend="remote_docker")).tester.run("pytest")).ok
    spawn.assert_not_called()
