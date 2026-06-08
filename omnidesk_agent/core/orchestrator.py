from __future__ import annotations

import json
from omnidesk_agent.core.models import ChannelMessage, ToolResult
from omnidesk_agent.core.planner import HierarchicalPlanner
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.security.permissions import PermissionManager


class Orchestrator:
    def __init__(self, planner: HierarchicalPlanner, tools: ToolRegistry, permissions: PermissionManager, memory: ExperienceStore):
        self.planner = planner
        self.tools = tools
        self.permissions = permissions
        self.memory = memory

    async def handle_message(self, msg: ChannelMessage) -> dict:
        plan = await self.planner.plan(msg)
        ctx = ToolContext(permissions=self.permissions, source=msg.channel, actor=msg.sender_id)
        results: list[ToolResult] = []
        for step in plan.steps:
            result = await self.tools.call(step.tool, step.action, step.args, ctx)
            results.append(result)
            if not result.ok:
                break
        outcome = "\n".join([r.summary or r.error or "" for r in results])
        self.memory.add(task=msg.text, plan=json.dumps([s.__dict__ for s in plan.steps], ensure_ascii=False), outcome=outcome, tags=[msg.channel])
        return {
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "rationale": plan.rationale,
            "steps": [s.__dict__ for s in plan.steps],
            "results": [r.__dict__ for r in results],
        }
