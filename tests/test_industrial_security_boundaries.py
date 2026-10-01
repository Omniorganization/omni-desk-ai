from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from omnidesk_agent.config import AppConfig, ApiResourceGuardConfig, ChromeConfig, PermissionConfig, UIBridgeConfig
from omnidesk_agent.observability.metrics import MetricsRegistry
from omnidesk_agent.security.chat_resource_guard import ChatAwareApiResourceGuard
from omnidesk_agent.security.permissions import PermissionDecision, PermissionManager
from omnidesk_agent.security.resource_guard import ApiResourceGuard
from omnidesk_agent.server_routes.webhook_guard import WebhookGuard
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.tools.files import FilesTool
from omnidesk_agent.tools.gmail_tool import GmailTool
from omnidesk_agent.tools.pr_tool import PullRequestTool
from omnidesk_agent.tools.shell import ShellTool
from omnidesk_agent.tools.ui_bridge_tool import UIBridgeTool
from omnidesk_agent.tools.vision import VisionGroundingTool


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
