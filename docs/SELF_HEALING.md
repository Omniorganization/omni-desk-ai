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
| `GatewayWatchdog` | separate process (`python -m omnidesk_agent.self_healing.watchdog`) | the gateway itself failing its health URL: crash, hang, crash loop |

## Detection (`probes.py`)

- `WorkerProbe`: every registered worker (`WorkerRegistry.register(name, start=, stop=, is_alive=)`) must be alive.
- `JobQueueProbe`: running jobs past their lease, and which of them were claimed before their worker was restarted (`queue:stale_jobs`; only for a queue this process alone consumes, through `orphaned_before`); dead letters whose last error was transient and whether their dependency is healthy (`queue:dead_letters`). Transient means a timeout, connection error, rate limit or 429/502/503/504 next to "HTTP"/"status" or with its reason phrase; digits inside an order number or ID do not count, and neither does the queue's crash-recovery message (a job that hung or crashed its worker stays for a human). `dependency_healthy` has no default.
- `RequeueLedger`: a durable file recording each automatic requeue with the error the queue row loses; a job is requeued automatically at most once.
- `ModelRouterProbe`: per-profile failure counts from `ModelRouter.status()`.
- `ConfigFileProbe`: the config file passes the validator; otherwise the signal carries the last known-good snapshot.
- `HttpHealthProbe`: a health URL returns 2xx.

## Decisions (`policy.py`) and actions (`actuators.py`)

| signal | action | autonomous | actuator |
|---|---|---|---|
| safety violation | `disable_capability` | yes, plus human re-enable | escalation only: no capability switch API exists yet |
| config invalid, snapshot known | `rollback_config` | no: an owner decides, the config holds security, sandbox and approval settings | escalation naming the snapshot |
| config invalid, no snapshot | `escalate_human_approval` | no | escalation |
| release unhealthy, rollback ref | `rollback_release` | no | escalation |
| worker dead | `restart_worker` | yes | stop, start, check alive |
| stale jobs claimed before their worker was restarted | `recover_stale_jobs` | yes | re-reads each such job and returns it to retry (`JobQueue.fail`); never stops a worker |
| other stale jobs (or a shared queue) | `escalate_human_approval` | no | escalation: the queue does not renew leases, so a long healthy run looks like a hung one, and re-running it would repeat its side effects |
| transient dead letters not requeued before, dependency healthy | `requeue_dead_letters` | yes | requeues at most 20; each job at most once (`RequeueLedger`); others stay for a human |
| the same, dependency down | `wait` | | waits; not counted as recovery |
| transient dead letters already requeued once | `escalate_human_approval` | no | escalation |
| 200 or more dead letters, none transient among the newest 200 | `escalate_human_approval` | no | escalation: older transient failures may be hidden (`JobQueue.list` returns the newest 200) |
| model profile failures over threshold | `switch_model_profile` | yes | `ModelRouter.open_circuit(profile)`; the router has no such public API yet, so today the action reports unavailable and the router's own breaker acts |
| repeated failures elsewhere | `create_upgrade_proposal` | files a request | one repair request per component per day, handed to the self-upgrade pipeline (which opens an `ai/*` PR for review) or queued in `<workspace>/repair_requests/` |

## Guard rails (`supervisor.py`)

- Modes: `off`, `observe` (decide and audit, never act), `act`.
- Each action respects its decision's cooldown, at most 3 actions per component per hour and 20 overall.
- A decision that is not autonomous or needs approval only reaches an actuator that files a request (`requests_human = True`: escalation, repair request); registering an actuator that would make such a change itself is refused.
- After each action the same probe runs again; the record says whether the component recovered, or `progress` when a bounded batch (dead-letter requeue) worked and more remains.
- Three failed verifications quarantine the component: no more actions, one escalation, until it recovers or `reset(component)` is called.
- Probe or actuator errors are audited and never crash the gateway.
- Every decision, action and verification is a line in the audit JSONL.

## Watchdog

After `--failures` failed health checks the watchdog restarts the gateway and
polls health until `recovery_timeout`. It never changes the config: when the
file differs from the last known-good snapshot the record says so
(`config_differs_from_known_good`), and an operator can put the snapshot back
with `python -m omnidesk_agent.self_healing.watchdog --config PATH --state-dir DIR --restore-known-good`
(the replaced file is kept, its mode and owner are preserved). A config
becomes known-good only after the watchdog restarted the gateway with it on
disk and the gateway stayed healthy for `--good-after` seconds; the gateway
reads its config only at start, so a file written while the old process keeps
serving proves nothing. Snapshots are private (directory 0700, files 0600). A
failing restart command is recorded as `recovery_failed` and the watchdog
keeps checking. Restarts are limited per hour; past the limit it records an
escalation and stops restarting. The restart command is a fixed argv (no
shell).

Autonomous config rollback (for example only for a config that fails
validation and changes no security, approval, sandbox or RBAC key) needs the
owner's sign-off under AGENTS.md first; it is not implemented.

## Drills (`drill.py`)

`python -m omnidesk_agent.self_healing.drill --plan plan.json --out release/external-evidence/drills/self-healing-failure-injection.json`
runs each planned injection (argv lists, no shell) on the real host, waits for
the self-healing audit record that proves containment (for the injected
`component`, with a successful and verified action), checks health
afterwards and writes the report in the external GA evidence format with the
audit excerpts attached by SHA-256. It reports only what it observed: nothing
is injected when the baseline health check fails, an injection counts only
when every inject command exited 0, and `status` is `passed` only when every
injection was injected and contained and health passed. Tests run the report
through `scripts/check_external_ga_evidence.py`.

## Failure-injection tests

`tests/test_self_healing_failure_injection.py`: a worker killed by a crash
mid-job is restarted and, once its lease runs out, the orphaned job is retried
and completes; a healthy job that outlives its lease is neither cancelled nor
run twice; transient dead letters wait for the provider and are then requeued
once while a permanent failure stays, and a job that fails again is escalated;
order numbers and IDs with status digits are not transient; a truncated config
is escalated with its snapshot, not rolled back; an actuator that would carry
out a human decision cannot be registered; a worker that keeps dying is
restarted 3 times, quarantined and escalated once; cooldowns and hourly
limits; observe mode; one repair request per day; model circuit through the
public API only; the watchdog restarting without reverting a config, a staged
config never becoming known-good, the operator restore, and its restart
limit. `tests/test_self_healing_drill.py` covers each containment rule on its
own and a fault that was never injected.

## Not wired yet

- The daemon does not start `HealingSupervisor` or register its workers yet;
  that is part of the wiring change after the agent-core re-planning loop.
- No unit file runs the watchdog; deploy assets belong to operations.
- No real drill has run. The staging drill and its evidence file are pending.
- Recovering a hung run safely needs lease renewal (a heartbeat while a job runs) and job ownership in the queue and worker (`core/job_queue.py`, `core/worker.py`, the PostgreSQL queue); until then such jobs are escalated.
