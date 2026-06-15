# Patch Manifest 0.7.39 Enterprise Staging

## Backend
- Added scoped idempotency payload hashing and conflict detection.
- Enforced idempotency-key for AppSync mutating routes.
- Added device enrollment start/complete routes.
- Added device push-token registration route.
- Added PostgreSQL normalized schema and mirroring tables.

## Web Admin
- Added HTTP-only session and CSRF route scaffolds.
- Added CSRF header support in admin API client.
- Added device enrollment helper.

## Desktop Tauri
- Added auto worker dry-run loop with heartbeat, claim, completion, and worker log.
- Preserved OS secure storage for gateway/session values.

## Mobile Flutter
- Added Firebase Core/Messaging hooks.
- Added fail-closed biometric/PIN confirmation for approval decisions.
- Added push-token registration API.
- Expanded Android/iOS release scaffolding.

## Verification
- Python compile checks pass for patched backend modules.
- Backend focused pytest suite is expected to run with updated idempotency headers.
- Node/Flutter/Cargo builds still require external toolchains and signing credentials.
