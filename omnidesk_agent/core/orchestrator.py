from __future__ import annotations
from typing import Any, Optional

import asyncio
from dataclasses import asdict, replace
import json
import time

from omnidesk_agent.core.agent_loop import (
    ORIGIN_AUTO_CLICK,
    ORIGIN_AUTO_GROUND,
    ORIGIN_PLANNER,
    ORIGIN_VERIFY,
    TURN_DONE,
    TURN_GIVE_UP,
    LegacyPlannerAdapter,
    LoopBudget,
    LoopState,
    LoopView,
    PlannerTurn,
    PromptInjectionBlocked,
    StepRecord,
    TurnRecord,
    budget_exhausted,
    budget_left,
    finish_single_plan,
    history_for_planner,
    sanitize_tool_result,
)
from omnidesk_agent.core.models import ChannelMessage, Plan, PlanStep, ToolResult
from omnidesk_agent.core.planner_policy import PlannerPolicy, PolicyAssignment, RunOutcome
from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.core.serialization import message_from_dict, plan_from_dict
from omnidesk_agent.core.vision_executor import VisionActionExecutor
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.learning.experience_extractor import ExperienceExtractor
from omnidesk_agent.models.provider_errors import safe_error_text
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.core.execution_strategy import ResultOrientedExecutionStrategy
from omnidesk_agent.observability_tracing import trace_span


class Orchestrator:
    """Drives the agent loop: plan a turn, run its steps, show the results to the planner, repeat.

    Every step goes through the tool registry, so each tool's own permission
    check runs on every call; an approval request pauses the run with its whole
    loop state, and resume continues from the blocked step. The run store gets
    a checkpoint after every turn and every step.
    """

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
        loop_budget: Optional[LoopBudget] = None,
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
        self.loop_budget = loop_budget or LoopBudget()

    # ------------------------------------------------------------------ entry points

    async def handle_message(self, msg: ChannelMessage) -> dict:
        run_id = await asyncio.to_thread(self.run_store.create, asdict(msg)) if self.run_store else None
        state = LoopState.new(msg, run_id)
        self._assign(msg, state)
        self._metric("omnidesk_planner_requests_total", channel=msg.channel)
        return await self._run(msg, state)

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
        if not run["plan_json"]:
            return {"ok": False, "status": "missing_plan"}

        # A resume before anyone decided must not use up the token: that would
        # leave a run nobody can ever resume once the approval comes in.
        pending = await asyncio.to_thread(self._approval_pending, approval_id)
        if pending:
            return {"ok": False, "status": "approval_pending", "run_id": run_id, "approval_id": approval_id, "message": "the approval has not been decided yet"}

        state = self._restore_state(run)
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
            await asyncio.to_thread(self.run_store.update, run_id, {"status": "running", "waiting_approval_id": None})
        except PermissionError as exc:
            if resume_started and hasattr(self.run_store, "mark_resume_failed"):
                await asyncio.to_thread(self.run_store.mark_resume_failed, run_id, str(exc))
            if resume_started:
                state.note_safety_event("approval_denied")
                state.termination = {"reason": "approval_denied", "detail": safe_error_text(exc)[:2000]}
                self._observe(state, status="failed", final=True, error_class=type(exc).__name__)
            return {"ok": False, "status": "approval_not_satisfied", "approval_id": approval_id, "error": str(exc)}

        msg = message_from_dict(run["original_message"])
        return await self._run(msg, state)

    async def plan_only(self, msg: ChannelMessage, policy: Optional[PlannerPolicy] = None) -> PlannerTurn:
        """The first planner turn for `msg`, without running anything.

        For shadow experiments: no tool call, no run-store or memory write (memory
        is not read either, since reading it records last use). Model cost only.
        """
        state = LoopState.new(msg, None)
        if policy is not None:
            state.policy = policy.to_dict()
        return await self._turn_planner().next_turn(self._view(msg, state, retrieve_memory=False))

    # ------------------------------------------------------------------ the loop

    async def _run(self, msg: ChannelMessage, state: LoopState) -> dict:
        budget = self.loop_budget
        segment_started = time.time()
        base_ctx = ToolContext(permissions=self.permissions, source=msg.channel, actor=msg.sender_id, run_id=state.run_id, plan_id=state.plan_id)
        try:
            while True:
                if state.pending:
                    stop = budget_exhausted(state, budget, segment_started_at=segment_started, before="step")
                    if stop:
                        return await self._finish(msg, state, "failed", stop, segment_started)
                    step = state.step(state.pending[0])
                    derived = await self._execute_step(state, step, base_ctx)
                    state.pending.pop(0)
                    if step.status == "done":
                        state.consecutive_failures = 0
                        state.pending[0:0] = [record.index for record in derived]
                    else:
                        # Stop this turn's remaining steps; the planner sees the failure next.
                        state.consecutive_failures += 1
                        state.pending.clear()
                    await self._checkpoint(state)
                    continue

                if state.single_plan:
                    final = finish_single_plan(state)
                    state.final_answer = final.final_answer
                    if final.status == TURN_DONE:
                        return await self._finish(msg, state, "completed", "goal_reached", segment_started)
                    return await self._finish(msg, state, "failed", "gave_up", segment_started, detail=final.final_answer)

                stop = budget_exhausted(state, budget, segment_started_at=segment_started, before="turn")
                if stop:
                    return await self._finish(msg, state, "failed", stop, segment_started)
                try:
                    turn = await self._plan_turn(msg, state)
                except Exception as exc:
                    self._metric("omnidesk_planner_results_total", channel=msg.channel, status="error")
                    blocked = isinstance(exc, PermissionError)
                    if blocked:
                        state.note_safety_event("prompt_injection_blocked" if isinstance(exc, PromptInjectionBlocked) else "permission_denied")
                    result = await self._finish(msg, state, "failed", "blocked" if blocked else "planner_error", segment_started, error=exc)
                    if state.steps_executed == 0:
                        raise  # nothing ran yet: callers (and job retries) see the error, as before
                    return result
                self._metric("omnidesk_planner_results_total", channel=msg.channel, status="ok")
                self._record_turn(msg, state, turn)
                if turn.status == TURN_DONE:
                    state.final_answer = turn.final_answer
                    return await self._finish(msg, state, "completed", "goal_reached", segment_started)
                if turn.status == TURN_GIVE_UP:
                    state.final_answer = turn.final_answer
                    return await self._finish(msg, state, "failed", "gave_up", segment_started, detail=turn.final_answer)
                if turn.errors:
                    state.consecutive_failures += 1
                await self._checkpoint(state)
        except ApprovalRequired as approval:
            return await self._pause(msg, state, approval, segment_started)

    def _view(self, msg: ChannelMessage, state: LoopState, *, retrieve_memory: bool = True) -> LoopView:
        return LoopView(
            msg=msg,
            run_id=state.run_id,
            turn_index=len(state.turns) + 1,
            history=history_for_planner(state, self.loop_budget),
            last_errors=list(state.last_errors),
            budget_left=budget_left(state, self.loop_budget),
            max_steps_per_turn=self.loop_budget.max_steps_per_turn,
            policy=state.planner_policy(),
            retrieve_memory=retrieve_memory,
        )

    def _turn_planner(self) -> Any:
        return self.planner if callable(getattr(self.planner, "next_turn", None)) else LegacyPlannerAdapter(self.planner)

    async def _plan_turn(self, msg: ChannelMessage, state: LoopState) -> PlannerTurn:
        view = self._view(msg, state)
        with trace_span("planner.plan", metrics=getattr(self, "metrics", None), otel_exporter=getattr(self, "otel_exporter", None), channel=msg.channel, actor=msg.sender_id, run_id=state.run_id, turn=view.turn_index):
            return await self._turn_planner().next_turn(view)

    def _record_turn(self, msg: ChannelMessage, state: LoopState, turn: PlannerTurn) -> TurnRecord:
        record = TurnRecord(
            index=len(state.turns) + 1,
            status=turn.status,
            source=turn.source,
            goal=turn.goal,
            rationale=turn.rationale,
            final_answer=turn.final_answer[:4000],
            success_criteria=turn.success_criteria[:1000],
            llm_calls=turn.llm_calls,
            cost_usd=turn.cost_usd,
            cost_known=turn.cost_known,
            errors=list(turn.errors),
            warnings=list(turn.warnings),
        )
        state.turns.append(record)
        state.llm_calls += turn.llm_calls
        state.cost_usd += turn.cost_usd
        state.cost_known = state.cost_known and turn.cost_known
        state.last_errors = list(turn.errors)
        if turn.status not in {TURN_DONE, TURN_GIVE_UP} and not turn.errors:
            steps = turn.steps[: self.loop_budget.max_steps_per_turn]
            if len(turn.steps) > len(steps):
                record.warnings.append(f"only the first {len(steps)} of {len(turn.steps)} steps were accepted")
            for step in steps:
                added = state.add_step(step, turn=record.index)
                record.step_indexes.append(added.index)
                state.pending.append(added.index)
        self._metric("omnidesk_agent_turns_total", status=turn.status, source=turn.source)
        self._audit_event("agent_turn_planned", {
            "run_id": state.run_id,
            "plan_id": state.plan_id,
            "turn": record.index,
            "status": record.status,
            "source": record.source,
            "steps": [f"{state.step(i).tool}.{state.step(i).action}" for i in record.step_indexes],
            "errors": len(record.errors),
            "llm_calls": record.llm_calls,
            "policy_id": (state.policy or {}).get("policy_id"),
        })
        if record.index == 1:
            self._after_first_plan(msg, state, turn)
        return record

    # ------------------------------------------------------------------ steps

    async def _execute_step(self, state: LoopState, step: StepRecord, base_ctx: ToolContext) -> list[StepRecord]:
        """Run one step and record its result. Returns the steps it adds. Raises ApprovalRequired."""
        ctx = replace(base_ctx, step_index=step.index)
        args = step.args
        if "expected_result" not in args:
            args["expected_result"] = step.description or state.goal
        scope = state.run_id or state.plan_id

        decision = self.execution_strategy.decide_tool_step(tool=step.tool, action=step.action, args=args, goal=state.goal, scope=scope)
        if not decision.allowed:
            state.note_safety_event("execution_strategy_blocked")
            self._store_result(state, step, ToolResult(False, error=f"Execution skipped: {decision.reason}", summary=f"skipped {step.tool}.{step.action}"))
            return []

        try:
            with trace_span("tool.call", metrics=getattr(self, "metrics", None), otel_exporter=getattr(self, "otel_exporter", None), run_id=state.run_id, plan_id=state.plan_id, step_index=step.index, tool=step.tool, action=step.action):
                result = await self._dispatch(state, step, args, ctx)
        except ApprovalRequired:
            raise
        except Exception as exc:
            # Raised outside the registry's own handling (a direct permission check, a vision helper).
            result = ToolResult(False, data={"error_class": type(exc).__name__, "permission_denied": isinstance(exc, PermissionError)}, error=f"{type(exc).__name__}: {safe_error_text(exc)}", summary=f"step {step.index} raised")
        self._store_result(state, step, result)
        if not result.ok:
            return []
        note_state_change = getattr(self.execution_strategy, "note_state_change", None)
        if callable(note_state_change) and not ResultOrientedExecutionStrategy.is_expensive(step.tool, step.action):
            note_state_change(scope)
        return self._derived_steps(state, step, result)

    async def _dispatch(self, state: LoopState, step: StepRecord, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if step.origin == ORIGIN_VERIFY:
            return await self.vision_executor.verify_with_retry(
                ctx=ctx,
                instruction=str(args.get("instruction") or state.goal),
                verification=dict(args.get("verification") or {}),
                retry_policy=args.get("retry_policy"),
            )
        if step.origin == ORIGIN_AUTO_CLICK and step.parent is not None:
            ground = state.step(step.parent)
            shot = state.step(ground.parent) if ground.parent is not None else None
            ground_result = ground.result or {}
            grounding = ToolResult(bool(ground_result.get("ok")), data=ground_result.get("data"), summary=ground_result.get("summary"))
            clicked = await self.vision_executor.maybe_click_target(grounding, str(args.get("instruction") or state.goal), ctx, screenshot_metadata=(shot.result or {}).get("data") if shot else None)
            return clicked if clicked is not None else ToolResult(True, data={"clicked": False}, summary="no grounded target to click")
        return await self.tools.call(step.tool, step.action, args, ctx)

    def _derived_steps(self, state: LoopState, step: StepRecord, result: ToolResult) -> list[StepRecord]:
        """Screenshot grounding, grounded clicks and vision verification run as their own steps,
        so an approval pause on one of them resumes that step, never the action before it."""
        names = set(self.tools.names()) if callable(getattr(self.tools, "names", None)) else set()
        expected = str(step.args.get("expected_result") or state.goal)
        if step.origin == ORIGIN_AUTO_GROUND:
            parent = state.step(step.parent) if step.parent is not None else None
            if parent is not None and parent.args.get("auto_click_grounded", False) and "computer" in names:
                click = PlanStep("click the grounded target", "computer", "click_grounded", {"instruction": str(parent.args.get("expected_result") or state.goal), "expected_result": f"Click grounded target for: {parent.args.get('expected_result') or state.goal}"}, "high", True)
                return [state.add_step(click, turn=step.turn, origin=ORIGIN_AUTO_CLICK, parent=step.index)]
            return []
        if step.origin != ORIGIN_PLANNER:
            return []
        derived: list[StepRecord] = []
        data = result.data if isinstance(result.data, dict) else {}
        if step.tool in {"computer", "ui_bridge"} and data.get("image_path") and "vision" in names and step.args.get("auto_ground", True):
            ground = PlanStep("ground the screenshot", "vision", "ground", {"image_path": data["image_path"], "instruction": expected, "expected_result": f"Ground screenshot for: {expected}"}, "medium", True)
            derived.append(state.add_step(ground, turn=step.turn, origin=ORIGIN_AUTO_GROUND, parent=step.index))
        verification = step.args.get("verification")
        if verification and "vision" in names and "computer" in names:
            verify = PlanStep("verify the step result", "vision", "verify", {"instruction": expected, "verification": verification, "retry_policy": step.args.get("retry_policy"), "expected_result": f"Verify: {expected}"}, "medium", True)
            derived.append(state.add_step(verify, turn=step.turn, origin=ORIGIN_VERIFY, parent=step.index))
        return derived

    def _store_result(self, state: LoopState, step: StepRecord, result: ToolResult) -> None:
        step.result = sanitize_tool_result(result.ok, result.data, result.error, result.summary)
        step.status = "done" if result.ok else "failed"
        data = result.data if isinstance(result.data, dict) else {}
        step.error_class = None if result.ok else (str(data.get("error_class")) if data.get("error_class") else None)
        if not result.ok and data.get("permission_denied"):
            state.note_safety_event("permission_denied")
        usage = data.get("model_usage") if isinstance(data.get("model_usage"), dict) else None
        if usage is not None:
            state.tool_model_calls += int(usage.get("calls") or 1)
            state.cost_usd += float(usage.get("cost_usd") or 0.0)
            state.cost_known = state.cost_known and bool(usage.get("cost_known"))
        elif step.tool == "vision":
            # A vision step (grounding, or a verification of several) that does not
            # report its model cost makes the run's cost unknown.
            state.cost_known = False
        state.order.append(step.index)
        self._audit_event("agent_step_finished", {
            "run_id": state.run_id,
            "plan_id": state.plan_id,
            "step_index": step.index,
            "turn": step.turn,
            "origin": step.origin,
            "tool": step.tool,
            "action": step.action,
            "ok": result.ok,
            "error_class": step.error_class,
        })

    # ------------------------------------------------------------------ pause, finish, persistence

    async def _checkpoint(self, state: LoopState) -> None:
        if self.run_store is None or state.run_id is None:
            return
        current = state.pending[0] if state.pending else (state.order[-1] if state.order else 0)
        await asyncio.to_thread(self.run_store.update, state.run_id, {
            "status": "running",
            "plan_json": json.dumps(state.plan_json(), ensure_ascii=False, default=str),
            "current_step_index": current,
            "results_json": json.dumps(state.results(), ensure_ascii=False, default=str),
        })

    async def _pause(self, msg: ChannelMessage, state: LoopState, approval: ApprovalRequired, segment_started: float) -> dict:
        state.active_seconds += time.time() - segment_started
        self._metric("omnidesk_approval_waiting_runs_total", tool=str(approval.proposal.get("tool", "")) if isinstance(approval.proposal, dict) else "unknown")
        blocked = state.pending[0] if state.pending else 0
        results = state.results()
        resume_token = None
        if self.run_store and state.run_id:
            resume_token = await asyncio.to_thread(self.run_store.save_waiting, state.run_id, state.plan_json(), blocked, results, approval.approval_id, approval.proposal)
        state.termination = {"reason": "approval_wait", "detail": approval.approval_id}
        self._observe(state, status="waiting_approval", final=False)
        return {
            "status": "waiting_approval",
            "run_id": state.run_id,
            "resume_token": resume_token,
            "approval_id": approval.approval_id,
            "proposal": approval.proposal,
            "plan_id": state.plan_id,
            "goal": state.goal,
            "current_step_index": blocked,
            "results": results,
            "loop": self._loop_summary(state),
            "learning_assignment": state.learning_assignment,
        }

    async def _finish(self, msg: ChannelMessage, state: LoopState, status: str, reason: str, segment_started: float, *, detail: str = "", error: Optional[BaseException] = None) -> dict:
        state.active_seconds += time.time() - segment_started
        state.termination = {"reason": reason, "detail": detail[:2000]}
        if error is not None:
            state.termination["error"] = f"{type(error).__name__}: {safe_error_text(error)}"[:2000]
        results = state.results()
        if status != "completed" and not results:
            # Nothing ran: say why in the results, so the run row explains itself.
            if error is not None:
                results.append({"ok": False, "summary": "planning failed", "error": state.termination["error"], "data": {"stage": "planning"}})
            else:
                rationale = state.turns[0].rationale if state.turns else ""
                results.append({"ok": False, "summary": (rationale or reason)[:2000], "error": detail or reason, "data": {"stage": "planning"}})
        if self.run_store and state.run_id:
            try:
                await self._checkpoint(state)
                await asyncio.to_thread(self.run_store.complete, state.run_id, status, results)
            except Exception as store_exc:
                if error is not None:
                    error.add_note(f"run {state.run_id} could not be marked {status}: {type(store_exc).__name__}: {store_exc}")
                raise
        self._record_memory(msg, state, status, results)
        self._observe(state, status=status, final=True, error_class=type(error).__name__ if error is not None else None)
        self._metric("omnidesk_agent_runs_total", channel=msg.channel, status=status)
        self._metric("omnidesk_agent_terminations_total", reason=reason)
        self._audit_event("agent_run_finished", {"run_id": state.run_id, "plan_id": state.plan_id, "status": status, "termination": reason, **self._loop_summary(state)})
        first = state.turns[0] if state.turns else None
        return {
            "status": status,
            "run_id": state.run_id,
            "resume_token": None,
            "plan_id": state.plan_id,
            "goal": (first.goal if first and first.goal else state.goal),
            "rationale": first.rationale if first else "",
            "final_answer": state.final_answer,
            "termination": dict(state.termination),
            "steps": [asdict(step.plan_step()) for step in state.steps if step.origin == ORIGIN_PLANNER],
            "results": results,
            "loop": self._loop_summary(state),
            "planner_policy": (state.policy or {}).get("policy_id"),
            "learning_assignment": state.learning_assignment,
        }

    def _restore_state(self, run: dict[str, Any]) -> LoopState:
        plan_json = run["plan_json"] or {}
        loop = plan_json.get("agent_loop") if isinstance(plan_json, dict) else None
        if isinstance(loop, dict):
            return LoopState.from_dict(loop)
        # Saved by a release without the loop: finish its one plan from the saved step.
        plan: Plan = plan_from_dict(plan_json)
        state = LoopState(goal=plan.goal, plan_id=plan.plan_id, run_id=str(run["id"]))
        state.prior_results = list(run.get("results") or [])
        turn = TurnRecord(index=1, status="continue", source="legacy", goal=plan.goal, rationale=plan.rationale)
        state.turns.append(turn)
        start = int(run.get("current_step_index") or 0)
        for position, step in enumerate(plan.steps):
            record = state.add_step(step, turn=1)
            turn.step_indexes.append(record.index)
            if position >= start:
                state.pending.append(record.index)
            else:
                record.status = "done"
        return state

    def _approval_pending(self, approval_id: str) -> bool:
        get = getattr(self.approval_store, "get", None)
        approval = get(approval_id) if callable(get) else None
        if not isinstance(approval, dict) or approval.get("status") != "pending":
            return False
        expires_at = approval.get("expires_at")
        return not (expires_at and time.time() > float(expires_at))

    def _loop_summary(self, state: LoopState) -> dict[str, Any]:
        return {
            "turns": len(state.turns),
            "steps_executed": state.steps_executed,
            "llm_calls": state.llm_calls,
            "model_calls": state.llm_calls + state.tool_model_calls,
            "cost_usd": round(state.cost_usd, 6) if state.cost_known else None,
            "replans": max(0, len(state.turns) - 1),
            "active_seconds": round(state.active_seconds, 3),
        }

    def _record_memory(self, msg: ChannelMessage, state: LoopState, status: str, results: list[dict[str, Any]]) -> None:
        planner_steps = [step for step in state.steps if step.origin == ORIGIN_PLANNER]
        compact_steps = [{"description": s.description, "tool": s.tool, "action": s.action, "risk": s.risk, "args_keys": sorted(s.args.keys())} for s in planner_steps]
        outcome = "\n".join([r.get("summary") or r.get("error") or "" for r in results])
        try:
            # Inside the try: the run is already recorded, and a memory error must
            # not turn it into an exception that makes a job retry its side effects.
            self.memory.add(task=msg.text[:1000], plan=json.dumps(compact_steps, ensure_ascii=False), outcome=outcome[:2000], tags=[msg.channel], channel=msg.channel, actor=msg.sender_id)
            structured = self.experience_extractor.extract(
                task=msg.text,
                plan={"steps": compact_steps},
                run_result={
                    "status": status,
                    "run_id": state.run_id,
                    "plan_id": state.plan_id,
                    "goal": state.goal,
                    "steps": [asdict(s.plan_step()) for s in planner_steps],
                    "results": results,
                    "learning_assignment": state.learning_assignment,
                },
                tags=[msg.channel],
            )
            self.memory.add_experience(structured, channel=msg.channel, actor=msg.sender_id)
            self.memory.record_metric(
                success=status == "completed",
                manual_intervention=any("approval" in str(r).lower() for r in results),
                tool_error=any((not r.get("ok")) and r.get("error") for r in results),
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
                    manual_intervention=any("approval" in str(r).lower() for r in results),
                    safety_violation=structured.get("failure_reason") == "security_violation",
                )
        except Exception:
            # Learning failures must never break task execution.
            pass

    # ------------------------------------------------------------------ learning hook, audit, metrics

    def _assign(self, msg: ChannelMessage, state: LoopState) -> None:
        hook = self.learning_loop
        if hook is None:
            return
        try:
            if callable(getattr(hook, "assign", None)) and callable(getattr(hook, "observe", None)):
                state.assignment_protocol = "hook"
                assignment = hook.assign(msg)
                if isinstance(assignment, PolicyAssignment):
                    state.learning_assignment = assignment.to_dict()
                    state.policy = assignment.policy.to_dict() if assignment.policy else None
            elif callable(getattr(hook, "assign_policy", None)):
                # Transitional: the pre-hook RuntimeLearningLoop assigns arms but has no policy.
                state.assignment_protocol = "legacy"
                state.learning_assignment = hook.assign_policy(msg)
        except Exception:
            state.learning_assignment = None
            state.policy = None

    def _after_first_plan(self, msg: ChannelMessage, state: LoopState, turn: PlannerTurn) -> None:
        after_plan = getattr(self.learning_loop, "after_plan", None)
        if state.assignment_protocol != "hook" or not callable(after_plan):
            return
        try:
            assignment = PolicyAssignment.from_dict(state.learning_assignment) if state.learning_assignment else None
            after_plan(msg, assignment, turn)
        except Exception:
            pass

    def _observe(self, state: LoopState, *, status: str, final: bool, error_class: Optional[str] = None) -> None:
        hook = self.learning_loop
        if hook is None or state.assignment_protocol == "none":
            return
        reason = str((state.termination or {}).get("reason") or "")
        outcome_status = reason if reason in {"planner_error", "blocked"} else status
        latency_ms = (time.time() - state.started_at) * 1000.0
        try:
            if state.assignment_protocol == "hook":
                assignment = PolicyAssignment.from_dict(state.learning_assignment) if state.learning_assignment else None
                hook.observe(assignment, RunOutcome(
                    run_id=state.run_id,
                    status=outcome_status,
                    final=final,
                    termination=reason,
                    latency_ms=latency_ms,
                    model_cost_usd=round(state.cost_usd, 6) if state.cost_known else None,
                    model_calls=state.llm_calls + state.tool_model_calls,
                    replans=max(0, len(state.turns) - 1),
                    steps_executed=state.steps_executed,
                    safety_events=tuple(state.safety_events),
                    error_class=error_class,
                ))
            elif final:
                hook.observe_result(state.learning_assignment, status=status, result_count=len(state.results()), cost=state.cost_usd, latency_ms=latency_ms, safety_violation=bool(state.safety_events))
        except Exception:
            # Learning failures must never break task execution.
            pass

    def _audit_event(self, event: str, payload: dict[str, Any]) -> None:
        # Hash-chained entry for loop lifecycle events, where the permission
        # manager provides it; tool permission decisions are audited by the tools.
        record = getattr(self.permissions, "record_event", None)
        if callable(record):
            record(event, payload)

    def _metric(self, name: str, **labels: Any) -> None:
        metrics = getattr(self, "metrics", None)
        inc = getattr(metrics, "inc", None)
        if callable(inc):
            inc(name, **labels)
