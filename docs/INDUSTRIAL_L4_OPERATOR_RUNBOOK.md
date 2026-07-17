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

Targets:

- RPO <= 5 minutes
- RTO <= 30 minutes
- rollback <= 15 minutes

## Signed distribution

Produce and verify Android AAB, iOS IPA, macOS notarized bundle and Windows Authenticode artifacts. Bind each artifact digest to the source commit, build run, signing run, Main Verification run and GitHub artifact attestation.

## Live evidence

Capture APNS/FCM receipts, real model smoke, BigSeller smoke when in scope, live organization/team governance, OTLP trace export and dashboard/rule loading. Import only operator-produced evidence through the approved import scripts.

Do not mark customer-distribution Real GA until `python scripts/check_real_ga_complete.py .` and `python scripts/check_customer_distribution_ga.py .` pass without `--audit-only`.
