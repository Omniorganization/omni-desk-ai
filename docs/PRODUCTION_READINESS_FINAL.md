# Production Readiness Final Hardening

This package closes the industrial review gaps that remained after the runtime hardening pass.

## Completed gates

- Test suite is green.
- CI global coverage gate is 75%; security/core/tools grouped gates are enabled; next target is 80%.
- AdminAuth semantics are explicit: default class has no IP restriction, production runtime passes `gateway.admin_allowed_ips`.
- Learning dashboard renders recent audit events with HTML escaping.
- Memory writes have a governed writer for namespace, retention, redaction, credential-like blocking and audit.
- Plugin permissions are validated at runtime.
- Docker plugin runner is available for no-network, read-only plugin execution.
- Shell Docker command construction is covered by tests.
- Self-upgrade state machine has a PR promotion gate requiring canary + tests + human approval.
- SQLite migration runner is available for schema versioning.
- Runtime metrics are initialized for agent, tool, approval, webhook, plugin, self-upgrade and memory events.
- Release workflow builds artifacts and emits an SBOM.

## Remaining production operation

Before live production, enable GitHub branch protection and push workflow files using a token with `workflow` scope.
