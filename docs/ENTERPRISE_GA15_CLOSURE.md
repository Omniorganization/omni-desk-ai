# OmniDesk AI 0.7.31 Industrial GA15 Enterprise Closure

This release closes the GA14 production-readiness gaps that were still blocking a 100/100 industrial maturity claim in the local static/runtime contract assessment.

## Closed gaps

### Dual approval execution closure

Critical proposals now use a single enforced path:

1. `PermissionManager` marks configured critical risks with `requires_dual_approval`.
2. `ApprovalStore.create()` opens a matching `DualApprovalStore` record.
3. `/approvals/{id}/dual-approve` records two distinct owner approvals.
4. `ApprovalStore.decide(..., approved)` refuses approval until dual approval is ready.
5. `Orchestrator.resume()` checks dual approval again before consuming the approval/resume token.

This prevents direct single-owner approval or resume bypass for critical proposals.

### Break-glass runtime closure

Break-glass is now exposed through authenticated admin routes:

- `POST /admin/break-glass/open`
- `GET /admin/break-glass/status/{session_id}`
- `POST /admin/break-glass/revoke/{session_id}`

The session remains TTL-bound, requires a reason, requires an approver distinct from the actor, and writes an audit trail.

### Remote Docker plugin sandbox closure

Plugin execution can now use the remote sandbox-runner service when `sandbox.backend=remote_docker`. The application container no longer needs the Docker socket for plugin execution in production.

The sandbox-runner supports a bounded `stdin_base64` payload so plugins keep the same JSON stdin/stdout contract while running inside the remote no-network, read-only, non-root, digest-pinned container.

### Production config closure

The production example now aligns plugin capability toggles, remote sandbox policy, critical dual approval, break-glass HMAC policy, PostgreSQL HA storage, and generated config validation.

The production validator now also checks:

- `storage.require_multi_instance_safe=true` requires PostgreSQL.
- PostgreSQL mode requires a configured DSN env var.
- SQLite is rejected when binding production traffic.

### Kubernetes/Helm production closure

The Helm chart now includes:

- ServiceAccount
- Service
- ConfigMap-mounted production config
- resources requests/limits
- startup/liveness/readiness probes
- PodDisruptionBudget
- HorizontalPodAutoscaler
- topology spread constraints
- pod anti-affinity
- preStop/termination grace
- non-root, read-only-root filesystem, dropped capabilities
- non-placeholder digest-pinned images
- network policy without broad `namespaceSelector: {}`

The Kubernetes contract checker now rejects missing HA primitives and placeholder digests.

### Observability closure

OTLP HTTP export is now available through a bounded asynchronous exporter. Request paths enqueue spans instead of synchronously blocking on the collector/exporter endpoint.

## Verification performed

Contract gates executed successfully:

```text
check_version_consistency.py
check_release_hygiene.py
check_enterprise_readiness.py
check_kubernetes_contract.py
check_observability_contract.py
check_deployment_readiness.py
production_closure_drill.py --contract-only
check_github_actions_pinned.py
check_lock_hashes.py
check_supply_chain_standard.py
check_script_executability.py
```

Regression tests added:

```text
tests/test_ga15_enterprise_closure.py
```

Key regression groups were run, including GA14/GA15 closure, production validator, plugin sandbox, approval/resume, server routes, release governance, and previous GA hardening suites.
