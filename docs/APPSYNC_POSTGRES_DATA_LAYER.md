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

A failed statement or a domain error (not found, permission, conflict) rolls back
and keeps the pooled connection; only a dead or interrupted connection is
discarded.

The session limits travel as the libpq `options` startup parameter. PgBouncer in
transaction pooling mode refuses an unknown startup parameter, or drops it when
it is listed in `ignore_startup_parameters`, in which case the limits silently do
not apply. Behind PgBouncer, set them on the database role instead (`ALTER ROLE
... SET lock_timeout = ...`), or connect the gateway to PostgreSQL directly.

## Migration 5 and rolling upgrades

Migration 5 (`per_entity_transactions`) adds the event counter (seeded from the
existing events), an operation-log entry sequence, hot-path indexes, and the
projects table that project operations used to create with DDL at runtime. Apply
it with the existing CLI before starting this release:

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
fails with `LockNotAvailable` (HTTP 500), so keep the roll short and scale the
previous release down quickly. This shared lock can be removed in a later
release, once no previous-release replica can run against the database.

## Rollback

Migration 5 only adds two tables (the event counter, seeded from the existing
events, and projects), a sequence and indexes; the previous release ignores them, so rolling back the application needs no database change. Events
numbered by this release are ordinary rows the previous release reads. Operation
log entries written by this release use ids from 10^12 upward and sort after the
older ones.
