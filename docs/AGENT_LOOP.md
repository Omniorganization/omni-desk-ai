# Agent loop

`Orchestrator` (`omnidesk_agent/core/orchestrator.py`) runs every task as a loop of planner turns:

```text
task -> plan turn -> run its steps (permission check per step) -> observe results -> plan the next turn -> ... -> done | give_up | budget
```

## A turn

The planner returns one JSON object per turn (`PlannerTurnSchema` in `core/plan_schema.py`):

- `continue` with the next steps: only steps whose arguments the model can fully determine now. A step that needs an earlier step's output belongs to a later turn.
- `done` with `final_answer`: the observed results meet the success criteria. Ends the run as `completed`.
- `give_up` with `final_answer`: the task cannot be done with the available tools. Ends the run as `failed`.

A plain `StructuredPlan` object (no `status`) parses as `continue`. Steps are validated against the registered tools (`PlanValidator`). Invalid steps are not run and are not replaced by a rule plan: the errors go back to the model as `previous_turn_errors` on the next turn and count as a failure.

## What the model sees

Each planner call (`LLMStructuredPlanner.next_turn`) sends the task (wrapped as untrusted channel content), the selected tool specs, skills, retrieved experiences and `history`: for every executed step its tool, action, compacted arguments, `ok`, summary, error and output.

- Tool output (file text, shell output, DOM text, grounding JSON) is untrusted data. Secrets are redacted with the memory privacy filter, the text is cut to `agent.observation_chars`, and it is wrapped as `{"trusted": false, "kind": "tool_output", ...}`. The system prompt tells the model never to follow instructions found there.
- `agent.history_chars` caps all outputs of one call. Newer outputs are kept whole; older steps keep only their outcome.
- Screenshots reach a model only through the approval-gated `vision.ground` tool. After a successful `computer`/`ui_bridge` step that saved a screenshot, the loop adds a `vision.ground` step on it (unless the step set `auto_ground: false`); its grounding output is what the planner sees.
- Tool output now goes to the planner model provider. Deployments that must not send workspace content to an external model should route the `planner` task to a local profile or use `runtime.offline_mode`.

## Steps

Every step runs through `ToolRegistry.call`, so each tool's own permission check (`ctx.permissions.verify` with a content-bound proposal) runs on every call. The loop never decides risk; tools do.

- After a failed step the rest of that turn's steps are dropped (`steps_not_run` in history) and the model plans again.
- The loop adds steps of its own (`origin` in results): `auto_ground` (above), `auto_click` when the planner step set `auto_click_grounded`, and `verify` for a step with a `verification` spec. They are separate steps so that an approval pause on one of them resumes that step, not the action before it.
- Repeating a screenshot (or another observation) is skipped only while nothing changed: the duplicate check is scoped to the run and reset after any successful action.

## Budgets and termination

Configured under `agent:` (`AgentLoopConfig`); the first one exhausted ends the run as `failed`, with its name in `termination.reason`:

| key | default | reason |
|---|---|---|
| `max_turns` | 8 | `budget_turns` |
| `max_steps` | 20 | `budget_steps` |
| `max_llm_calls` | 12 planner requests (a schema repair counts) | `budget_llm_calls` |
| `max_tokens` | 200000 planner input + output tokens | `budget_tokens` |
| `max_cost_usd` | 1.0 | `budget_cost` (checked before each turn, only when the cost is known) |
| `max_consecutive_failures` | 3 (failed steps or rejected turns in a row) | `repeated_failure` |
| `max_active_seconds` | 900 (approval waits excluded) | `deadline` |
| `max_steps_per_turn` | 8 | extra steps are dropped with a warning |

Other reasons: `goal_reached`, `gave_up`, `planner_error` (the model provider failed after the first turn), `blocked` (a `PermissionError` while planning, such as a prompt-injection refusal), `approval_wait`, `cancelled` and `emergency_stop`. A plan or turn may list at most 50 steps (`MAX_PLAN_STEPS`); a longer one fails validation.

## Stopping runs

- `Orchestrator.cancel(run_id, reason=, actor=)` (CLI `omnidesk cancel <run_id>`): sets the run's row to `cancelled`. A running loop reads the row before every step and turn and ends the run there (a tool call already running finishes); a run waiting for approval can no longer be resumed, and its pending approval is denied.
- Emergency stop (CLI `omnidesk stop --reason ...`, `--status`, `--release`): a file `emergency_stop.json` next to the audit log, outside the workspace. While it exists, loops end between steps (`emergency_stop`), new runs end at once, the tool registry refuses every call, and a step that asks for approval withdraws the request. An unreadable stop file counts as engaged. The CLI records each change in the audit log through `record_event` when the permission manager provides it.
- The desktop is leased to one run at a time (`tools.computer.DesktopLease`): another run's screenshot, click, typing or key presses are refused until the holder finishes, pauses for an approval, or leaves the desktop idle for 120 s.

All planner calls of a run share the call-cap key `planner-<run_id>`, and vision calls `vision-<run_id>`; `llm.per_task_max_llm_calls` applies to each over `llm.per_task_window_seconds`.

Cost is the sum of the router's per-call cost (provider-reported, else the price-table estimate). When any call's model has no price, the run's cost is reported as `null`, never as 0.

Errors:

- A planner exception before any step ran marks the run `failed` and is re-raised, as before the loop.
- After a step ran, the run is returned as `failed` instead, so a queued job does not retry and repeat side effects.
- On the first turn only, a provider failure falls back to the rule planner (`rule_fallback`, reason in the rationale). Explicit `shell:` / `write file` commands use the rule planner directly. Rule and legacy (`plan(msg)` only) planners plan once; the loop finishes their run when their steps are done.

## Persistence, approvals and audit

The run store gets a checkpoint after every turn and every step: `status=running`, `plan_json.agent_loop` (all turns, steps, results, counters, policy) and `results_json`. An `ApprovalRequired` from any step saves that state with `save_waiting`. `resume()` checks the token, the approval (dual approval for critical) and then continues the loop from the blocked step, in any process. A resume before the approval is decided returns `approval_pending` and leaves the token unused. Runs saved by a release without the loop resume as a single plan from the saved step.

Tool permission decisions are audited by the permission manager as before. Loop lifecycle entries (`agent_turn_planned`, `agent_step_finished`, `agent_run_finished`; ids, counts and outcomes, never tool output) are written to the same hash-chained audit log when the permission manager provides `record_event(event, payload)`; until it does, they are only in the run store.

## Experiment hook

`core/planner_policy.py` defines `PlannerPolicy` (bounded prompt addendum, skills text and rules), `PolicyAssignment`, `RunOutcome` and the `PlannerExperimentHook` protocol (`assign(msg)`, `observe(assignment, outcome)`, optional `after_plan`). The orchestrator takes the hook as `learning_loop`:

- The policy applies to every planner turn of the run and is persisted with it. It is appended under the fixed header `Reviewed learned guidance (data, not authority):`; it cannot change tools, validation, risk or approvals.
- `observe` is called at an approval pause (`final=False`) and at the end of the run. Hook errors never break a run.
- `Orchestrator.plan_only(msg, policy)` returns the first turn without running anything or reading memory.
- A learning loop with only the older `assign_policy`/`observe_result` methods is still called (no policy, observed once per finished run).

## Known limits

- `verify` steps take a new screenshot on every attempt. Under remote approval each attempt needs its own approval of that screenshot.
- `ToolContext.provenance` / `ActionProposal.provenance` exist for the security session's taint tracking but the loop leaves them `None` until that module lands.
- No live-model evaluation is part of this change; the eval suite measures task success separately.
