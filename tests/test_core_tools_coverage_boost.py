from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnidesk_agent.config import PermissionConfig, SandboxConfig, UIBridgeConfig
from omnidesk_agent.core.execution_strategy import ResultOrientedExecutionStrategy
from omnidesk_agent.core.models import ApprovalDecision, ChannelMessage, Plan, PlanStep, ToolResult
from omnidesk_agent.core.outbound_dispatcher import OutboundDispatcher
from omnidesk_agent.core.outbound_messages import OutboundMessageStore
from omnidesk_agent.core.planner import HierarchicalPlanner
from omnidesk_agent.core.structured_planner import LLMStructuredPlanner
from omnidesk_agent.core.worker import WebhookWorker
from omnidesk_agent.models.base import ModelResponse
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.shell import ShellTool
from omnidesk_agent.tools.ui_bridge_tool import UIBridgeTool


class Perms:
    def __init__(self, mode: str = "allow"):
        self.mode = mode
        self.proposals = []

    def verify(self, prop):
        self.proposals.append(prop)
        return ApprovalDecision(self.mode != "deny", self.mode, "ok")


def ctx(mode: str = "allow") -> ToolContext:
    return ToolContext(permissions=Perms(mode), source="unit", actor="tester")


class Memory:
    def search(self, text: str, limit: int = 4):
        return [{"task": text, "limit": limit}]

    def retrieve_for_task(self, text: str, limit: int = 4):
        return [{"task": text, "source": "retrieve", "limit": limit}]


class Skills:
    def __init__(self, block: str = ""):
        self.block = block

    def prompt_block(self, text: str, max_chars: int = 6000):
        return self.block[:max_chars]


class ToolsDescribe:
    def names(self):
        return ["files", "computer"]

    def describe(self):
        return {
            "files": {"actions": {"write_text": {"input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}, "expected_result": {"type": "string"}}, "required": ["path", "text", "expected_result"], "additionalProperties": True}, "risk": "high", "requires_approval": True}}},
            "computer": {"actions": {"screenshot": {}, "click": {}}},
        }


class FallbackPlanner:
    def __init__(self):
        self.calls = 0

    async def plan(self, msg: ChannelMessage):
        self.calls += 1
        return Plan(goal="fallback", steps=[PlanStep("fallback", "files", "write_text", {"path": "x", "text": "y", "expected_result": "saved"}, "high", True)], rationale="fallback")


class Router:
    def __init__(self, texts: list[str]):
        self.texts = list(texts)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(text=self.texts.pop(0), provider="fake", model="fake", profile="planner")


VALID_STRUCTURED_PLAN = json.dumps({
    "goal": "save note",
    "task_type": "file",
    "risk": "high",
    "steps": [
        {
            "description": "write note",
            "tool": "files",
            "action": "write_text",
            "args": {"path": "note.txt", "text": "hello", "expected_result": "saved"},
            "risk": "high",
            "requires_approval": True,
            "expected_result": "saved",
        }
    ],
    "success_criteria": "file saved",
    "rollback_plan": "delete file",
})


def test_hierarchical_planner_rule_branches():
    planner = HierarchicalPlanner(None, Memory(), Skills("matched skill"), None)
    cases = [
        ("screenshot screen", "computer", "screenshot"),
        ("shell: python -m compileall omnidesk_agent", "shell", "run"),
        ("write file hello", "files", "write_text"),
        ("click 100, 200", "computer", "click"),
        ("click login button", "computer", "screenshot"),
        ("open WhatsApp Business", "ui_bridge", "observe"),
        ("remember this later", "files", "write_text"),
    ]
    for text, tool, action in cases:
        plan = planner._rule_plan(ChannelMessage(channel="test", sender_id="u", thread_id="t", text=text), [{"x": 1}], "skill")
        assert any(step.tool == tool and step.action == action for step in plan.steps)
    assert HierarchicalPlanner._guess_app("use 飞书") == "Feishu"
    assert HierarchicalPlanner._guess_app("unknown") == "Google Chrome"
    assert asyncio.run(planner.plan(ChannelMessage(channel="test", sender_id="u", thread_id="t", text="telegram status"))).steps[0].tool == "ui_bridge"


def test_structured_planner_valid_repair_and_fallback_paths():
    async def run_case():
        msg = ChannelMessage(channel="test", sender_id="u", thread_id="thread", message_id="m1", text="make a note")
        fallback = FallbackPlanner()
        planner = LLMStructuredPlanner(Router([VALID_STRUCTURED_PLAN]), Memory(), Skills("skill"), ToolsDescribe(), fallback)
        plan = await planner.plan(msg)
        assert plan.goal == "save note"
        assert fallback.calls == 0

        fallback2 = FallbackPlanner()
        repair = LLMStructuredPlanner(Router(["not json", VALID_STRUCTURED_PLAN]), Memory(), Skills(), ToolsDescribe(), fallback2)
        repaired = await repair.plan(msg)
        assert repaired.goal == "save note"
        assert len(repair.router.requests) == 2

        fallback3 = FallbackPlanner()
        bad = LLMStructuredPlanner(Router(["not json", "still bad"]), Memory(), Skills(), ToolsDescribe(), fallback3)
        assert (await bad.plan(msg)).goal == "fallback"
        assert fallback3.calls == 1

        fallback4 = FallbackPlanner()
        rule = LLMStructuredPlanner(Router([VALID_STRUCTURED_PLAN]), Memory(), Skills(), ToolsDescribe(), fallback4)
        assert (await rule.plan(ChannelMessage(channel="test", sender_id="u", thread_id="t", text="shell: pytest"))).goal == "fallback"
        assert LLMStructuredPlanner._should_use_rule("写文件 abc") is True
        assert LLMStructuredPlanner._should_use_rule("plan a task") is False

    asyncio.run(run_case())


def test_execution_strategy_duplicate_and_screenshot_branches(monkeypatch):
    strategy = ResultOrientedExecutionStrategy()
    assert strategy.decide_tool_step(tool="files", action="write_text", args={}, goal="").allowed is False
    accepted = strategy.decide_tool_step(tool="computer", action="screenshot", args={"expected_result": "inspect"}, goal="g")
    assert accepted.allowed and accepted.requires_screenshot
    duplicate = strategy.decide_tool_step(tool="computer", action="screenshot", args={"expected_result": "inspect"}, goal="g")
    assert duplicate.allowed is False
    llm = strategy.decide_tool_step(tool="llm", action="complete", args={"expected_result": "answer"}, goal="g")
    assert llm.requires_llm is True

    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    screenshot = ResultOrientedExecutionStrategy()
    first = screenshot.decide_screenshot_analysis(b"a", min_interval_seconds=1.0)
    assert first.allowed is True
    same = screenshot.decide_screenshot_analysis(b"a", min_interval_seconds=1.0)
    assert same.reason.startswith("same screenshot")
    quick = screenshot.decide_screenshot_analysis(b"b", min_interval_seconds=1.0)
    assert quick.reason.startswith("screenshot too frequent")
    now[0] += 2.0
    assert screenshot.decide_screenshot_analysis(b"b", min_interval_seconds=1.0).allowed is True


class QueueNoJob:
    def __init__(self):
        self.recovered = False

    def recover_stale_running(self, lease_seconds=300):
        self.recovered = True

    def claim_next(self):
        return None


class NeverAdapter:
    pass


async def _let_task_start():
    await asyncio.sleep(0.01)


def test_worker_and_outbound_dispatcher_start_stop_and_error_branches(tmp_path: Path):
    async def run_case():
        queue = QueueNoJob()
        worker = WebhookWorker(queue, object(), poll_interval_seconds=0.01)
        worker.start()
        worker.start()  # idempotent branch
        assert queue.recovered is True
        await _let_task_start()
        await worker.stop()
        assert await worker.run_once() is False

        store = OutboundMessageStore(tmp_path / "outbound.sqlite3", max_retries=0, base_retry_seconds=0)
        dispatcher = OutboundDispatcher(store, {}, poll_interval_seconds=0.01)
        dispatcher.start()
        dispatcher.start()
        await _let_task_start()
        await dispatcher.stop()
        assert await dispatcher.run_once() is False

        unknown = store.create(channel="missing", recipient="r", payload={"type": "text", "text": "hello"}, max_retries=0)
        assert await dispatcher.run_once() is True
        assert store.get(unknown)["status"] == "dead_letter"

        bad_adapter = OutboundDispatcher(store, {"x": NeverAdapter()})
        unsupported = store.create(channel="x", recipient="r", payload={"type": "text", "text": "hello"}, max_retries=0)
        assert await bad_adapter.run_once() is True
        assert store.get(unsupported)["status"] == "dead_letter"

        bad_payload = store.create(channel="x", recipient="r", payload={"type": "image"}, max_retries=0)
        assert await bad_adapter.run_once() is True
        assert store.get(bad_payload)["status"] == "dead_letter"

    asyncio.run(run_case())


class ToolProxy:
    def __init__(self):
        self.calls = []

    async def call(self, tool, action, args, call_ctx):
        self.calls.append((tool, action, args))
        return ToolResult(True, data={"tool": tool, "action": action, "args": args}, summary="ok")


def test_ui_bridge_all_actions_and_app_guard():
    async def run_case():
        proxy = ToolProxy()
        tool = UIBridgeTool(UIBridgeConfig(allowed_apps=["Gmail"]), proxy)
        assert tool.spec().actions["observe"].name == "observe"
        assert (await tool.call("observe", {"app": "Gmail", "max_width": 700}, ctx())).data["action"] == "screenshot"
        assert (await tool.call("type_visible_reply", {"app": "Gmail", "text": "hello"}, ctx())).data["action"] == "type_text"
        assert (await tool.call("press_send", {"app": "Gmail"}, ctx())).data["action"] == "hotkey"
        assert (await tool.call("click", {"app": "Gmail", "x": "1", "y": "2"}, ctx())).data["action"] == "click"
        with pytest.raises(ValueError, match="not allowed"):
            await tool.call("observe", {"app": "Slack"}, ctx())
        with pytest.raises(ValueError, match="Unsupported ui_bridge"):
            await tool.call("unknown", {"app": "Gmail"}, ctx())

    asyncio.run(run_case())


class FakeProc:
    def __init__(self, rc=0, out=b"out", err=b"err", hang=False):
        self.returncode = rc
        self.out = out
        self.err = err
        self.hang = hang
        self.killed = False

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(10)
        return self.out, self.err

    def kill(self):
        self.killed = True


def test_shell_call_success_failure_timeout_and_arg_parsing(monkeypatch, tmp_path: Path):
    async def run_case():
        cfg = PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log")
        tool = ShellTool(tmp_path, cfg, SandboxConfig(backend="argv", timeout_seconds=1))
        assert tool.spec().actions["run"].risk == "critical"
        assert tool._argv({"argv": ["pytest", "-q"]}) == ["pytest", "-q"]
        with pytest.raises(ValueError):
            tool._argv({"command": ""})
        assert tool._allowed(["pytest", "-q"])
        assert not tool._allowed(["rm", "-rf", "/"])
        blocked = await tool.call("run", {"command": "rm -rf /", "expected_result": "bad"}, ctx())
        assert not blocked.ok
        with pytest.raises(ValueError, match="Unsupported shell"):
            await tool.call("bad", {}, ctx())

        procs = [FakeProc(0, b"ok", b""), FakeProc(2, b"", b"boom"), FakeProc(0, b"", b"", hang=True)]
        created = []

        async def fake_exec(*argv, cwd, stdout, stderr):
            created.append(argv)
            return procs.pop(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        success = await tool.call("run", {"argv": ["pytest"], "expected_result": "test"}, ctx())
        assert success.ok and success.data["exit_code"] == 0
        failure = await tool.call("run", {"argv": ["pytest"], "expected_result": "test"}, ctx())
        assert not failure.ok and "boom" in failure.error
        timeout = await tool.call("run", {"argv": ["pytest"], "expected_result": "test", "timeout": 0}, ctx())
        assert not timeout.ok and "timed out" in timeout.error

    asyncio.run(run_case())

from omnidesk_agent.tools.spec import ActionSpec, ToolSpec, ToolSpecRegistry, normalize_schema, _type_ok, _json_type
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.security.approval_required import ApprovalRequired


def test_tool_spec_and_registry_remaining_error_branches():
    schema = normalize_schema({"obj": {"type": "object"}, "flag": "boolean", "items": "array", "ratio": "number", "meta": "dict"})
    assert schema["properties"]["obj"]["type"] == "object"
    assert _json_type("float") == "number"
    assert _json_type("bool") == "boolean"
    assert _json_type("list[string]") == "array"
    assert _json_type("object") == "object"
    assert _json_type("custom") == "custom"
    assert _type_ok(1, ["string", "integer"])
    assert _type_ok("anything", "custom")
    assert not _type_ok(True, "integer")
    assert not _type_ok(True, "number")

    action = ActionSpec(
        "a",
        "desc",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
        output_schema={"ok": "boolean"},
    )
    assert "unknown arg" in action.validate_args({"name": "x", "extra": 1})[0]
    prompt = ToolSpec("t", "tool", {"a": action}, permissions=["p"]).to_prompt_dict()
    assert prompt["actions"]["a"]["output_schema"]["properties"]["ok"]["type"] == "boolean"

    class Nameless:
        pass

    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register(Nameless())
    with pytest.raises(KeyError):
        registry.get("missing")

    class FailingTool:
        name = "fail"
        async def call(self, action, args, call_ctx):
            raise RuntimeError("boom")

    class ApprovalTool:
        name = "approve"
        async def call(self, action, args, call_ctx):
            raise ApprovalRequired("run", {"x": 1})

    class Metric:
        def __init__(self):
            self.labels = []
        def inc(self, name, **labels):
            self.labels.append((name, labels))

    async def run_case():
        registry.register(FailingTool())
        registry.register(ApprovalTool())
        registry.metrics = Metric()
        failed = await registry.call("fail", "x", {}, ctx())
        assert not failed.ok and "RuntimeError" in failed.error
        with pytest.raises(ApprovalRequired):
            await registry.call("approve", "x", {}, ctx())
        assert {labels["status"] for _, labels in registry.metrics.labels} == {"exception", "approval_required"}

    asyncio.run(run_case())
