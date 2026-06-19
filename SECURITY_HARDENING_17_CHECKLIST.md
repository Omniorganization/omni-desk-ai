# Security Hardening 17-Item Checklist

Implemented:

1. Fixed `config.py` YAML loading recursion and invalid root handling.
2. Added blocking Pyright gate to CI workflow.
3. Regression/security test runners fail closed when required self-upgrade tests are missing.
4. Production default `allow_local_admin_without_token = False`.
5. Hardened `FilesTool` against path prefix escape using `Path.relative_to`.
6. Enabled forced signature verification for enabled webhook channels.
7. Plugins now require both `sha256` and HMAC `signature`; in-process plugins are forbidden.
8. Management routes are protected by AdminAuth.
9. `server.py` reuses `rt.approval_store`.
10. Webhooks are routed through `_guard_webhook`; `PermissionError` becomes 403.
11. Adapters expose `extract_envelope()` for real sender/message identity.
12. `session_allows` scope is `source + actor + risk + scope_hash`.
13. `RunStore.save_waiting()` rotates resume tokens; `complete()` clears them.
14. `ToolSelector` reduces planner tool context.
15. Low-confidence vision actions enter approval flow.
16. Self-upgrade proposal metadata records regression/security/shadow/canary results.
17. `PlanValidator` delegates argument validation to `ActionSpec.validate_args`.
