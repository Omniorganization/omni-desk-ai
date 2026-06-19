# OmniDesk AI 1.09 Patch Manifest — Production GA Self-Healing Evidence

## Scope

This patch upgrades the 1.08 runtime-evidence source package toward a signed tri-app GA release by adding device signed-request enforcement, stricter tri-app release workflow dependencies, and a governed runtime self-healing policy.

## Implemented changes

1. Version bumped to `1.09+production-ga-self-healing-evidence` across backend, charts, Web Admin, Desktop Tauri, Mobile Flutter, and release metadata.
2. Backend AppSync now supports canonical device request signatures using method, path, body SHA-256, timestamp, and one-use nonce replay guards.
3. Production-sensitive AppSync routes now enforce signed device requests for desktop task claims/status, mobile/web approval decisions, push token registration, token rotation, device revoke, and desktop heartbeat.
4. Desktop Tauri now creates signed device request headers from the per-install P-256 key stored through OS secure storage.
5. Mobile Flutter now creates signed device request headers from the per-install Ed25519 key stored in secure storage.
6. Production config and validators now fail closed when device signed-request enforcement is disabled or timestamp skew exceeds the production limit.
7. Main release workflow now depends on Web Admin, Desktop, Android, and iOS release jobs before producing the backend release artifact set.
8. Runtime self-healing is codified as a governed controller: transient failures can retry/fallback/circuit-break, while safety violations, rollback, and durable code changes require containment and/or human-approved release workflow.
9. GA gate now checks device signature enforcement, client request signing, self-healing policy, and tri-app release workflow dependencies.
10. New tests validate production signed-request rejection, successful signed claim, nonce replay rejection, and self-healing decision classes.

## Verified local gates

- `scripts/check_version_consistency.py`
- `scripts/check_release_hygiene.py`
- `scripts/check_ga_release_gate.py`
- `scripts/check_tri_app_release_readiness.py --mode source`
- `scripts/check_observability_contract.py`
- `scripts/check_deployment_readiness.py`
- `scripts/check_supply_chain_standard.py`
- `scripts/check_kubernetes_contract.py`
- `scripts/check_enterprise_readiness.py`
- Python selected pytest suite: signed device requests, self-healing runtime, 1.08 runtime evidence, tri-app foundation
- Web Admin: `npm ci`, `npm run typecheck`, `npm test`, `npm run build`
- Desktop Tauri frontend: `npm ci`, `npm run typecheck`, `npm test`, `npm run build`

## Not locally verified in this container

The current execution environment does not provide Flutter, Cargo, or Rust. Therefore Android AAB, iOS IPA, and Tauri native package/signature builds remain release-CI requirements rather than locally verified artifacts.
