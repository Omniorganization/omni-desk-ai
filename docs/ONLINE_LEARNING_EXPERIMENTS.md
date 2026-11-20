# Replay, online A/B, canary and shadow evaluation of learned policies

A learned artefact (see [LEARNED_ARTIFACT_LIFECYCLE.md](LEARNED_ARTIFACT_LIFECYCLE.md))
changes the planner policy. This page describes how a candidate policy is
measured before and after real traffic sees it. Code:
`omnidesk_agent/self_learning/replay/engine.py`, `experiments/sequential_gate.py`,
`experiments/stats.py`, `rollout.py`, `shadow.py`, `runtime_loop.py`.

## 1. Offline: paired replay

`ReplayEngine` re-plans recorded runs twice with the same planner (a scripted
model in CI, the configured model in a deployment): once with the active
policy, once with `active + candidate`. Plans are never executed. Each plan
step is looked up in the recorded outcomes (`RecordedOutcomeOracle`); runs whose
steps add auxiliary results (computer/ui_bridge grounding, verification) are
skipped because results cannot be mapped to steps yet. A step
with the same tool, action and arguments (minus `expected_result`,
`verification`, `retry_policy`) gets the recorded result; anything else is
`unknown`. A plan is `ok` if every step is known-good, `fail` at its first
known failure, and `unknown` if an unknown step comes first. A planner error
counts as `fail` for that arm; cases where both arms error are excluded.

Only cases where both arms are known are compared. Wins (candidate ok,
baseline fail) and losses are tested with an exact one-sided McNemar test:

| verdict | rule (alpha 0.05) |
|---|---|
| `regressed` | losses significantly exceed wins |
| `inconclusive` | fewer than 10 comparable cases, or over half the candidate plans unverifiable, or a non-significant difference with losses |
| `improved` | wins significantly exceed losses |
| `non_inferior` | no losses |

`policy_improvement_score` is wins / comparable and
`learning_regression_score` is losses / comparable. The replay cannot credit a
step it has never seen succeed; the report gives the unknown counts. Recorded
runs come from the run store; a run is usable only if its plan was stored and
every result maps to one plan step (`cases_from_runs` reports what it skipped).

`DailySelfLearningJob(..., replay_plan_fn=..., replay_runs=...)` replays every
new candidate and records the result as its evaluation. Without a planner or a
run source the report says `replay_report: {"status": "not_run", ...}`.
The old template replay, which scored a failed trace as an improvement and
gated skill "canary" labels, is removed.

## 2. Online: A/B and canary rollout

`RolloutController.start(artifact_id, actor=..., spec=RolloutSpec(...))` needs:
a human actor; a stored, content-bound human approval of the artefact; a
passing offline evaluation; no other running rollout; and a planner that
applies policies (`planner_applies_policy=True`). Without the last one the arms
would be identical, so the start is refused.

- Admission also requires the offline evaluation to have been run against
  the policy that is active now.
- Assignment: the randomization unit is a hash of channel and thread id (or
  message id), never message text. Treatment runs get `active + candidate`,
  control runs the active policy. If the active policy changes during the
  rollout, the rollout stops (`stopped_base_changed`).
- Per arm: runs, successes (`completed`), runs with a policy safety event
  (`permission_denied`, `execution_strategy_blocked`, `policy_violation`; a
  human declining an approval does not count), model cost and latency. Cost
  that was not measured is `None` and never counted as zero. Runs that end in
  `waiting_approval` are counted separately and not scored.
- Gate (`SequentialGate`), every `look_every` treatment runs, at most
  `max_looks` times. Each test uses `alpha / max_looks` (Bonferroni), so over
  the whole rollout the chance of promoting a candidate that is not better
  (A/B) or worse by more than the margin (canary), or of rolling back one that
  is not worse, stays at or below `alpha`. A simulation in the tests checks
  this for 400 A/A rollouts.
  - Roll back at once: significantly more safety-event runs (exact Fisher
    test, any sample size) or a significantly lower success rate.
  - Promote: A/B needs a significantly higher success rate; canary needs
    non-inferiority (one-sided lower bound above minus the 2-point margin).
    Bounds use the Newcombe hybrid score method, which does not collapse at
    0% or 100% success the way a Wald interval does.
    Both also need: safety rate not above control, cost measured on at least
    90% of runs and at most +20%, latency at most +25%.
- Canary stages (default 1, 5, 10, 25, 50% of traffic): a passing look moves
  to the next stage once the stage has `min_stage_samples` runs, and the last
  stage also needs them before `ready_to_promote`. Within a stage the
  treatment rolls back when its error rate is confidently above the SLO (99%
  Wilson lower bound) and above the control's, or when the p95 latency of its
  most recent 2000 runs exceeds `slo_p95_latency_ms`.
- Outcomes: `rolled_back` (automatic; traffic returns to the active policy and
  the artefact needs a new evaluation and approval), `stopped_inconclusive`,
  or `ready_to_promote`, which records the online result as the artefact's
  evaluation (`online_ab_v1` / `online_canary_v1`). Applying it stays a human
  step (`python -m omnidesk_agent.self_learning.cli apply ...`).

Raw observations also go to the experiment store for dashboards; the
rollout's own aggregates decide. Rollout state is one SQLite file per
runtime, so several gateway instances would need a shared store.

## 3. Shadow mode

`ShadowRunner` plans a sample of real requests with the candidate policy in a
background task with a timeout, an in-flight cap and a daily cap. The shadow
plan is never executed, shown or written to memory. It stores only the
comparison with the production plan (tool/action sequence, common prefix,
risk and approval deltas, planning latency, planner errors) and, once the
production run ends (in either order; whichever side arrives second makes the
join), a join: `identical_plan`, `would_fail_same` (identical
through the failing step), `diverged_before_failure`,
`production_succeeded_shadow_diverged`. Divergence is not evidence of
success, and the report says so.

## 4. The hook

`RuntimeLearningLoop` is the orchestrator's hook: `assign_run(msg)` returns the
`PolicyAssignment`, `observe_run(assignment, RunOutcome)` feeds the rollout, and
`shadow_after_plan(...)` feeds the shadow runner. The wiring change exposes them
as agent-core's `PlannerExperimentHook` (`assign`/`observe`/`after_plan` with the
`omnidesk_agent.core.planner_policy` types). Without a rollout controller it
changes nothing and records nothing; the default 10% "planner_learning_candidate"
experiment, which compared two identical arms, is gone. The old
`assign_policy` / `observe_result` call shape still works; its substring
"security" guess is ignored and safety comes from structured result prefixes.

## Not wired yet

- The planner does not take `policy=` yet and the orchestrator does not build
  `RunOutcome` (latency, cost, structured safety events). Both come with the
  agent-core re-planning loop; the wiring change then constructs the registry,
  rollout controller and shadow runner in the daemon and sets
  `planner_applies_policy=True`.
- The run store keeps a plan only for runs that waited for approval, so most
  completed runs cannot be replayed until it stores every plan.
- No HTTP route or CLI for rollouts yet; `RolloutController` is the API.
- No live-traffic evidence exists yet. The staging canary run is pending.
