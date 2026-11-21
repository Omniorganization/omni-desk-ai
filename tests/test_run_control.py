"""Stopping agent work: cancel one run, stop everything, and one run per desktop.

The critic found no way to cancel a run or stop the runtime, no bound on plan
length or tokens per run, and no lock that keeps two runs from driving the
desktop at once. These tests run the real loop (see test_agent_loop.Harness).
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from pydantic import ValidationError

from omnidesk_agent.core.agent_loop import LoopBudget
from omnidesk_agent.core.models import ChannelMessage, ToolResult
from omnidesk_agent.core.plan_schema import MAX_PLAN_STEPS, PlannerTurnSchema, StructuredPlan
from omnidesk_agent.core.run_control import EmergencyStop, EmergencyStopEngaged
from omnidesk_agent.tools.base import ToolContext, permission_guarded, proposal
from omnidesk_agent.tools.computer import ComputerTool, DesktopLease
from omnidesk_agent.tools.registry import ToolRegistry
from test_agent_loop import Harness, step, turn, write_then_done


class SideEffect:
    """A low-risk tool that runs a callback, to act while a step is executing."""

    name = "side"

    def __init__(self):
        self.calls: list[str] = []
        self.during: Any = None

    def spec(self):
        from omnidesk_agent.tools.spec import ActionSpec, ToolSpec

        return ToolSpec(name=self.name, description="side", permissions=[], actions={"do": ActionSpec("do", "do", {"label": "string"}, risk="low", side_effect=True, requires_approval=False)})

    @permission_guarded
    async def call(self, action: str, args: dict, ctx: ToolContext) -> ToolResult:
        ctx.permissions.verify(proposal("side", action, {"label": args.get("label")}, "low", "side", ctx))
        self.calls.append(str(args.get("label")))
        if self.during is not None:
            await self.during(ctx)
        return ToolResult(True, summary=f"did {args.get('label')}")


def two_steps(payload, request):
    if payload["turn"] == 1:
        return turn(steps=[step("side", "do", {"label": "first"}, risk="low"), step("side", "do", {"label": "second"}, risk="low")])
    return turn("done", final_answer="both done")


@pytest.fixture
def make(tmp_path, monkeypatch):
    made: list[Harness] = []

    def build(decide, **kwargs) -> Harness:
        h = Harness(tmp_path, monkeypatch, decide, **kwargs)
        h.orchestrator.emergency_stop = EmergencyStop(tmp_path / "stop.json")
        h.orchestrator.tools.emergency_stop = h.orchestrator.emergency_stop
        made.append(h)
        return h

    yield build
    for h in made:
        h.close()


def test_cancel_during_a_step_stops_the_loop_before_the_next_one(make):
    side = SideEffect()
    h = make(two_steps, extra_tools=(side,))

    async def cancel_now(ctx):
        await h.orchestrator.cancel(ctx.run_id, reason="operator changed their mind", actor="alice")

    side.during = cancel_now
    result = h.run("do both")

    assert side.calls == ["first"]  # the second step never ran
    assert result["status"] == "cancelled" and result["termination"]["reason"] == "cancelled"
    assert h.runs.get(result["run_id"])["status"] == "cancelled"
    assert len(h.script.requests) == 1  # and no further planning


def test_cancelling_a_waiting_run_withdraws_its_approval_and_blocks_resume(make):
    h = make(write_then_done, approval_mode="remote_approval")
    paused = h.run("save the quarterly report")

    cancelled = asyncio.run(h.orchestrator.cancel(paused["run_id"], reason="not needed", actor="alice"))
    assert cancelled == {"ok": True, "status": "cancelled", "run_id": paused["run_id"], "previous_status": "waiting_approval"}
    assert h.approvals.get(paused["approval_id"])["status"] == "denied"
    assert h.resume(paused)["status"] == "resume_denied"
    assert not (h.workspace / "report.txt").exists()
    assert asyncio.run(h.orchestrator.cancel(paused["run_id"]))["ok"] is False  # already finished


def test_emergency_stop_refuses_new_runs_and_tool_calls(make, tmp_path):
    side = SideEffect()
    h = make(two_steps, extra_tools=(side,))
    h.orchestrator.emergency_stop.engage(reason="suspected injection", actor="alice")

    result = h.run("do both")
    assert result["status"] == "failed" and result["termination"]["reason"] == "emergency_stop"
    assert h.script.requests == [] and side.calls == []
    direct = asyncio.run(h.orchestrator.tools.call("side", "do", {"label": "x"}, ToolContext(permissions=h.permissions)))
    assert direct.ok is False and "EmergencyStopEngaged" in (direct.error or "")

    h.orchestrator.emergency_stop.release()
    assert h.run("do both")["status"] == "completed"


def test_emergency_stop_halts_a_running_loop_between_steps(make):
    side = SideEffect()
    h = make(two_steps, extra_tools=(side,))

    async def stop_everything(ctx):
        h.orchestrator.emergency_stop.engage(reason="drill", actor="alice")

    side.during = stop_everything
    result = h.run("do both")
    assert side.calls == ["first"] and result["termination"]["reason"] == "emergency_stop"


def test_a_stop_during_an_approval_request_withdraws_it(make):
    def write(payload, request):
        return turn(steps=[step("files", "write_text", {"path": "x.txt", "text": "x"}, risk="high")])

    h = make(write, approval_mode="remote_approval")
    original = h.orchestrator._pause

    async def engage_then_pause(msg, state, approval, segment_started):
        h.orchestrator.emergency_stop.engage(reason="drill", actor="alice")
        return await original(msg, state, approval, segment_started)

    h.orchestrator._pause = engage_then_pause  # type: ignore[method-assign]
    result = h.run("write x")
    assert result["status"] == "failed" and result["termination"]["reason"] == "emergency_stop"
    assert [a["status"] for a in h.approvals.list()] == ["denied"]


def test_an_unreadable_stop_file_counts_as_engaged(tmp_path):
    switch = EmergencyStop(tmp_path / "stop.json")
    assert switch.status().engaged is False
    (tmp_path / "stop.json").write_text("{not json", encoding="utf-8")
    assert switch.status().engaged is True
    with pytest.raises(EmergencyStopEngaged):
        switch.check()


def test_one_run_drives_the_desktop_at_a_time(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("omnidesk_agent.tools.computer.time.monotonic", lambda: clock[0])
    lease = DesktopLease(idle_seconds=60)
    tools = ToolRegistry()
    tool = ComputerTool(tmp_path / "shots", lease=lease)
    tools.register(tool)

    class Allow:
        def verify(self, prop):
            from omnidesk_agent.security.permissions import PermissionDecision

            return PermissionDecision(True)

    pyautogui = type(sys)("pyautogui")
    pyautogui.click = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "pyautogui", pyautogui)

    def click(run_id: str) -> ToolResult:
        ctx = ToolContext(permissions=Allow(), source="local-cli", actor="owner", run_id=run_id)  # type: ignore[arg-type]
        return asyncio.run(tools.call("computer", "click", {"x": 1, "y": 1, "expected_result": "clicked"}, ctx))

    assert click("run-a").ok
    blocked = click("run-b")
    assert blocked.ok is False and "in use by another run (run-a)" in (blocked.error or "")
    tools.release_run("run-a")  # run A finished or paused for an approval
    assert click("run-b").ok
    clock[0] += 61  # run B left the desktop idle
    assert click("run-a").ok and lease.owner == "run-a"


def test_plans_and_turns_are_capped():
    step_dict = {"description": "d", "tool": "files", "action": "list", "args": {}, "expected_result": "e"}
    with pytest.raises(ValidationError):
        StructuredPlan.model_validate({"goal": "g", "steps": [step_dict] * (MAX_PLAN_STEPS + 1), "success_criteria": "s"})
    with pytest.raises(ValidationError):
        PlannerTurnSchema.model_validate({"status": "continue", "steps": [step_dict] * (MAX_PLAN_STEPS + 1)})
    assert len(PlannerTurnSchema.model_validate({"steps": [step_dict] * MAX_PLAN_STEPS}).steps) == MAX_PLAN_STEPS


def test_a_run_stops_at_its_token_budget(make):
    h = make(lambda payload, request: turn(steps=[step("files", "list", {"path": "."}, risk="low")]), budget=LoopBudget(max_tokens=5_000), usage={"input_tokens": 3_000, "output_tokens": 500})
    result = h.run("keep listing")
    assert result["termination"]["reason"] == "budget_tokens"
    assert result["loop"]["tokens"] == 7_000 and result["loop"]["llm_calls"] == 2


def test_cli_stop_and_cancel(tmp_path, monkeypatch, capsys):
    import yaml

    from omnidesk_agent import cli

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "llm": {"provider": "rule"},
        "workspace": {"root": str(home / "ws"), "skills_dirs": [str(home / "skills")], "plugins_dirs": [str(home / "plugins")], "memory_db": str(home / "memory.sqlite3")},
        "permissions": {"approval_mode": "remote_approval", "audit_log": str(home / "audit" / "audit.log")},
        "capabilities": {"files": {"enabled": True, "allow_write": True}},
        "learning": {"growth_plan_file": str(home / "growth.json")},
        "channels": {"gmail": {"credentials_file": str(home / "g" / "c.json"), "token_file": str(home / "g" / "t.json")}},
    }), encoding="utf-8")

    def omnidesk(*argv: str) -> tuple[int, dict]:
        monkeypatch.setattr(sys, "argv", ["omnidesk", "--config", str(config), *argv])
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = int(exc.code or 0)
        return code, json.loads(capsys.readouterr().out)

    _, engaged = omnidesk("stop", "--reason", "drill")
    assert engaged["engaged"] is True and (home / "audit" / "emergency_stop.json").exists()
    assert omnidesk("stop", "--status")[1]["engaged"] is True
    assert omnidesk("stop", "--release")[1]["engaged"] is False

    msg = ChannelMessage(channel="local-cli", sender_id="owner", text="write file hello")
    with cli.runtime_context(cli.load_config(str(config))) as rt:
        paused = asyncio.run(rt.orchestrator.handle_message(msg))
    assert paused["status"] == "waiting_approval"
    code, cancelled = omnidesk("cancel", paused["run_id"], "--reason", "not needed")
    assert code == 0 and cancelled["status"] == "cancelled"
    code, again = omnidesk("cancel", paused["run_id"])
    assert code == 1 and again["status"] == "cancelled"
