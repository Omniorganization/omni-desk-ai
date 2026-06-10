from __future__ import annotations

import json
from omnidesk_agent.core.models import ChannelMessage, ToolResult
from omnidesk_agent.core.planner import HierarchicalPlanner
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.registry import ToolRegistry
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

        try:
            for step in plan.steps:
                if "expected_result" not in step.args:
                    step.args["expected_result"] = step.description or plan.goal

                decision = self.execution_strategy.decide_tool_step(
                    tool=step.tool,
                    action=step.action,
                    args=step.args,
                    goal=plan.goal,
                )
                if not decision.allowed:
                    results.append(ToolResult(False, error=f"Execution skipped: {decision.reason}", summary=f"skipped {step.tool}.{step.action}"))
                    break

                result = await self.tools.call(step.tool, step.action, step.args, ctx)
                results.append(result)

                # Optional perception chain: screenshot file -> vision.ground.
                if result.ok and step.tool in {"computer", "ui_bridge"} and isinstance(result.data, dict):
                    image_path = result.data.get("image_path")
                    if image_path and "vision" in self.tools.names() and step.args.get("auto_ground", True):
                        vision_result = await self.tools.call("vision", "ground", {
                            "image_path": image_path,
                            "instruction": step.args.get("expected_result", plan.goal),
                            "expected_result": f"Ground screenshot for: {step.args.get('expected_result', plan.goal)}",
                            "task_id": plan.plan_id,
                        }, ctx)
                        results.append(vision_result)

                if not result.ok:
                    break

        except ApprovalRequired as approval:
            return {
                "status": "waiting_approval",
                "approval_id": approval.approval_id,
                "proposal": approval.proposal,
                "plan_id": plan.plan_id,
                "goal": plan.goal,
                "results": [self._sanitize_result(r) for r in results],
            }

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
            "status": "completed" if all(r.ok for r in results) else "failed",
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "rationale": plan.rationale,
            "steps": [s.__dict__ for s in plan.steps],
            "results": [self._sanitize_result(r) for r in results],
        }

    @staticmethod
    def _sanitize_result(result: ToolResult) -> dict:
        data = result.data
        if isinstance(data, dict):
            data = dict(data)
            for key in ["png_base64", "raw_html", "full_file_content"]:
                if key in data:
                    data.pop(key, None)
                    data[f"{key}_removed"] = True
            for key in ["stdout", "stderr"]:
                if isinstance(data.get(key), str) and len(data[key]) > 4000:
                    data[key] = data[key][:2000] + "\n...[TRUNCATED]...\n" + data[key][-2000:]
        return {"ok": result.ok, "summary": result.summary, "error": result.error, "data": data}
