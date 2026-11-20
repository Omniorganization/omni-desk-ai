# Self-healing

Code: `omnidesk_agent/self_healing/` (`policy.py`, `probes.py`, `actuators.py`,
`supervisor.py`, `watchdog.py`, `drill.py`). The model router's retry, circuit
breaker and fallback chain and the job queue's retry, lease recovery and
dead-letter keep working on their own; self-healing adds detection across
components, actions those primitives do not take, verification and an audit
trail.

## Two loops

| loop | runs | handles |
|---|---|---|
| `HealingSupervisor` | inside the gateway | dead or stuck workers, stale job leases, dead-lettered jobs, model profiles with repeated failures, an invalid config file, repeated unexplained failures |
| `GatewayWatchdog` | separate process (`python -m omnidesk_agent.self_healing.watchdog`) | the gateway itself failing its health URL: crash loop, bad config deploy |

## Detection (`probes.py`)

- `WorkerProbe`: every registered worker (`WorkerRegistry.register(name, start=, stop=, is_alive=)`) must be alive.
- `JobQueueProbe`: running jobs past their lease (`queue:stale_jobs`); dead letters whose last error was transient (timeout, connection error, 429/5xx, crash recovery) and whether their dependency is healthy (`queue:dead_letters`).
- `ModelRouterProbe`: per-profile failure counts from `ModelRouter.status()`.
- `ConfigFileProbe`: the config file passes the validator; otherwise the signal carries the last known-good snapshot.
- `HttpHealthProbe`: a health URL returns 2xx.

## Decisions (`policy.py`) and actions (`actuators.py`)

| signal | action | autonomous | actuator |
|---|---|---|---|
| safety violation | `disable_capability` | yes, plus human re-enable | escalation only: no capability switch API exists yet |
| config invalid, snapshot known | `rollback_config` | yes | restores the snapshot atomically, keeps the rejected file, optional restart hook |
| release unhealthy, rollback ref | `rollback_release` | no | escalation |
| worker dead | `restart_worker` | yes | stop, start, check alive |
| stale jobs | `recover_stale_jobs` | yes | restarts the stuck worker first, then `JobQueue.recover_stale_running` |
| transient dead letters, dependency healthy | `requeue_dead_letters` | yes | requeues at most 20 transient ones; others stay for a human |
| transient dead letters, dependency down | none | | waits |
| 200 or more dead letters, none transient among the newest 200 | `escalate_human_approval` | no | escalation: older transient failures may be hidden (`JobQueue.list` returns the newest 200) |
| model profile failures over threshold | `switch_model_profile` | yes | `ModelRouter.open_circuit(profile)`; the router has no such public API yet, so today the action reports unavailable and the router's own breaker acts |
| repeated failures elsewhere | `create_upgrade_proposal` | files a request | one repair request per component per day, handed to the self-upgrade pipeline (which opens an `ai/*` PR for review) or queued in `<workspace>/repair_requests/` |

## Guard rails (`supervisor.py`)

- Modes: `off`, `observe` (decide and audit, never act), `act`.
- Each action respects its decision's cooldown, at most 3 actions per component per hour and 20 overall.
- After each action the same probe runs again; the record says whether the component recovered, or `progress` when a bounded batch (dead-letter requeue) worked and more remains.
- Three failed verifications quarantine the component: no more actions, one escalation, until it recovers or `reset(component)` is called.
- Probe or actuator errors are audited and never crash the gateway.
- Every decision, action and verification is a line in the audit JSONL.

## Watchdog

After `--failures` failed health checks: if the config differs from the last
known-good snapshot, restore it (the rejected file is kept) and restart;
otherwise restart. Then poll health until `recovery_timeout`. A config that
stays healthy for `--good-after` seconds becomes known-good; the window
restarts whenever the config file changes. A failing restart command is
recorded as `recovery_failed` and the watchdog keeps checking. Restarts are
limited per hour; past the limit it records an escalation and stops
restarting. The restart command is a fixed argv (no shell).

## Drills (`drill.py`)

`python -m omnidesk_agent.self_healing.drill --plan plan.json --out release/external-evidence/drills/self-healing-failure-injection.json`
runs each planned injection (argv lists, no shell) on the real host, waits for
the self-healing audit record that proves containment (for the injected
`component`, with a successful and verified action), checks health
afterwards and writes the report in the external GA evidence format with the
audit excerpts attached by SHA-256. It reports only what it observed:
`status` is `passed` only when every injection was contained and health
passed. Tests run the report through `scripts/check_external_ga_evidence.py`.

## Failure-injection tests

`tests/test_self_healing_failure_injection.py`: a worker killed by a crash
mid-job is restarted and the job completes; transient dead letters wait for
the provider and are then requeued while a permanent failure stays; a
truncated config is rolled back; a worker that keeps dying is restarted 3
times, quarantined and escalated once; cooldowns and hourly limits; observe
mode; one repair request per day; model circuit through the public API only;
the watchdog rolling back a bad config deploy and stopping after its restart
limit.

## Not wired yet

- The daemon does not start `HealingSupervisor` or register its workers yet;
  that is part of the wiring change after the agent-core re-planning loop.
- No unit file runs the watchdog; deploy assets belong to operations.
- No real drill has run. The staging drill and its evidence file are pending.
