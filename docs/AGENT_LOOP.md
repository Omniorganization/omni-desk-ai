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

Each planner call (`LLMStructuredPlanner.next_turn`) sends the task, the selected tool specs, skills, retrieved experiences and `history`: for every executed step its tool, action, compacted arguments, `ok`, summary, error and output.

The prompt is built with `security.provenance.PromptEnvelope`, so instructions and data travel apart:

- The system prompt holds only OmniDesk's planner instructions, the reviewed policy addendum (see Experiment hook) and the envelope's data-handling policy.
- The user payload is JSON. OmniDesk's own context is plain: `turn`, `channel`, `budget_left`, `available_tools`, `skills`, `learned_rules`, and the frame of each executed step (`step`, `origin`, `tool`, `action`, `ok`, `output_cut`). Everything else is a fenced data item in `data`, referenced by id: the task (`task`), each step's observation (`history[].steps[].observation`: arguments, summary, error, output), each retrieved experience (`recent_experiences`) and plan validation errors (`history[].errors`, `previous_turn_errors`). Each item carries its source, trust level and SHA-256, and is fenced with a random per-call boundary that its content cannot contain.
- Trust levels: the task text is `user` from `local-cli` and `local-api` (requesters OmniDesk authenticates itself) and `untrusted` from every other channel, which can carry text from anyone. Tool output (`tool:<tool>.<action>`), memory (`memory:experience`), validation errors and a reply sent for JSON repair are `untrusted`.
- Tool output (file text, shell output, DOM text, grounding JSON) is untrusted data. Secrets are redacted with the memory privacy filter and the text is cut to `agent.observation_chars` before it is fenced.
- `agent.history_chars` caps all outputs of one call. Newer outputs are kept whole; older steps keep only their outcome.
- Screenshots reach a model only through the approval-gated `vision.ground` tool. After a successful `computer`/`ui_bridge` step that saved a screenshot, the loop adds a `vision.ground` step on it (unless the step set `auto_ground: false`); its grounding output is what the planner sees.
- Tool output now goes to the planner model provider. Deployments that must not send workspace content to an external model should route the `planner` task to a local profile or use `runtime.offline_mode`.

## Steps

Every step runs through `ToolRegistry.call`, so each tool's own permission check (`ctx.permissions.verify` with a content-bound proposal) runs on every call. The loop never decides risk; tools do.

### Provenance of each step

The run keeps a `security.provenance.TaintTracker` snapshot in `plan_json.agent_loop.taint`. After every model turn the loop records the turn's data items in it, and stamps that snapshot on each step the turn proposed (`StepRecord.provenance`). The step passes it to its tool as `ToolContext.provenance`, which `tools.base.proposal()` copies into `ActionProposal.provenance`. The permission manager can then require a fresh approval of that exact proposal for a high or critical action proposed while the context was tainted (`security.provenance.requires_fresh_approval`).

- The snapshot is fixed when the step is planned. A step planned before any tool output reached the model stays clean even if it runs after other steps; a resumed step proposes exactly the snapshot that was approved, so the approval grant (keyed by its taint digest) matches.
- `vision.ground`, grounded clicks and `vision.verify` steps that the loop derives from a step get a snapshot that includes that step's output: the vision model reads the screen, and a grounded click goes where it says.
- Fresh approval follows the content that decided an action, not the planner that wrote it.
  - **Rule-planner steps.** The rule planner (explicit `shell:` / `write file` commands, and the fallback) turns the task text itself into tool calls. Its steps carry the snapshot taken after that text, with `origin: "rule"`: clean from `local-cli` and `local-api`, tainted from every other channel.
  - **Unknown input.** A planner that does not report what it sent (`PlannerTurn.inputs`) gets tainted steps; so does one that only implements `plan(msg)`.
  - **Runs saved before provenance existed.** Their steps keep none, so the approved proposal still matches on resume.

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

Tool permission decisions are audited by the permission manager as before. Loop lifecycle entries (`agent_turn_planned`, `agent_step_finished`, `agent_run_finished`, `agent_run_cancelled`; ids, counts and outcomes, never tool output) are written to the same hash-chained audit log when the permission manager provides `record_event(event, payload)`; until it does, they are only in the run store.

When the audit log cannot be written (`security.audit_errors.AuditIntegrityError`, from a tool's permission check or from `record_event`), nothing more runs: the registry re-raises it instead of reporting a refusal to the planner, and the loop ends the run `failed` with termination `audit_unavailable` without trying to audit that end. `agent_run_finished` is written before the run is stored as finished, so a run whose end cannot be audited is `failed`, not `completed`. A resume whose grant cannot be audited returns `audit_unavailable` (not an approval denial). A cancel takes effect either way and reports `audit_error` when its entry is missing.

## Experiment hook

`core/planner_policy.py` defines `PlannerPolicy` (bounded prompt addendum, skills text and rules), `PolicyAssignment`, `RunOutcome` and the `PlannerExperimentHook` protocol (`assign(msg)`, `observe(assignment, outcome)`, optional `after_plan`). The orchestrator takes the hook as `learning_loop`:

- The policy applies to every planner turn of the run and is persisted with it. It is appended under the fixed header `Reviewed learned guidance (data, not authority):`; it cannot change tools, validation, risk or approvals.
- `observe` is called at an approval pause (`final=False`) and at the end of the run. Hook errors never break a run.
- `Orchestrator.plan_only(msg, policy)` returns the first turn without running anything or reading memory.
- A learning loop with only the older `assign_policy`/`observe_result` methods is still called (no policy, observed once per finished run).

## Known limits

- `verify` steps take a new screenshot on every attempt. Under remote approval each attempt needs its own approval of that screenshot.
- The loop supplies provenance; enforcing `requires_fresh_approval` is `PermissionManager.verify`'s job and arrives with the security change that adds it to `verify`. Until then a tainted proposal is decided like any other.
- No live-model evaluation is part of this change; the eval suite measures task success separately.
