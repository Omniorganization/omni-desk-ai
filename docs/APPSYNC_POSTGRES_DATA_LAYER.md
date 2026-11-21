# AppSync on PostgreSQL: per-entity transactions

Production AppSync (`app_sync.backend: postgres`) is served by
`omnidesk_agent/appsync/migrated_postgres_store.py`
(`MigratedMultiInstancePostgresAppSyncStore`). The JSON store
(`StrictJsonAppSyncStore`) remains for local development only; the factory refuses
it in production and multi-instance mode.

## How a call runs

- Public methods run as short transactions, most as one; `bootstrap` reads in up
  to three and `verify_approval_signature` in two. A transaction reads rows by
  primary key or through an index, locks only the rows it changes
  (`SELECT ... FOR UPDATE`; task claims use `FOR UPDATE SKIP LOCKED`), and writes
  only those rows.
- Nothing is cached between calls. Replicas share the database and nothing else,
  so a revocation, a consumed approval or a completed enrollment is visible to
  every replica as soon as it commits.
- Idempotent writes take a transaction-scoped advisory lock on their own
  idempotency scope, so concurrent retries with one key run once and all get the
  first response.
- Insert races (two transactions creating the same new row), deadlocks and
  serialization failures are retried up to three times. Lock and statement
  timeouts are not retried; they fail the request. In an offline upload
  (`POST /app/sync`) they abort the whole upload rather than skipping the
  operation, so the client sends it again instead of marking it synced.
- Each signed request stores its nonce until its replay window ends, and each
  nonce claim also deletes up to 100 expired nonces of its namespace, so the
  table stays about as large as the replay window.
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

Signed device requests are on security's tamper-evident admin audit log instead
(`AdminAuth.record_device_request`, #278). Every refused signature is recorded with
its reason, and so is every verified request that changes state: task claims and
reports, approval decisions, device push-token, key, token and revocation
operations, and a device registration that carries a signature. Heartbeats are
not recorded. A state change that cannot be recorded is
refused with `503`. Until #278 provides the hook, nothing is recorded.

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

The session limits travel as the libpq `options` startup parameter. PgBouncer in
transaction pooling mode refuses an unknown startup parameter, or drops it when
it is listed in `ignore_startup_parameters`, in which case the limits silently do
not apply. Behind PgBouncer, set them on the database role instead (`ALTER ROLE
... SET lock_timeout = ...`), or connect the gateway to PostgreSQL directly.

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
| push outbox | 30 | `..._PUSH_OUTBOX_DAYS` | sent or failed only |
| notifications | 90 | `..._NOTIFICATIONS_DAYS` | |
| device challenges | 30 | `..._DEVICE_CHALLENGES_DAYS` | |
| device enrollments | 30 | `..._DEVICE_ENROLLMENTS_DAYS` | pending, expired or revoked; completed ones are kept as the record of each device's key binding |
| local inbox / synced outbox / operation log | 30 | `..._LOCAL_INBOX_DAYS`, `..._LOCAL_OUTBOX_DAYS`, `..._OPERATION_LOG_DAYS` | |
| resolved sync conflicts | 90 | `..._SYNC_CONFLICTS_DAYS` | |
| finished chat requests (and their stream events) | 30 | `..._CHAT_REQUESTS_DAYS` | completed, failed or interrupted |

Every override must be at least 1 day. Conversations and messages are user data
and are not pruned by age.

Sync events are pruned by sequence number: the watermark (`pruned_through`) is
committed before any event is deleted, so a reader below it is reset rather than
moved silently past the gap.

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
previous-release replica assigned. A write of this release waits behind queued
previous-release operations for at most `lock_timeout` (5 s by default) and then
fails with `LockNotAvailable`, answered as `503 database_busy`, so keep the roll short and scale the
previous release down quickly. This shared lock can be removed in a later
release, once no previous-release replica can run against the database.

## Rollback

Migrations 5 and 6 only add two tables (the event counter, seeded from the
existing events, and projects), a column, a sequence and indexes; the previous
release ignores them, so rolling back the application needs no database
change. Events pruned by retention stay deleted. Events
numbered by this release are ordinary rows the previous release reads. Operation
log entries written by this release use ids from 10^12 upward and sort after the
older ones.
