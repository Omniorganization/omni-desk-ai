from __future__ import annotations

from dataclasses import dataclass

import pytest

from omnidesk_agent.config import ChromeConfig
from omnidesk_agent.models.base import ModelResponse
from omnidesk_agent.security.permissions import PermissionDecision
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.tools.files import FilesTool
from omnidesk_agent.tools.git_tool import GitTool
from omnidesk_agent.tools.pr_tool import PullRequestTool
from omnidesk_agent.tools.spec import ActionSpec, ToolSpecRegistry, normalize_schema
from omnidesk_agent.tools.vision import VisionGroundingTool


class AllowPermissions:
    def verify(self, proposal):
        return PermissionDecision(True, "allow", "ok")


class DryRunPermissions:
    def verify(self, proposal):
        return PermissionDecision(False, "dry_run", "dry")


def ctx(perms=None):
    return ToolContext(permissions=perms or AllowPermissions(), source="test", actor="u")


@pytest.mark.asyncio
async def test_files_tool_read_write_list_escape_and_unknown(tmp_path):
    tool = FilesTool(tmp_path)
    write = await tool.call("write_text", {"path": "a/b.txt", "text": "hello"}, ctx())
    assert write.ok
    read = await tool.call("read_text", {"path": "a/b.txt"}, ctx())
    assert read.data["text"] == "hello"
    listed = await tool.call("list", {"path": "a"}, ctx())
    assert listed.data["items"][0]["name"] == "b.txt"
    with pytest.raises(PermissionError):
        tool._safe_path("../escape.txt")
    with pytest.raises(ValueError):
        await tool.call("bad", {}, ctx())


@pytest.mark.asyncio
async def test_git_tool_branches_dry_run_and_subprocess(monkeypatch, tmp_path):
    tool = GitTool(tmp_path)
    calls = []

    @dataclass
    class Result:
        returncode: int = 0
        stdout: str = "ok"
        stderr: str = ""

    def fake_run(args, timeout=60):
        calls.append(args)
        return Result()

    monkeypatch.setattr(tool, "_run", fake_run)
    assert not (await tool.call("unsupported", {}, ctx())).ok
    assert (await tool.call("status", {}, ctx())).ok
    assert (await tool.call("diff", {}, ctx())).ok
    assert not (await tool.call("checkout_new_branch", {"branch": "bad"}, ctx())).ok
    assert (await tool.call("checkout_new_branch", {"branch": "ai/test"}, ctx())).ok
    assert (await tool.call("add", {}, ctx())).ok
    assert (await tool.call("commit", {"message": "msg"}, ctx())).ok
    assert not (await tool.call("push", {"branch": "main"}, ctx())).ok
    assert (await tool.call("push", {"branch": "ai/test"}, ctx())).ok
    assert not (await tool.call("status", {}, ctx(DryRunPermissions()))).ok
    assert ["status", "--short"] in calls


@pytest.mark.asyncio
async def test_pull_request_tool_create_and_validation(monkeypatch, tmp_path):
    tool = PullRequestTool(tmp_path)

    @dataclass
    class Result:
        returncode: int = 0
        stdout: str = "https://github.com/acme/repo/pull/1\n"
        stderr: str = ""

    def fake_run(args):
        assert args[:2] == ["pr", "create"]
        return Result()

    monkeypatch.setattr(tool, "_run", fake_run)
    with pytest.raises(ValueError):
        await tool.call("bad", {}, ctx())
    assert not (await tool.call("create", {"title": "t", "head": "main"}, ctx())).ok
    result = await tool.call("create", {"title": "t", "body": "b", "base": "main", "head": "ai/x", "draft": False}, ctx())
    assert result.ok
    assert "pull/1" in result.summary


class FakeBrowser(BrowserTool):
    async def _tabs(self):
        return [{"id": "1", "title": "T", "url": "https://example.com/page", "webSocketDebuggerUrl": "ws://x"}]

    async def _cdp(self, method, params=None, target_id=None):
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            return {"nodeId": 2}
        if method == "DOM.getOuterHTML":
            return {"outerHTML": "<html><body>hello page</body></html>"}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [10, 10, 30, 10, 30, 30, 10, 30]}}
        if method == "Runtime.evaluate" and params and "innerText" in params.get("expression", ""):
            return {"result": {"value": "hello page"}}
        if method == "Page.captureScreenshot":
            return {"data": "png"}
        return {"method": method, "params": params or {}, "target_id": target_id}


@pytest.mark.asyncio
async def test_browser_tool_safe_branches():
    cfg = ChromeConfig(allowed_origins=["https://example.com"], allow_evaluate=True)
    tool = FakeBrowser(cfg)
    tool._check_url("https://example.com/a")
    with pytest.raises(ValueError):
        tool._check_url("https://evil.test/a")
    with pytest.raises(PermissionError):
        tool._check_js("document.cookie")
    cfg.deny_js_patterns = []
    assert (await tool.call("list_tabs", {}, ctx())).ok
    assert (await tool.call("navigate", {"url": "https://example.com/b"}, ctx())).ok
    assert (await tool.call("evaluate", {"expression": "1+1"}, ctx())).ok
    assert (await tool.call("get_dom_text", {}, ctx())).data["text"] == "hello page"
    assert (await tool.call("click_selector", {"selector": "button"}, ctx())).ok
    assert (await tool.call("type_selector", {"selector": "input", "text": "abc"}, ctx())).ok
    assert (await tool.call("screenshot", {}, ctx())).data["png_base64"] == "png"
    with pytest.raises(ValueError):
        await tool.call("unknown", {}, ctx())


def test_spec_helpers_cover_shorthand_and_inferred_tool():
    schema = normalize_schema({"name": "string", "count": "integer", "items": "list[string]"})
    assert schema["properties"]["items"]["type"] == "array"
    spec = ActionSpec("a", "desc", {"name": "string", "count": "integer"})
    assert spec.validate_args({"name": "x", "count": 1}) == []
    assert "missing required arg" in spec.validate_args({"name": "x"})[0]
    assert "expected integer" in spec.validate_args({"name": "x", "count": True})[0]

    class AnonymousTool:
        name = "anon"

    inferred = ToolSpecRegistry.infer(AnonymousTool())
    assert inferred.actions["*"].side_effect is True


class FakeRouter:
    async def complete(self, request):
        assert request.images
        return ModelResponse(text='{"summary":"ok","target":{"x":1},"confidence":0.9}', provider="fake", model="vision", profile="vision")


@pytest.mark.asyncio
async def test_vision_grounding_tool_success_and_errors(tmp_path):
    img = tmp_path / "screen.png"
    img.write_bytes(b"png")
    tool = VisionGroundingTool(FakeRouter())
    with pytest.raises(ValueError):
        await tool.call("bad", {}, ctx())
    missing = await tool.call("ground", {"image_path": tmp_path / "missing.png"}, ctx())
    assert not missing.ok
    result = await tool.call("ground", {"image_path": img, "instruction": "find"}, ctx())
    assert result.ok
    assert result.data["grounding"]["summary"] == "ok"
