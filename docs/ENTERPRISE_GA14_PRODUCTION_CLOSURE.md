# OmniDesk 0.7.30 GA14 Production Closure

GA14 adds a verifiable production-closure layer on top of GA13. The goal is not to claim that a zip file alone proves a perfect production system; the goal is to make the remaining production proof points executable and auditable.

## Closure gates

The package now includes these mandatory closure assets:

- `scripts/production_closure_drill.py` for contract-only and live production drills.
- `scripts/check_kubernetes_contract.py` for Helm/Kubernetes security contracts.
- `omnidesk_agent/repositories/health.py` for live storage capability and transactional outbox health checks.
- `omnidesk_agent/observability_probe.py` for OTLP collector reachability using the same exporter as runtime traces.
- `omnidesk_agent/security/break_glass.py` for time-boxed emergency access sessions with audit entries.
- runtime wiring for `DualApprovalStore`, `BreakGlassStore`, and `WormAuditCheckpoint`.
- `.github/workflows/production-closure-drill.yml` for scheduled closure contract checks.

## Contract-only drill

Run this in CI and before creating a release artifact:

```bash
python scripts/production_closure_drill.py --root . --contract-only
```

This validates the packaged deployment/security assets without requiring live external services.

## Live staging/production drill

Run this against a real environment before promotion or after a recovery drill:

```bash
OMNIDESK_POSTGRES_DSN='postgresql://...' \
OMNIDESK_OTLP_ENDPOINT='https://otel.example.com/v1/traces' \
OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY='...' \
python scripts/production_closure_drill.py \
  --backend postgres \
  --require-multi-instance-safe \
  --live-write \
  --audit-log /var/log/omnidesk/audit.log \
  --audit-checkpoint-dir /var/lib/omnidesk/audit-checkpoints
```

The live drill checks:

1. runtime storage plan refuses unsafe multi-instance SQLite;
2. repository factory supports PostgreSQL multi-instance capabilities;
3. transactional outbox can enqueue, claim, and complete a health event;
4. OTLP collector accepts a synthetic span;
5. audit checkpoint can be written and signed.

## Emergency access policy

Production must keep `permissions.require_dual_approval_for_risks` including `critical`. If break-glass is enabled, it must be time-boxed, separately approved by a distinct approver, written to the audit log, and covered by a signed audit checkpoint.

## Remaining real-world evidence needed

A package can close code and deployment contracts, but a final production score still depends on real evidence:

- PostgreSQL HA/failover drill results;
- Kubernetes rollout and rollback drill results;
- OTLP/Prometheus/Grafana screenshots or exported dashboards showing live metrics;
- SLO burn-rate reports;
- backup restore reports with RTO/RPO;
- cosign keyless release records;
- security review and audit checkpoint retention evidence.
