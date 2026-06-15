# Self-Healing Failure Injection Report 1.10

## Status

No real self-healing failure injection run is present in this workspace.

## Current result

`blocked_missing_external_evidence`

## Why this is blocked

The source package contains a governed self-healing controller and tests, but it does not include a real controlled failure injection report with production-like telemetry, containment action, rollback/proposal decision, and post-recovery health verification.

## Required real report

Attach `release/external-evidence/drills/self-healing-failure-injection.json` with:

- `status`: `passed` or `verified`
- `produced_at`: timestamp from the drill run
- `producer`: CI job, staging cluster, or operator
- `failure_injections`: at least one controlled injection
- `containment_action`: retry, fallback, circuit breaker, capability disable, rollback proposal, or signed rollback
- `recovery_verified`: `true`
- `post_recovery_health`: `passed` or `verified`

Placeholder, mock, example, or sample values are rejected by `scripts/check_external_ga_evidence.py`.
