# Industrial Runtime Hardening

This pass implements the production-grade requirements raised in the industrial review.

## P0 implemented

- AdminAuth is role-aware and protects `/agent/run`, `/agent/resume/*`, `/approvals/*`, `/validate/*`, `/oauth/*`, `/self-upgrade/*`, `/admin/*`.
- AdminAuth supports Bearer token, `x-omnidesk-admin-token`, local-only development mode, IP allowlist, and JSONL audit. Legacy gateway-secret auth is disabled by default and production validation rejects `gateway.allow_legacy_gateway_secret_auth=true`; use role-bound viewer/operator/owner tokens instead.
- AppSync device pairing codes, challenge nonces, and device tokens are stored with purpose-separated HMAC-SHA256 using `OMNIDESK_APPSYNC_SECRET_PEPPER`; old SHA256 hashes are read only for compatibility with in-flight enrollments.
- Web Admin approval decisions use per-install WebCrypto device identity and signed `x-omnidesk-device-*` headers. The fixed `web-admin-console` device id is not valid for production approval.
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
