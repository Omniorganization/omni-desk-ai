# Real GA Closure Gates 1.12.7

This document records the remaining boundary between source-level GA candidate readiness and customer-distribution Real GA.

## Source-level closure added

- BigSeller metrics now publish Prometheus-compatible `omnidesk_bigseller_*` names through `MetricsRegistry` while retaining the existing status payload for compatibility.
- Prometheus and Grafana contracts include BigSeller webhook rejection, duplicate webhook, and current dead-letter gauges.
- BigSeller live smoke can be generated only from real mode using `scripts/run_bigseller_live_smoke.py` with approved endpoint paths supplied through environment variables.
- Operator-produced BigSeller smoke evidence can be imported only through `scripts/import_bigseller_live_smoke_evidence.py`, which rejects placeholder, mock, sample, fake, and secret-bearing evidence.
- Real GA readiness now validates a live Main Verification workflow artifact for the target commit through `scripts/check_main_verification_artifact_live.py`.
- Branch-protection policy now requires admin enforcement and linear history in addition to required checks, pull request reviews, CODEOWNERS, signed commits, and pending-check blocking.

## Real evidence still required

Real GA remains blocked until the following evidence is produced by external systems:

- signed Android/iOS/Desktop artifacts and macOS notarization
- native Flutter/Rust/Tauri build evidence
- APNS/FCM live push receipts
- BigSeller live auth/order/inventory/webhook smoke from an approved real environment
- successful post-merge Main Verification evidence artifact
- multi-instance PostgreSQL soak with at least three gateways and two workers
- rollback, backup/restore, and self-healing failure-injection drills

## Non-negotiable rule

Do not mark customer-distribution Real GA until `scripts/check_external_ga_evidence.py` and `scripts/check_main_verification_artifact_live.py` pass without `--audit-only` against real evidence.
