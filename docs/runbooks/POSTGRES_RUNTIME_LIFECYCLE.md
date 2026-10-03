# PostgreSQL runtime lifecycle acceptance

The core PostgreSQL pool previously could lend a socket after shutdown if its
connection attempt completed late. Returning a socket concurrently with close
could also strand it in the idle queue. A failed connection attempt did not wake
capacity waiters. These are source-level availability and resource-lifetime
defects; existing signed-distribution GA evidence requirements still apply.

## Behavior

- Pool admission, idle ownership, counters and close share one condition.
- Shutdown immediately wakes queued acquisitions, rejects late connections and
  drains idle sockets. Borrowed transactions finish and close on return.
- Failed or cancelled connection attempts release reserved capacity and wake
  waiters. Queue waiting uses one monotonic deadline across wakeups.
- Acquisition and connect timeouts must be positive and finite. Network connect
  still has its own libpq timeout; the queue deadline does not cancel socket IO.
- Transactions commit on success, roll back on failure and discard connections
  when rollback fails. Transaction bodies and ambiguous commits are never
  automatically replayed.
- Repository factories serialize lazy initialization and cannot reopen after
  close. Cleanup failures log a stable message without connection credentials.

## Cloud acceptance

`Industrial Runtime Fast Lane` runs lifecycle regression tests and a real
PostgreSQL service. The isolated database tests create a unique schema and drop
it in fixture teardown. Three repository instances submit the same 48 synthetic
jobs concurrently; two workers claim and complete them with exact deduplication
and no repeated claims. Additional tests exercise actual SQL rollback, backend
termination, recovery on a new connection and terminal shutdown.

`Boundary Baseline Controls` checks the two shutdown controls against immutable
commit `41cca0af821d45a98216dfc20a8e32903e2c4f23`, requiring failures there and
success on the patched source. Existing security baseline controls stay intact.

The `postgres-runtime-acceptance-*` artifact includes pytest output, JUnit and
`evidence.json` with source head, actual checkout commit, Actions run ID and UTC
timestamp. Its scope is core storage runtime acceptance in isolated Actions,
and its customer GA status stays `blocked_missing_external_evidence`.

## Risk and rollback

The pool keeps its public connection context-manager interface and bounded
capacity. Shutdown is terminal and callers must construct a new factory when
restarting a runtime. An active transaction is deliberately not aborted by pool
close; the runtime must first drain workers and then close the repository.
Notifications do not guarantee FIFO fairness under sustained contention.

If a regression is observed, stop new admissions, drain workers and roll back
the application to the preceding signed release using the existing protected
deployment workflow. No database schema migration is introduced by this change.
Preserve the acceptance artifact and actual deployment diagnostics before
rollback. Re-run the full CI and PostgreSQL job on the rollback source.

## Persistent deployment boundary

An Actions PostgreSQL service is temporary. It supplies neither a customer
staging URL nor durable production infrastructure. Customer GA still requires
the actual persistent host/cluster, managed secrets, platform signer identities,
real devices, live integrations and production soak/recovery evidence. Do not
copy these isolated acceptance results into external GA evidence categories.
