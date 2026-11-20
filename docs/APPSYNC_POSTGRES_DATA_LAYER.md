# AppSync on PostgreSQL: per-entity transactions

Production AppSync (`app_sync.backend: postgres`) is served by
`omnidesk_agent/appsync/migrated_postgres_store.py`
(`MigratedMultiInstancePostgresAppSyncStore`). The JSON store
(`StrictJsonAppSyncStore`) remains for local development only; the factory refuses
it in production and multi-instance mode.

## How a call runs

- Every public method is one short transaction. It reads rows by primary key or
  through an index, locks only the rows it changes (`SELECT ... FOR UPDATE`;
  task claims use `FOR UPDATE SKIP LOCKED`), and writes only those rows.
- Nothing is cached between calls. Replicas share the database and nothing else,
  so a revocation, a consumed approval or a completed enrollment is visible to
  every replica as soon as it commits.
- Idempotent writes take a transaction-scoped advisory lock on their own
  idempotency scope, so concurrent retries with one key run once and all get the
  first response.
- Insert races (two transactions creating the same new row), deadlocks and
  serialization failures are retried up to three times. Lock and statement
  timeouts are not retried; they fail the request.
- The authorization rules that both backends apply (task status reports,
  enrollment binding, verified-device updates) are shared functions in
  `store.py`, and `tests/test_postgres_appsync_per_entity.py` runs one
  cross-device scenario on both backends and requires identical results.

## Sync event sequence

Sync events are numbered from one counter row per namespace
(`omnidesk_appsync_event_counters`), updated as the last statement of the
writing transaction. The row lock is held until commit, so sequence order is
commit order: a reader that sees event N has already seen every event below N.
Writers in one namespace serialize only for that final statement and the commit.

### Reading the feed (`GET /app/sync`, `/app/ws`)

A response is one page of events after the reader's cursor, oldest first
(both backends):

| Field | Meaning |
|---|---|
| `events` | at most 1000 events (or `limit`) after `since_seq` that the caller may see |
| `sync_seq` | the cursor to send next: the last event of a full page, otherwise the head |
| `has_more` | more events follow; read again from `sync_seq` right away |
| `head_seq` | the newest event number at read time |
| `reset_required` | the cursor is below retained history (or ahead of the head, after a restore): fetch `/app/bootstrap`, then read on from its `sync_seq` |
| `pruned_through` | every event at or below this number may have been deleted |

A page never moves the cursor past an event the reader has not been given.
Events hidden from the caller (another actor's chat) still advance it.
`/app/ws` sends the next page immediately while `has_more` is true.
`/app/bootstrap` reads the head before its snapshot, so an event committed
meanwhile is replayed rather than skipped; clients apply events idempotently.

Liveness is not an event. A heartbeat emits `runtime.status_changed` only when the
runtime's status, active task, version, hostname or capabilities change, a
verified device request emits no event (it updates `last_seen_at`; routes count
signatures in metrics), and the network state is an event only when it changes.

## Timeouts

Each pooled connection sets these session limits. Values come from the
environment; the defaults are shown.

| Variable | Default | Effect |
|---|---|---|
| `OMNIDESK_APPSYNC_STATEMENT_TIMEOUT_MS` | 15000 | `statement_timeout` |
| `OMNIDESK_APPSYNC_LOCK_TIMEOUT_MS` | 5000 | `lock_timeout`: a call waiting on a row lock fails instead of hanging |
| `OMNIDESK_APPSYNC_IDLE_TX_TIMEOUT_MS` | 60000 | `idle_in_transaction_session_timeout` |
| `OMNIDESK_APPSYNC_CONNECT_TIMEOUT_SECONDS` | 5 | `connect_timeout` for new connections |
| `OMNIDESK_APPSYNC_POOL_ACQUIRE_TIMEOUT_SECONDS` | 10 | wait for a free pooled connection, then `PoolTimeout` |
| `OMNIDESK_APPSYNC_POSTGRES_POOL_SIZE` | 12 | connections per replica (2-64) |
| `OMNIDESK_APPSYNC_DB_THREADS` | 32 | worker threads that run store calls for async routes (2-256) |

A failed statement or a domain error (not found, permission, conflict) rolls back
and keeps the pooled connection; only a dead or interrupted connection is
discarded.

## Off the event loop, and busy databases

Route handlers await store calls on a bounded worker pool
(`omnidesk_agent/appsync/blocking.py`, `OMNIDESK_APPSYNC_DB_THREADS`, default 32),
including the chat reservation, completion and stream-event writes, so a call
waiting on a row lock or the network never stalls other requests, WebSockets or
SSE streams on the process. A lock timeout, statement timeout or pool timeout
answers `503` with `Retry-After: 2` and `{"detail": {"code": "database_busy"}}`.

## Retention

`omnidesk_agent/appsync/retention.py` deletes rows past their age in batches of
5000, each its own short transaction. Each replica's routes start a background
worker (`OMNIDESK_APPSYNC_RETENTION_INTERVAL_SECONDS`, default 3600; `0` turns it
off, for example when a CronJob runs it instead); a session advisory lock lets
one replica run at a time.

```bash
python -m omnidesk_agent.appsync.retention --namespace <namespace> --dry-run
```

| Table | Kept for (days) | Override | Condition |
|---|---|---|---|
| sync events | 14 | `OMNIDESK_APPSYNC_RETENTION_SYNC_EVENTS_DAYS` | by sequence; watermark first |
| device request nonces | until expiry | — | `expires_at` passed |
| idempotency keys | 7 | `..._IDEMPOTENCY_KEYS_DAYS` | |
| push outbox | 30 | `..._PUSH_OUTBOX_DAYS` | sent, dry run or failed |
| undelivered pushes | 7 | `..._PUSH_OUTBOX_UNDELIVERED_DAYS` | pending or sending: queued while no push provider was configured |
| notifications | 90 | `..._NOTIFICATIONS_DAYS` | |
| device challenges | 30 | `..._DEVICE_CHALLENGES_DAYS` | |
| device enrollments | 30 | `..._DEVICE_ENROLLMENTS_DAYS` | pending, expired or revoked; completed ones are kept as the record of each device's key binding |
| local inbox / synced outbox / operation log | 30 | `..._LOCAL_INBOX_DAYS`, `..._LOCAL_OUTBOX_DAYS`, `..._OPERATION_LOG_DAYS` | |
| resolved sync conflicts | 90 | `..._SYNC_CONFLICTS_DAYS` | |
| finished chat requests (and their stream events) | 30 | `..._CHAT_REQUESTS_DAYS` | completed, failed or interrupted |

Every override must be at least 1 day. Conversations and messages are user data
and are not pruned by age; they are deleted on request (below).

The same elected pass prunes the core runtime state when it is on PostgreSQL
(`omnidesk_agent/repositories/retention.py`): expired structured memories go
through the memory store's audited purge, oldest first (it used to see only the
newest 1000 rows); expired side-effect idempotency rows are deleted; the model
response cache (`OMNIDESK_CORE_RETENTION_LLM_CACHE_DAYS`, default 7) and model
usage records (`OMNIDESK_CORE_RETENTION_LLM_USAGE_DAYS`, default 90, which covers
the monthly budget window) are pruned by age. The per-call budget count now uses
an expression index on `value_json->>'task_id'`, and lookups by
`value_json->>'id'` have one too.

## Deleting a conversation

`DELETE /app/conversations/{conversation_id}` (operator; only the conversation's
own actor, 404 otherwise) erases it in one transaction: its messages, chat
requests and their stream events, notifications and pushes about it, the actor's
idempotency responses that contain it, and the actor's offline-sync inbox,
outbox and conflict rows that carry it. Sync events keep their numbers, so
readers' cursors stay valid, but lose the conversation title, and a
`conversation.deleted` event tells the owner's devices to drop their copy.
Tasks and approvals created from the conversation stay: they are the
organization's record of requested and approved actions.

Sync events are pruned by sequence number: the watermark (`pruned_through`) is
committed before any event is deleted, so a reader below it is reset rather than
moved silently past the gap.

## Data-subject export and erasure

Security's privacy service (`privacy/data_subject.py`) calls
`export_subject(actor)` and `erase_subject(actor)` on `rt.app_sync`. Both stores
apply the rules in `appsync/subject_data.py`. Matching is by actor id, and the
PostgreSQL store erases in one transaction. An export lists what an erasure
deletes or anonymises, and never includes a credential: no push target, token
hash or pairing code hash.

| Records | Erasure |
|---|---|
| User profile, conversations, messages, chat requests and their stream events | Deleted |
| The person's tasks | Deleted. A desktop still holding one gets "task not found" when it reports, so nothing is written back |
| Approvals they requested, or that gate their tasks | Deleted. The approval ledger erases its own rows |
| Notifications and pushes about these records; pushes and runtime status of their devices | Deleted |
| Offline-sync outbox, inbox, cursors, conflicts, their operation-log entries; idempotency replays | Deleted |
| Their devices | Kept, revoked for good: no verification, registration or push again. Name and push token removed, actor `[erased]` |
| Device enrollments they started | `requested_by` becomes `[erased]`. One not completed is revoked, so its pairing code cannot enrol a device |
| Approvals they decided for someone else | `decided_by`/`approved_by` become `[erased]`, and their reason is removed. On a pending approval their partial approval is withdrawn: an approver nobody can identify must not count toward four eyes |
| Notifications they caused on someone else's records | Actor becomes `[erased]` |
| Sync events | Numbers kept, so cursors stay valid. Actor `[erased]`. Events by the person or about an erased record keep only identifiers and states |

Not covered: the security audit logs (kept by design); projects
(`GatewayProjectStore`), whose `owner_actor` stays; and the PostgreSQL twins of
the agent-core stores in `repositories/postgres_state.py`. An erasure on a
local-first daemon store does not reach the remote gateway; run it on each.
Backups taken before an erasure still hold the data until they are rotated out.

## Approvals: four eyes

`decide_approval` follows the same rule as the agent's own approvals (security, #224):
- The **requester never approves** their own request: 403 `the requester cannot approve their own request`. Withdrawing it (rejecting) is allowed.
- A **critical** action (`risk == "critical"`) needs **two distinct approvers**, neither of them the requester. After the first approval the approval stays `pending`, with `approved_by` listing who approved. The task stays blocked, and a "Second approval required" push goes to the organization's mobile devices. The second distinct approver makes it `approved` and queues the task. The same approver approving twice gets 403.
- Any eligible approver's rejection is final.
- `required_approvals` is stored with the approval, and recomputed from the risk when the decision is made, so critical approvals created before this rule also need two.
- Actors are bound to gateway tokens (`OMNIDESK_<ROLE>_ACTOR`), so the owner token is one actor. An owner who requests a desktop action cannot also approve it; another approver must (for example the operator requests and the owner approves). A critical action needs two approvers other than the requester, for example the owner and the admin token.
- Migration 9 (`approval_four_eyes`) adds `required_approvals` and `approved_by`.

## Agent approvals in the clients' plane

The orchestrator records an agent step that needs approval in security's `ApprovalStore`. That store is the ledger: single-use, scope-bound, with dual approval when the proposal sets `requires_dual_approval`. `attach_approval_plane(rt)` connects it to the clients:
- **Listener.** `rt.orchestrator.approval_listener = AppSyncApprovalListener(rt.app_sync)` and `rt.orchestrator.organization_resolver = rt.app_sync.organization_for_actor` (agent-core #280). Each pause becomes the AppSync approval `agt_<ledger id>`. It is idempotent, belongs to the run's organization, and is pending for Web Admin and mobile. Its action reads `Agent step tool.action`, and `action_detail.proposal` holds the stored, redacted proposal. A pause without an organization or a requester is shown to nobody; it stays decidable through the ledger's own routes.
- **Decision.** `POST /app/approvals/agt_.../decide` takes the owner role and a device signature (a signature proves the device, not the right to approve). Before AppSync records anything, it applies the ledger's authority:
  - the requester (the proposal's `created_by`/`proposer`/`actor`) never approves;
  - a break-glass elevation never approves a critical step;
  - the approver must be in the run's organization;
  - a critical step needs two distinct owners. When the ledger opened a dual approval, each approval goes through `dual_approval_store.approve` first, and the ledger's readiness decides. A critical step without that flag still needs two approvers here.

  Then `decide("approved"|"denied")`, then AppSync mirrors the decision, and `await rt.orchestrator.resume_by_approval(ledger id)` resumes the run or ends it (`approval_denied` / `approval_expired`). The response carries only a summary of the run (`status`, `run_id`, `termination_reason`), never its output.
- **Errors.** An expired or already decided ledger approval gets 409; a refusal gets 403.

## Pushes carry no content

Every push, from both stores and for every event, has a fixed title and body (`push_text`). An agent step adds only its `tool.action` and risk, never its args, chat text, task text or file paths, because FCM and APNs are third parties. In-app notifications and approvals keep their text, and devices read them over the authenticated API.

## Push outbox and dispatch

A notification queues one push per challenge-verified, unrevoked device it is
for: approval requests go to the organization's mobile devices, and chat replies
only to the conversation's owner. With `OMNIDESK_PUSH_PROVIDER=firebase` (and
`FIREBASE_SERVICE_ACCOUNT_JSON` or `GOOGLE_APPLICATION_CREDENTIALS`), every
replica runs a dispatcher thread that sends due pushes every
`OMNIDESK_PUSH_DISPATCH_INTERVAL_SECONDS` (5; `0` turns it off).
`POST /app/push/dispatch` (owner) sends due pushes at once through the same
path. With the default dry-run provider nothing is dispatched automatically.

- **Leases.** A dispatcher claims due pushes with `FOR UPDATE SKIP LOCKED`, marks
  them `sending` under its id with a lease covering the whole batch, and only the
  lease holder can record the outcome. Replicas therefore send disjoint pushes. If
  a dispatcher dies, its lease expires and another one sends the push again, so
  delivery is at least once.
- **Target.** Each push goes to the device's current push target, read when it is
  claimed, so a target FCM rotated after the push was queued is used.
- **Retries.** A retryable failure (HTTP 429/5xx, `UNAVAILABLE`, `QUOTA_EXCEEDED`,
  `INTERNAL`, `THIRD_PARTY_AUTH_ERROR`, network errors) is retried after 10 s,
  20 s, 40 s and so on, at most 15 minutes apart. After `OMNIDESK_PUSH_MAX_ATTEMPTS`
  (5) sends, or once the push is older than `OMNIDESK_PUSH_MAX_AGE_SECONDS`
  (86400), it is given up as `failed` (`max_attempts`, `expired`). A push whose
  device was revoked or lost verification fails as `device_not_eligible`.
- **Dead targets.** `UNREGISTERED` or `SENDER_ID_MISMATCH` fails the push and
  clears the device's push target, unless the app has registered a new one
  meanwhile. The device gets no pushes until the app registers again, and the
  sync feed carries `device.push_token_invalidated`.
- **Honest outcomes.** A push the dry-run provider handled is recorded as
  `dry_run`, never `sent`. `provider` and `provider_message_id` record who
  delivered it.

Metrics: `omnidesk_push_deliveries_total{provider,outcome}` and
`omnidesk_push_dispatch_failures_total`. Migration 8 (`push_dispatch_leases`)
adds the lease, retry and provider columns and an index on due pushes.

## Migrations 5 and 6, and rolling upgrades

Migration 5 (`per_entity_transactions`) adds the event counter (seeded from the
existing events), an operation-log entry sequence, hot-path indexes, and the
projects table that project operations used to create with DDL at runtime.
Migration 6 (`sync_feed_retention`) adds the pruning watermark and the indexes
the retention rules scan. Index creation takes a write lock on each table for
its duration. Apply both with the existing CLI before starting this release:

```bash
python -m omnidesk_agent.appsync.migrate --namespace <namespace>
```

A previous-release replica serializes its operations under the session-level
`pg_advisory_lock` on the namespace key and writes back its whole cache. While
both releases run during a rolling upgrade, writing transactions of this release
take the same key with `pg_advisory_xact_lock_shared`: they never wait for each
other, only for a previous-release operation, so a stale write-back cannot
overwrite their rows. The event counter also stays above event numbers that a
previous-release replica assigned. This shared lock can be removed in a later
release, once no previous-release replica can run against the database.

## Rollback

Migrations 5 and 6 only add a table, a column, a sequence and indexes; the
previous release ignores them, so rolling back the application needs no database
change. Events pruned by retention stay deleted. Events
numbered by this release are ordinary rows the previous release reads. Operation
log entries written by this release use ids from 10^12 upward and sort after the
older ones.
