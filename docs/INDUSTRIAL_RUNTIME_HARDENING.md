# Industrial Runtime Hardening

This pass implements the production-grade requirements raised in the industrial review.

## P0 implemented

- AdminAuth is role-aware and protects `/agent/run`, `/agent/resume/*`, `/approvals/*`, `/validate/*`, `/oauth/*`, `/self-upgrade/*`, `/admin/*`.
- AdminAuth supports Bearer token, `x-omnidesk-admin-token`, legacy secret, local-only mode, IP allowlist, and JSONL audit.
- `server.py` reuses `rt.approval_store`.
- Webhook routes flow through `_guard_webhook()` and translate `PermissionError` to HTTP 403.
- Webhook processing uses `verify_request()`, `extract_envelope()`, replay/rate/idempotency guard, and adapter parse.
- Session allow is scoped by `source + actor + risk + scope_hash`.
- RunStore rotates resume tokens on waiting approval and consumes/clears them on resume/completion.
- Self-upgrade dashboard is protected by AdminAuth.
- Shell supports `argv` and Docker backends.
- Subprocess plugin runner limits output size and keeps plugins outside the main process.
- Self-upgrade governance records state-machine state and evidence.
- Memory governance provides redaction, actor/channel namespace, retention, and credential-write blocking.

## Still operationally required

GitHub Actions workflow files require a token with `workflow` scope before they can be pushed to GitHub.
