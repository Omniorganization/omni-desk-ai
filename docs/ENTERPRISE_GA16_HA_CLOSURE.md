# OmniDesk AI 1.05+production-ga-closure HA Closure

This release closes the remaining enterprise HA gaps identified after GA15.

## Runtime state

Production `storage.backend=postgres` now routes critical runtime state through `RepositoryFactory` rather than direct local SQLite constructors:

- approvals
- dual approvals
- break-glass sessions
- webhook replay and rate limiting
- job queue
- outbound message queue
- run / resume-token state
- transactional outbox

SQLite remains available only as the single-node local backend. `storage.require_multi_instance_safe=true` fails closed unless PostgreSQL is selected.

## Emergency access

Break-glass is now wired into `AdminAuth`. A valid lower-privilege token plus `X-OmniDesk-Actor` and `X-OmniDesk-Break-Glass-Session` can be used for time-boxed role elevation when the session is active and actor-bound. Every use is audited. Break-glass does not bypass approval consumption or dual-approval gates.

## Deployment closure

Docker Compose now includes PostgreSQL with healthcheck, secret-backed password, DSN injection, and persistent volume. Kubernetes readiness now uses `/ready`, Helm uses explicit ingress selectors, and `/data` is backed by a PVC.

## Remaining operational requirement

Production operators must provide a real digest-pinned application image, sandbox-runner image, PostgreSQL image, strong secrets, and a valid `OMNIDESK_POSTGRES_DSN` before exposing traffic.
