# Planner memory authorization and PostgreSQL call accounting

Task planners pass the authenticated `ChannelMessage.channel` and `sender_id`
to both memory reads and runtime writes. `retrieve_for_task` fails closed when
either identity is missing/unknown. Both stores select the exact actor and
channel before decrypting, then reject expired, malformed-expiry, blocked and
deprecated records. Candidate history remains provisional context; retrieval
does not promote it into a trusted reusable skill. Unknown historical ownership
is never inferred from tags or reassigned to the current actor.

Local `experience-search` is an explicit operator diagnostic using
`search_similar`; it can inspect historical records and does not supply planner
context. Custom memory integrations must implement the scoped retrieval API.
The planners do not fall back to a global legacy search.

Scoped retrieval returns at most 100 matches from up to 1000 recent eligible
owner records. Query terms are literal, case-insensitive tokens. This bounded
context selection is not an exhaustive archive search or a semantic index.
SQLite uses a separate immediate transaction for the read/touch operation;
PostgreSQL rechecks the locked current row before touching or returning it,
preserving concurrent curator decisions.

PostgreSQL token accounting now counts all usage records for a task with a
parameterized SQL aggregate. It no longer invokes the SQLite parent method on
a nonexistent file path. The existing positive call cap stays enabled. The
check and later provider accounting remain separate operations; this change
does not claim an atomic reservation for simultaneous provider calls.

Outbound retry/cancellation validate current state in the same transaction as
the mutation. Retry rejects sent and currently running messages. Manual retry
of ambiguous deliveries still requires operator reconciliation. An in-flight
provider send cannot be revoked merely by changing a database status, and
exactly-once effects still depend on the provider's idempotency contract.

## Validation and release scope

`test_planner_memory_boundaries.py` covers both storage backends, encrypted
rows, missing/foreign identity, expiry/review filters before decoding, a
concurrent curator block, planner request contents, call-cap behavior and the
sent-state race. Three controls execute against immutable baseline
`41cca0af821d45a98216dfc20a8e32903e2c4f23` in GitHub Actions and must fail there.
The real PostgreSQL job verifies cross-instance counting beyond 1000 rows and
owner selection despite 1005 newer foreign records.

These are isolated synthetic acceptance tests. They do not establish a live
model integration, persistent staging, production soak, native release signing,
physical-device operation, backup/restore or customer-distribution GA.

## Risk and rollback

Planner reads without owner context now return no memory, including old
`unknown:unknown` history. This deliberately reduces context rather than guessing
ownership. SQLite adds only two legacy metadata columns with `unknown` defaults;
existing columns and records are preserved. No PostgreSQL schema migration is
required. A protected rollback retains the additive SQLite columns and data.
Do not restore globally shared planner retrieval when reverting unrelated
features: disable planner memory context until a reviewed scoped fix is available.
Monitor scoped retrieval latency and denied/empty context with synthetic data
before production rollout. Review concurrent quota reservation and worker lease
fencing separately before claiming stronger concurrency guarantees.
