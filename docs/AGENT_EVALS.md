# Agent task-success evals

`python -m omnidesk_agent.evals run` runs the scenarios in `omnidesk_agent/evals/scenarios/` through the real agent
runtime and writes a JSON report.

## What a scenario is

One JSON file (`agent_scenarios.AgentScenario`):

- `task`, and the workspace `files` it starts with;
- `approve`: what a careful human would approve (`tool`, `action`, optional workspace `path_prefix`). The runtime
  uses remote approval, so every medium-or-higher step waits for a decision; anything not listed is denied, which
  ends the run;
- `success`: checks after the run (`status_in`, `final_answer_contains` / `_not_contains`, `file_equals`,
  `file_contains` / `_not_contains`, `file_absent`, `max_steps`);
- `forbidden`: what must not happen. `tool_action` counts an attempt (the model proposed it, even if it was then
  blocked or denied); `file_exists` counts an effect (the forbidden result exists after the run);
- `budget`: loop budget overrides (`agent:` keys); `script`: the model turns for scripted mode.

Each scenario gets a fresh workspace, memory, run store, approval store and audit log, the real `Orchestrator`,
`LLMStructuredPlanner`, `ModelRouter`, `ToolRegistry` and `FilesTool`.

## Modes

- `--mode scripted` (CI, `Agent Evals (scripted)` on every pull request and push to main): the model is the
  scenario's `script`, returned turn by turn. It checks the runtime, the approval flow and the scoring, and must
  pass 100% with no safety violation. It does not measure model quality.
- `--mode live`: the planner is a real model: `--provider` (default `openai`), `--model`, optional `--base-url`, and
  the API key in `$OMNIDESK_EVAL_MODEL_API_KEY`. In GitHub Actions it runs from `workflow_dispatch` with
  `mode=live`, in the `agent-evals` environment, with secret `OMNIDESK_EVAL_MODEL_API_KEY` and variables
  `OMNIDESK_EVAL_PROVIDER`, `OMNIDESK_EVAL_MODEL`, `OMNIDESK_EVAL_BASE_URL`. Only a forbidden effect fails the
  job; the success rate is reported. The report is uploaded as the `agent-evals-live-<sha>` artifact.

## Metrics

`success_rate` (a scenario passes when every success check holds and nothing forbidden was attempted or
happened), `mean_steps`, `mean_turns`, `mean_llm_calls`, `total_cost_usd` (`null` when any call's model has no
price), `safety_violations` (scenarios with an attempt or effect), `unsafe_attempts`, `unsafe_effects`; per
scenario also status, termination reason, approvals granted/denied, duration and the failed checks.

Live results are committed only from real workflow runs, with the run URL from the report (`run_url`).

## Coverage today

Ten scenarios, file tasks only: answering from a file, transforming and merging files, recovering from a missing
file, counting, answering without tools, a prompt injection inside a file, a write outside the workspace, a denied
approval, and a Chinese task. Browser, desktop, shell and channel scenarios need those tools in an isolated
runner and are not covered yet.
