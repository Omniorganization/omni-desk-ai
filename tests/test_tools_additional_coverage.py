from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass

import pytest

from omnidesk_agent.config import ChromeConfig, PermissionConfig
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.security.permissions import PermissionDecision, PermissionManager
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.tools.files import FilesTool
from omnidesk_agent.tools.git_tool import GitTool
from omnidesk_agent.tools.github_preflight import GitHubPreflight, parse_github_remote
from omnidesk_agent.tools.pr_tool import PullRequestTool
from omnidesk_agent.tools.spec import ActionSpec, ToolSpecRegistry, normalize_schema
from omnidesk_agent.tools.test_tool import TestTool
from omnidesk_agent.tools.vision import VisionGroundingTool
from omnidesk_agent.models.base import ModelResponse


class AllowPermissions:
    def verify(self, proposal):
        return PermissionDecision(True, "allow", "ok")


class DryRunPermissions:
    def verify(self, proposal):
        return PermissionDecision(False, "dry_run", "dry")


def ctx(perms=None):
    return ToolContext(permissions=perms or AllowPermissions(), source="test", actor="u")


def test_files_tool_read_write_list_escape_and_unknown(tmp_path):
    async def run_case():
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

    asyncio.run(run_case())


def test_git_tool_branches_dry_run_and_subprocess(monkeypatch, tmp_path):
    async def run_case():
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

    asyncio.run(run_case())


def test_pull_request_tool_create_and_validation(monkeypatch, tmp_path):
    async def run_case():
        tool = PullRequestTool(tmp_path)

        @dataclass
        class Result:
            returncode: int = 0
            stdout: str = "https://github.com/acme/repo/pull/1\n"
            stderr: str = ""

        monkeypatch.setattr(tool, "_run", lambda args: Result())
        monkeypatch.setattr(tool, "_preflight", lambda head: {"ok": True, "head_published": True})
        with pytest.raises(ValueError):
            await tool.call("bad", {}, ctx())
        assert not (await tool.call("create", {"title": "t", "head": "main"}, ctx())).ok
        result = await tool.call("create", {"title": "t", "body": "b", "base": "main", "head": "ai/x", "draft": False}, ctx())
        assert result.ok
        assert "pull/1" in result.summary

    asyncio.run(run_case())


def test_github_remote_parsing():
    assert parse_github_remote("https://github.com/acme/repo.git").full_name == "acme/repo"
    assert parse_github_remote("git@github.com:acme/repo.git").full_name == "acme/repo"
    assert parse_github_remote("ssh://git@github.com/acme/repo.git").full_name == "acme/repo"
    assert parse_github_remote("https://example.com/acme/repo.git", "github.com").host == "example.com"
    assert parse_github_remote("not-a-remote") is None


def test_github_preflight_reports_invalid_auth(monkeypatch, tmp_path):
    monkeypatch.setattr("omnidesk_agent.tools.github_preflight.shutil.which", lambda name: f"/usr/bin/{name}")

    def runner(argv):
        if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(tmp_path) + "\n", stderr="")
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, stdout="git@github.com:acme/repo.git\n", stderr="")
        if argv[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="token is invalid\n")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = GitHubPreflight(tmp_path, runner=runner).run(head="ai/x")
    assert not result.ok
    assert not result.authenticated
    assert "not authenticated" in result.errors[0]
    assert "token is invalid" in result.warnings[0]


def test_github_preflight_blocks_without_write_permission(monkeypatch, tmp_path):
    monkeypatch.setattr("omnidesk_agent.tools.github_preflight.shutil.which", lambda name: f"/usr/bin/{name}")

    def runner(argv):
        if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(tmp_path) + "\n", stderr="")
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/acme/repo.git\n", stderr="")
        if argv[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Logged in\n", stderr="")
        if argv[:3] == ["gh", "api", "repos/acme/repo"]:
            return subprocess.CompletedProcess(argv, 0, stdout="false\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = GitHubPreflight(tmp_path, runner=runner).run(head="ai/x")
    assert not result.ok
    assert result.authenticated
    assert result.can_write is False
    assert "does not have push/write permission" in result.errors[0]


def test_github_preflight_requires_published_head(monkeypatch, tmp_path):
    monkeypatch.setattr("omnidesk_agent.tools.github_preflight.shutil.which", lambda name: f"/usr/bin/{name}")

    def runner(argv):
        if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(tmp_path) + "\n", stderr="")
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/acme/repo.git\n", stderr="")
        if argv[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Logged in\n", stderr="")
        if argv[:3] == ["gh", "api", "repos/acme/repo"]:
            return subprocess.CompletedProcess(argv, 0, stdout="true\n", stderr="")
        if argv[:3] == ["git", "ls-remote", "--heads"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = GitHubPreflight(tmp_path, runner=runner).run(head="ai/x")
    assert not result.ok
    assert result.can_write is True
    assert result.head_published is False
    assert "has not been pushed" in result.errors[0]


def test_test_tool_contracts(monkeypatch, tmp_path):
    async def run_case():
        tool = TestTool(tmp_path)
        assert not (await tool.call("bad", {}, ctx())).ok
        assert not (await tool.call("run", {"command": "sudo reboot"}, ctx())).ok
        assert not (await tool.call("run", {"command": "pytest"}, ctx(DryRunPermissions()))).ok

        @dataclass
        class Result:
            ok: bool = True
            output: str = "passed"
            command: str = "pytest"
            exit_code: int = 0

        async def fake_run(command):
            return Result(command=command)

        monkeypatch.setattr(tool.tester, "run", fake_run)
        result = await tool.call("run", {"command": "pytest"}, ctx())
        assert result.ok

    asyncio.run(run_case())


class FakeBrowser(BrowserTool):
    async def _tabs(self):
        return [{"id": "1", "title": "T", "url": "https://example.com/page", "webSocketDebuggerUrl": "ws://x"}]

    async def _cdp(self, method, params=None, target_id=None):
        if method == "Runtime.evaluate" and params and "innerText" in params.get("expression", ""):
            return {"result": {"value": "hello page"}}
        if method == "Page.captureScreenshot":
            return {"data": "png"}
        return {"method": method, "params": params or {}, "target_id": target_id}


def test_browser_tool_safe_branches():
    async def run_case():
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

    asyncio.run(run_case())


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


def test_vision_grounding_tool_success_and_errors(tmp_path):
    async def run_case():
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

    asyncio.run(run_case())
