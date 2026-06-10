from __future__ import annotations

import json
from omnidesk_agent.core.models import ChannelMessage, Plan, ToolResult
from omnidesk_agent.core.planner import HierarchicalPlanner
from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.core.vision_executor import VisionActionExecutor
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
        run_store: RunStore | None = None,
    ):
        self.planner = planner
        self.tools = tools
        self.permissions = permissions
        self.memory = memory
        self.execution_strategy = execution_strategy or ResultOrientedExecutionStrategy()
        self.run_store = run_store
        self.vision_executor = VisionActionExecutor(tools)

    async def handle_message(self, msg: ChannelMessage) -> dict:
        run_id = self.run_store.create(msg.__dict__) if self.run_store else None
        plan = await self.planner.plan(msg)
        return await self._execute_plan(msg, plan, run_id=run_id, start_index=0, prior_results=[])

    async def resume(self, run_id: str) -> dict:
        if self.run_store is None:
            return {"ok": False, "status": "resume_unavailable", "message": "RunStore is not configured"}
        run = self.run_store.get(run_id)
        if not run:
            return {"ok": False, "status": "not_found", "run_id": run_id}
        if run["status"] != "waiting_approval":
            return {"ok": False, "status": run["status"], "message": "run is not waiting for approval"}

        original = run["original_message"]
        msg = ChannelMessage(**original)
        plan_dict = run["plan_json"]
        if not plan_dict:
            return {"ok": False, "status": "missing_plan"}
        plan = Plan(**plan_dict)
        return await self._execute_plan(msg, plan, run_id=run_id, start_index=int(run["current_step_index"]), prior_results=run["results"])

    async def _execute_plan(self, msg: ChannelMessage, plan: Plan, *, run_id: str | None, start_index: int, prior_results: list[dict]) -> dict:
        ctx = ToolContext(permissions=self.permissions, source=msg.channel, actor=msg.sender_id)
        results: list[ToolResult] = []
        sanitized_prior = list(prior_results)

        try:
            for idx, step in enumerate(plan.steps[start_index:], start=start_index):
                if "expected_result" not in step.args:
                    step.args["expected_result"] = step.description or plan.goal

                decision = self.execution_strategy.decide_tool_step(tool=step.tool, action=step.action, args=step.args, goal=plan.goal)
                if not decision.allowed:
                    results.append(ToolResult(False, error=f"Execution skipped: {decision.reason}", summary=f"skipped {step.tool}.{step.action}"))
                    break

                result = await self.tools.call(step.tool, step.action, step.args, ctx)
                results.append(result)

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

                        if step.args.get("auto_click_grounded", False):
                            click_result = await self.vision_executor.maybe_click_target(vision_result, step.args.get("expected_result", plan.goal), ctx)
                            if click_result is not None:
                                results.append(click_result)

                if not result.ok:
                    break

        except ApprovalRequired as approval:
            all_results = sanitized_prior + [self._sanitize_result(r) for r in results]
            if self.run_store and run_id:
                self.run_store.save_waiting(
                    run_id,
                    plan.__dict__,
                    start_index + len(results),
                    all_results,
                    approval.approval_id,
                )
            return {
                "status": "waiting_approval",
                "run_id": run_id,
                "approval_id": approval.approval_id,
                "proposal": approval.proposal,
                "plan_id": plan.plan_id,
                "goal": plan.goal,
                "results": all_results,
            }

        all_results = sanitized_prior + [self._sanitize_result(r) for r in results]
        status = "completed" if all(r["ok"] for r in all_results) else "failed"
        if self.run_store and run_id:
            self.run_store.complete(run_id, status, all_results)

        outcome = "\n".join([r.get("summary") or r.get("error") or "" for r in all_results])
        compact_steps = [{"description": s.description, "tool": s.tool, "action": s.action, "risk": s.risk, "args_keys": sorted(s.args.keys())} for s in plan.steps]
        self.memory.add(task=msg.text[:1000], plan=json.dumps(compact_steps, ensure_ascii=False), outcome=outcome[:2000], tags=[msg.channel])

        return {
            "status": status,
            "run_id": run_id,
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "rationale": plan.rationale,
            "steps": [s.__dict__ for s in plan.steps],
            "results": all_results,
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
