from __future__ import annotations
from typing import Any, Optional

import asyncio
from dataclasses import asdict
import json

from omnidesk_agent.core.models import ChannelMessage, Plan, ToolResult
from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.core.serialization import message_from_dict, plan_from_dict
from omnidesk_agent.core.vision_executor import VisionActionExecutor
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.learning.experience_extractor import ExperienceExtractor
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.core.execution_strategy import ResultOrientedExecutionStrategy
from omnidesk_agent.observability_tracing import trace_span


class Orchestrator:
    def __init__(
        self,
        planner: Any,
        tools: ToolRegistry,
        permissions: PermissionManager,
        memory: ExperienceStore,
        execution_strategy: Optional[ResultOrientedExecutionStrategy] = None,
        run_store: Optional[RunStore] = None,
        approval_store: Optional[ApprovalStore] = None,
        learning_loop: Optional[Any] = None,
        dual_approval_store: Optional[Any] = None,
    ):
        self.planner = planner
        self.tools = tools
        self.permissions = permissions
        self.memory = memory
        self.execution_strategy = execution_strategy or ResultOrientedExecutionStrategy()
        self.run_store = run_store
        self.approval_store = approval_store
        self.vision_executor = VisionActionExecutor(tools)
        self.experience_extractor = ExperienceExtractor()
        self.metrics: Any = None
        self.otel_exporter: Any = None
        self.storage_plan: Any = None
        self.learning_loop = learning_loop
        self.dual_approval_store = dual_approval_store

    async def handle_message(self, msg: ChannelMessage) -> dict:
        run_id = await asyncio.to_thread(self.run_store.create, asdict(msg)) if self.run_store else None
        learning_assignment = None
        if self.learning_loop is not None:
            try:
                learning_assignment = self.learning_loop.assign_policy(msg)
            except Exception:
                learning_assignment = None
        self._metric("omnidesk_planner_requests_total", channel=msg.channel)
        try:
            with trace_span("planner.plan", metrics=getattr(self, "metrics", None), otel_exporter=getattr(self, "otel_exporter", None), channel=msg.channel, actor=msg.sender_id, run_id=run_id):
                plan = await self.planner.plan(msg)
            self._metric("omnidesk_planner_results_total", channel=msg.channel, status="ok")
        except Exception:
            self._metric("omnidesk_planner_results_total", channel=msg.channel, status="error")
            raise
        return await self._execute_plan(msg, plan, run_id=run_id, start_index=0, prior_results=[], learning_assignment=learning_assignment)

    async def resume(self, run_id: str, resume_token: Optional[str] = None) -> dict:
        if self.run_store is None:
            return {"ok": False, "status": "resume_unavailable", "message": "RunStore is not configured"}
        run = await asyncio.to_thread(self.run_store.get, run_id)
        if not run:
            return {"ok": False, "status": "not_found", "run_id": run_id}
        try:
            await asyncio.to_thread(self.run_store.require_resume_token, run_id, resume_token)
        except (KeyError, PermissionError) as exc:
            return {"ok": False, "status": "resume_denied", "run_id": run_id, "error": str(exc)}
        if run["status"] != "waiting_approval":
            return {"ok": False, "status": run["status"], "message": "run is not waiting for approval"}

        approval_id = run.get("waiting_approval_id")
        if not approval_id:
            return {"ok": False, "status": "missing_approval_id"}
        if self.approval_store is None:
            return {"ok": False, "status": "approval_store_unavailable"}

        resume_started = False
        try:
            with trace_span("orchestrator.resume.consume_token", metrics=getattr(self, "metrics", None), otel_exporter=getattr(self, "otel_exporter", None), run_id=run_id, approval_id=approval_id):
                await asyncio.to_thread(self.run_store.consume_resume_token, run_id, resume_token)
            resume_started = True
            proposal = run.get("approval_proposal") or {}
            if proposal.get("requires_dual_approval"):
                dual_store = getattr(self.approval_store, "dual_approval_store", None) or getattr(self, "dual_approval_store", None)
                if dual_store is None:
                    raise PermissionError("dual approval store is required for critical approval resume")
                dual_decision = await asyncio.to_thread(dual_store.status, approval_id)
                if not dual_decision.ready:
                    raise PermissionError(f"dual approval is not satisfied: {dual_decision.reason}")
            if hasattr(self.approval_store, "consume_approved"):
                await asyncio.to_thread(self.approval_store.consume_approved, approval_id, proposal, consumed_by_run_id=run_id)
            else:
                await asyncio.to_thread(self.approval_store.require_approved, approval_id, proposal)
            if hasattr(self.permissions, "allow_approved_proposal"):
                await asyncio.to_thread(self.permissions.allow_approved_proposal, run.get("approval_proposal") or {})
            if self.run_store:
                await asyncio.to_thread(self.run_store.update, run_id, {"status": "running", "waiting_approval_id": None})
        except PermissionError as exc:
            if resume_started and hasattr(self.run_store, "mark_resume_failed"):
                await asyncio.to_thread(self.run_store.mark_resume_failed, run_id, str(exc))
            return {"ok": False, "status": "approval_not_satisfied", "approval_id": approval_id, "error": str(exc)}

        msg = message_from_dict(run["original_message"])
        if not run["plan_json"]:
            return {"ok": False, "status": "missing_plan"}
        plan = plan_from_dict(run["plan_json"])
        return await self._execute_plan(msg, plan, run_id=run_id, start_index=int(run["current_step_index"]), prior_results=run["results"], learning_assignment=None)

    async def _execute_plan(self, msg: ChannelMessage, plan: Plan, *, run_id: Optional[str], start_index: int, prior_results: list[dict], learning_assignment: Optional[dict[str, Any]] = None) -> dict:
        ctx = ToolContext(permissions=self.permissions, source=msg.channel, actor=msg.sender_id, run_id=run_id, plan_id=plan.plan_id)
        results: list[ToolResult] = []
        sanitized_prior = list(prior_results)
        current_step_index = start_index

        try:
            for idx, step in enumerate(plan.steps[start_index:], start=start_index):
                current_step_index = idx
                ctx.step_index = idx

                if "expected_result" not in step.args:
                    step.args["expected_result"] = step.description or plan.goal

                decision = self.execution_strategy.decide_tool_step(tool=step.tool, action=step.action, args=step.args, goal=plan.goal)
                if not decision.allowed:
                    results.append(ToolResult(False, error=f"Execution skipped: {decision.reason}", summary=f"skipped {step.tool}.{step.action}"))
                    break

                with trace_span(
                    "tool.call",
                    metrics=getattr(self, "metrics", None),
                    otel_exporter=getattr(self, "otel_exporter", None),
                    run_id=run_id,
                    plan_id=plan.plan_id,
                    step_index=idx,
                    tool=step.tool,
                    action=step.action,
                ):
                    result = await self.tools.call(step.tool, step.action, step.args, ctx)
                results.append(result)

                if result.ok and step.tool in {"computer", "ui_bridge"} and isinstance(result.data, dict):
                    image_path = result.data.get("image_path")
                    if image_path and "vision" in self.tools.names() and step.args.get("auto_ground", True):
                        with trace_span("tool.call", metrics=getattr(self, "metrics", None), otel_exporter=getattr(self, "otel_exporter", None), run_id=run_id, plan_id=plan.plan_id, step_index=idx, tool="vision", action="ground"):
                            vision_result = await self.tools.call("vision", "ground", {
                                "image_path": image_path,
                                "instruction": step.args.get("expected_result", plan.goal),
                                "expected_result": f"Ground screenshot for: {step.args.get('expected_result', plan.goal)}",
                                "task_id": plan.plan_id,
                            }, ctx)
                        results.append(vision_result)

                        if step.args.get("auto_click_grounded", False):
                            click_result = await self.vision_executor.maybe_click_target(
                                vision_result,
                                step.args.get("expected_result", plan.goal),
                                ctx,
                                screenshot_metadata=result.data,
                            )
                            if click_result is not None:
                                results.append(click_result)

                verification = step.args.get("verification")
                if verification and "vision" in self.tools.names() and "computer" in self.tools.names():
                    verify_result = await self.vision_executor.verify_with_retry(
                        ctx=ctx,
                        instruction=step.args.get("expected_result", plan.goal),
                        verification=verification,
                        retry_policy=step.args.get("retry_policy"),
                    )
                    results.append(verify_result)
                    if not verify_result.ok:
                        break

                if not result.ok:
                    break

        except ApprovalRequired as approval:
            self._metric("omnidesk_approval_waiting_runs_total", tool=str(approval.proposal.get("tool", "")) if isinstance(approval.proposal, dict) else "unknown")
            all_results = sanitized_prior + [self._sanitize_result(r) for r in results]
            if self.run_store and run_id:
                resume_token = await asyncio.to_thread(
                    self.run_store.save_waiting,
                    run_id,
                    asdict(plan),
                    current_step_index,
                    all_results,
                    approval.approval_id,
                    approval.proposal,
                )
            else:
                resume_token = None
            return {
                "status": "waiting_approval",
                "run_id": run_id,
                "resume_token": resume_token,
                "approval_id": approval.approval_id,
                "proposal": approval.proposal,
                "plan_id": plan.plan_id,
                "goal": plan.goal,
                "current_step_index": current_step_index,
                "results": all_results,
            "learning_assignment": learning_assignment,
            }

        all_results = sanitized_prior + [self._sanitize_result(r) for r in results]
        status = "completed" if all(r["ok"] for r in all_results) else "failed"
        if self.run_store and run_id:
            await asyncio.to_thread(self.run_store.complete, run_id, status, all_results)
        resume_token = None

        outcome = "\n".join([r.get("summary") or r.get("error") or "" for r in all_results])
        compact_steps = [{"description": s.description, "tool": s.tool, "action": s.action, "risk": s.risk, "args_keys": sorted(s.args.keys())} for s in plan.steps]
        self.memory.add(task=msg.text[:1000], plan=json.dumps(compact_steps, ensure_ascii=False), outcome=outcome[:2000], tags=[msg.channel])
        try:
            structured = self.experience_extractor.extract(
                task=msg.text,
                plan={"steps": compact_steps},
                run_result={
                    "status": status,
                    "run_id": run_id,
                    "plan_id": plan.plan_id,
                    "goal": plan.goal,
                    "steps": [asdict(s) for s in plan.steps],
                    "results": all_results,
            "learning_assignment": learning_assignment,
                },
                tags=[msg.channel],
            )
            self.memory.add_experience(structured)
            self.memory.record_metric(
                success=status == "completed",
                manual_intervention=any("approval" in str(r).lower() for r in all_results),
                tool_error=any((not r.get("ok")) and r.get("error") for r in all_results),
                repeat_failure=bool(structured.get("failure_reason") and structured.get("failure_reason") != "unknown"),
                skill_reuse=bool(structured.get("reusable_skill")),
                security_violation=structured.get("failure_reason") == "security_violation",
            )
            record_interaction = getattr(self.memory, "record_interaction_profile", None)
            if callable(record_interaction):
                record_interaction(
                    channel=msg.channel,
                    actor=msg.sender_id,
                    task=msg.text,
                    status=status,
                    manual_intervention=any("approval" in str(r).lower() for r in all_results),
                    safety_violation=structured.get("failure_reason") == "security_violation",
                )
        except Exception:
            # Learning failures must never break task execution.
            pass

        if self.learning_loop is not None:
            try:
                self.learning_loop.observe_result(learning_assignment, status=status, result_count=len(all_results), safety_violation=any("security" in str(r).lower() for r in all_results))
            except Exception:
                pass

        self._metric("omnidesk_agent_runs_total", channel=msg.channel, status=status)

        return {
            "status": status,
            "run_id": run_id,
            "resume_token": resume_token,
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "rationale": plan.rationale,
            "steps": [asdict(s) for s in plan.steps],
            "results": all_results,
            "learning_assignment": learning_assignment,
        }

    def _metric(self, name: str, **labels: Any) -> None:
        metrics = getattr(self, "metrics", None)
        inc = getattr(metrics, "inc", None)
        if callable(inc):
            inc(name, **labels)

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
