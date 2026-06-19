# OmniDesk AI 1.09 Production GA Self-Healing Evidence

## Status

`1.09+production-ga-self-healing-evidence` is a source-gated production candidate. It adds signed device request enforcement and a governed runtime self-healing controller, but it still requires external native signing, app-store/testflight evidence, registry attestations, live push verification, multi-instance soak, rollback, and backup/restore drill evidence before final customer-distribution GA.

## Self-healing capability model

The system is intentionally designed for controlled recovery rather than unrestricted self-modification.

| Failure class | Autonomous action | Human approval required | Notes |
|---|---:|---:|---|
| Transient API/model/tool error | Retry with backoff | No | Bounded retries only |
| Model quality/schema failure spike | Switch model profile / fallback | No | Must stay within approved policy |
| Plugin or provider outage | Open circuit breaker | No | Prevents cascading failure |
| Sandbox abnormal error rate | Disable capability / contain | Yes for re-enable | Fail-closed behavior |
| Safety/security violation | Disable capability and escalate | Yes | No autonomous re-enable |
| Bad release or SLO regression | Rollback to signed known-good artifact | Yes | Requires signed rollback target |
| Code defect requiring patch | Create upgrade proposal | Yes | No direct production code mutation |
| Data corruption or tenant-boundary issue | Contain, freeze, escalate | Yes | Restore requires runbook evidence |

## Operating loop

1. Detect signal from metrics, audit, task status, sandbox runner, gateway health, or release health.
2. Classify the failure into transient, provider, model, plugin, sandbox, release, safety, or data-integrity class.
3. Contain blast radius through retry, fallback, circuit breaker, capability disable, or rollback proposal.
4. Verify recovery through health checks, SLO metrics, and audit events.
5. For durable remediation, create a signed upgrade proposal that must pass tests, shadow/canary gates, and human approval before promotion.

## Guardrail

The AI can self-recover from many runtime failures, but it must not autonomously rewrite production code, bypass approvals, promote unverified models, or re-enable a disabled high-risk capability without governance.
