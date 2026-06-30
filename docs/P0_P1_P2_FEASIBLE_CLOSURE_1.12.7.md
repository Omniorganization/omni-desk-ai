# P0/P1/P2 Feasible Closure for 1.12.7

Version: `1.12.7+root-monorepo-production-ga-candidate`

This closure completes every source-side repair that can be implemented without fabricating real external evidence. It intentionally does not mark the release as customer-distribution Real GA.

## Completed in this change

### P0: Real GA evidence closure tooling

Implemented source-side assets:

- `scripts/write_external_ga_evidence_templates.py`
- `scripts/check_real_ga_evidence_template_contract.py`
- `scripts/write_real_ga_customer_distribution_report.py`
- `.github/workflows/p0-p1-p2-feasible-closure.yml`

Operator workflow:

```bash
python scripts/write_external_ga_evidence_templates.py . \
  --output-dir dist/external-evidence-templates \
  --write-report dist/evidence/external-evidence-template-report.json

python scripts/check_real_ga_evidence_template_contract.py .

python scripts/check_external_ga_evidence.py . \
  --audit-only \
  --write-report release/real-ga-evidence-audit-1.12.7.json

python scripts/write_real_ga_customer_distribution_report.py . \
  --audit-report release/real-ga-evidence-audit-1.12.7.json \
  --output release/real-ga-customer-distribution-report.md
```

Real GA remains blocked until the following evidence is produced by real systems and committed or attached through the approved release process:

- Android/iOS/Tauri/Rust native build evidence.
- Android signed AAB, iOS signed IPA, macOS notarization, Windows signing evidence.
- Live GitHub branch-protection report.
- Live model Q&A smoke with trace, audit event, cost ledger, and budget enforcement.
- BigSeller live smoke if that integration is in the release scope.
- APNS/FCM live delivery evidence.
- Multi-instance Postgres soak.
- Rollback, backup/restore, and self-healing failure-injection drills.

### P1: OpenTelemetry collector/exporter chain

Implemented source-side assets:

- `deploy/observability/docker-compose.otel.yml`
- `deploy/observability/prometheus.yml`
- `deploy/observability/tempo.yaml`
- `scripts/check_observability_collector_contract.py`
- `docs/OBSERVABILITY_OTEL_PRODUCTION_CHAIN.md`

Runtime wiring already exists through:

- `omnidesk_agent/observability_otel.py`
- `omnidesk_agent/observability_tracing.py`
- `omnidesk_agent/server.py`

Validation command:

```bash
python scripts/check_observability_collector_contract.py .
```

### P2: Customer-distribution reporting and operating clarity

Implemented:

- Customer-readable GA report generation.
- Explicit decision rule: when the external evidence audit is not `passed`, the report states that the artifact set must not be called customer-distribution Real GA.
- Template generator guard that refuses to write templates into `release/external-evidence`.

## What remains outside source-only repair

These items cannot be completed truthfully inside the repository without real infrastructure or accounts:

| Item | Required external system |
|---|---|
| iOS signed IPA and installation smoke | Apple Developer certificate, provisioning profile, real device/TestFlight |
| Android signed AAB and install smoke | Release keystore, Play/internal test or device install |
| macOS notarization | Apple Developer ID signing and notarization service |
| Windows code signing | Valid Windows signing certificate or signing service |
| APNS/FCM proof | Provider credentials and a registered device token |
| Postgres soak | Staging/prod-like cluster with at least three gateways and two workers |
| Rollback drill | Release controller plus failed rollout scenario |
| Backup/restore drill | Real backup target and restore environment |
| Model live smoke | Staging/prod model gateway, audit ledger, and cost ledger |
| BigSeller live smoke | Approved BigSeller staging/production integration |

## Release decision rule

A release can only move from `source-gated production-ga-candidate` to `customer-distribution real-ga` when:

```bash
python scripts/check_external_ga_evidence.py .
```

returns `status: passed` without `--audit-only`, and the customer distribution report is regenerated from that passed audit.
