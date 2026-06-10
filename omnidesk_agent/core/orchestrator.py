from __future__ import annotations

import json
from omnidesk_agent.core.models import ChannelMessage, ToolResult
from omnidesk_agent.core.planner import HierarchicalPlanner
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.core.execution_strategy import ResultOrientedExecutionStrategy


class Orchestrator:
    def __init__(
        self,
        planner: HierarchicalPlanner,
        tools: ToolRegistry,
        permissions: PermissionManager,
        memory: ExperienceStore,
        execution_strategy: ResultOrientedExecutionStrategy | None = None,
    ):
        self.planner = planner
        self.tools = tools
        self.permissions = permissions
        self.memory = memory
        self.execution_strategy = execution_strategy or ResultOrientedExecutionStrategy()

    async def handle_message(self, msg: ChannelMessage) -> dict:
        plan = await self.planner.plan(msg)
        ctx = ToolContext(permissions=self.permissions, source=msg.channel, actor=msg.sender_id)
        results: list[ToolResult] = []

        for step in plan.steps:
            # Think before executing: expensive operations must declare a target result.
            if "expected_result" not in step.args:
                step.args["expected_result"] = step.description or plan.goal

            decision = self.execution_strategy.decide_tool_step(
                tool=step.tool,
                action=step.action,
                args=step.args,
                goal=plan.goal,
            )
            if not decision.allowed:
                results.append(
                    ToolResult(
                        False,
                        error=f"Execution skipped before spending tokens/actions: {decision.reason}",
                        summary=f"skipped {step.tool}.{step.action}",
                    )
                )
                break

            result = await self.tools.call(step.tool, step.action, step.args, ctx)
            results.append(result)
            if not result.ok:
                break

        outcome = "\n".join([r.summary or r.error or "" for r in results])
        compact_steps = [
            {
                "description": s.description,
                "tool": s.tool,
                "action": s.action,
                "risk": s.risk,
                "args_keys": sorted(s.args.keys()),
            }
            for s in plan.steps
        ]
        self.memory.add(
            task=msg.text[:1000],
            plan=json.dumps(compact_steps, ensure_ascii=False),
            outcome=outcome[:2000],
            tags=[msg.channel],
        )
        return {
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "rationale": plan.rationale,
            "steps": [s.__dict__ for s in plan.steps],
            "results": [r.__dict__ for r in results],
        }
