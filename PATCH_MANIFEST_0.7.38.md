# Patch Manifest: 0.7.38 Controlled Staging Tri-App Hardening

## Backend / Gateway

- Added `AppSyncConfig` with `json` / `postgres` backend selection.
- Added `create_appsync_store()` factory and `PostgresAppSyncStore`.
- Added idempotency persistence for mobile message/task creation and approval decisions.
- Added device enrollment metadata: `organization_id`, `public_key`, `token_hash`.
- Added approval expiry metadata and replay-safe decision idempotency.
- Added desktop task claim/lease fields: `claimed_by_device_id`, `lease_expires_at`, `attempt_count`.
- Added `/app/runtime/desktop/claim` endpoint.
- Hardened `/app/ws` with viewer-or-higher admin token authentication.
- Updated shared API contract to include claim endpoint, idempotency header, and authenticated websocket contract.
- Added regression tests for idempotency, task claim lease, and authenticated websocket.

## Web Admin

- Replaced token persistence with in-memory session state.
- Added role selector and owner-only approval controls.
- Added audit reason input for approval decisions.
- Added idempotency header support.
- Added CSP, X-Frame-Options, Referrer-Policy, X-Content-Type-Options, Permissions-Policy headers.
- Added Dockerfile and security release gate notes.

## Desktop Tauri

- Replaced localStorage token persistence with Tauri secure storage commands backed by the OS credential store.
- Added strict CSP in `tauri.conf.json`.
- Added desktop task claim/complete UI flow.
- Added keyring dependency and Tauri command handler.
- Enabled updater artifact generation flag.
- Added signing release gate notes.

## Mobile Flutter

- Added `flutter_secure_storage` session persistence.
- Added `local_auth` biometric/PIN confirmation hook before approval decisions.
- Added `firebase_messaging` dependency hook for FCM/APNS push work.
- Added idempotency headers for message creation and approval decisions.
- Added Android/iOS release scaffolds and Fastlane placeholders.
- Added push/signing release gate notes.

## Validation Performed Locally

```text
PYTHONPATH=. pytest -q tests/test_tri_app_foundation.py tests/test_release_package_hygiene.py
8 passed
```

`check_tri_app_release_readiness.py` now passes all static file/security checks available in this container. It still reports expected local environment blockers for missing Flutter/Rust/Cargo toolchains and missing production signing/push secrets; those are release-environment requirements, not source-tree defects.
