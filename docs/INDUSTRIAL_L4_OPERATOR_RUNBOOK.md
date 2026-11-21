# Industrial L4 Operator Closure Runbook

This runbook covers the work that cannot be truthfully completed by source changes alone.

## Required topology

- 3 gateway replicas across at least two failure domains
- 2 background workers
- PgBouncer transaction pooling
- PostgreSQL staging/HA cluster
- OpenTelemetry Collector, Tempo, Prometheus and Grafana

## Soak gates

Run a 24-hour baseline and a 72-hour release-candidate soak. Record request volume, p50/p95/p99 latency, error rate, duplicate assistant-message count, event-sequence conflicts, lease losses, pool acquisition latency, failovers and recovery.

Acceptance criteria:

- no cross-tenant reads or writes
- no duplicate assistant messages
- no conflicting stream events
- no stale worker commits after lease loss
- PostgreSQL pool utilization below 80% sustained
- all SLO/error-budget thresholds satisfied

## Failure drills

Execute and capture evidence for gateway kill, worker kill, rolling restart, PostgreSQL primary failover, provider 429/500/timeouts, network latency/partition, SSE disconnect/reconnect, backup restore, failed rollout rollback and sandbox-runner isolation.

Targets: only numbers that the code or a drill backs (`docs/SLO.md`, "Recovery
objectives", has them for each deployment type).

- RPO: this topology's PostgreSQL HA cluster is run by an operator or managed
  service. It must provide continuous WAL archiving with point-in-time
  recovery and an RPO of 5 minutes or less, shown in its own drill. That is a
  requirement on the operator; OmniDesk does not implement it. A single host
  has an RPO of up to 24 hours (one verified logical dump a day).
- RTO: the operator must restore the database within 30 minutes. The OmniDesk
  steps after that (deploy the matching release, re-apply logged erasures,
  smoke test) have no measured time yet; the backup restore drill records it.
- Rollback: no target until the rollback drill measures one on the real
  environment.

## Signed distribution

Produce and verify Android AAB, iOS IPA, macOS notarized bundle and Windows Authenticode artifacts. Bind each artifact digest to the source commit, build run, signing run, Main Verification run and GitHub artifact attestation.

## Live evidence

Capture APNS/FCM receipts, real model smoke, BigSeller smoke when in scope, live organization/team governance, OTLP trace export and dashboard/rule loading. Import only operator-produced evidence through the approved import scripts.

Do not mark customer-distribution Real GA until `python scripts/check_real_ga_complete.py .` and `python scripts/check_customer_distribution_ga.py .` pass without `--audit-only`.
