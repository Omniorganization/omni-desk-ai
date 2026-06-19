from __future__ import annotations

import asyncio

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.core.models import ChannelMessage, Plan, PlanStep, ToolResult
from omnidesk_agent.core.orchestrator import Orchestrator
from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext, proposal
from omnidesk_agent.tools.registry import ToolRegistry


class OneStepPlanner:
    async def plan(self, msg):
        return Plan(goal="approve once", steps=[PlanStep("do it", "danger", "call", {}, "high", True)], plan_id="plan-1")


class ApprovalTool:
    name = "danger"

    async def call(self, action: str, args: dict, ctx: ToolContext) -> ToolResult:
        ctx.permissions.verify(proposal("danger", action, args, "high", "test high-risk action", ctx))
        return ToolResult(True, data={"executed": True}, summary="executed")


def test_resume_uses_approved_scope_without_creating_second_approval(tmp_path):
    async def run_case():
        approvals = ApprovalStore(tmp_path / "approvals.sqlite3")
        permissions = PermissionManager(
            PermissionConfig(approval_mode="remote_approval", audit_log=tmp_path / "audit.jsonl"),
            approvals,
        )
        tools = ToolRegistry()
        tools.register(ApprovalTool())
        memory = ExperienceStore(tmp_path / "memory.sqlite3")
        runs = RunStore(tmp_path / "runs.sqlite3")
        orchestrator = Orchestrator(OneStepPlanner(), tools, permissions, memory, run_store=runs, approval_store=approvals)
        try:
            first = await orchestrator.handle_message(ChannelMessage(channel="test", sender_id="u", text="go"))
            assert first["status"] == "waiting_approval"
            assert len(approvals.list()) == 1
            approvals.decide(first["approval_id"], "approved")

            resumed = await orchestrator.resume(first["run_id"], resume_token=first["resume_token"])

            assert resumed["status"] == "completed"
            assert resumed["results"][0]["ok"] is True
            assert len(approvals.list()) == 1
            assert runs.get(first["run_id"])["status"] == "completed"
        finally:
            memory.close()

    asyncio.run(run_case())
