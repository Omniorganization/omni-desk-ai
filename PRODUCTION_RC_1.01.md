# OmniDesk AI 1.02+production-rc2-tri-app-hardening

This release upgrades the 0.7.39 enterprise-staging line into the 1.02 production-rc2 line.

## RC Scope

- Unified release version governance across Python, Docker, workflows, shared API contract, Web Admin, Desktop Tauri, and Mobile Flutter.
- Source/release split for tri-app quality gates.
- PostgreSQL AppSync production seams for transaction-safe task claim using `FOR UPDATE SKIP LOCKED`, idempotency keys, leases, push outbox, and device challenge verification.
- Web Admin HTTP-only session proxy and CSRF guarded mutation routes.
- Desktop Runtime capability executor contract with dry-run default and gated shell/browser/file/ui executors.
- Mobile push registration and expanded iOS/Android scaffolding.
- Release zip hygiene enforcement.

## Still Required For Full GA

- Run `flutter build appbundle --release` and `flutter build ipa --release` in a real Flutter/Xcode environment.
- Run `cargo check` and signed Tauri builds on macOS/Windows/Linux runners.
- Provide real FCM/APNS credentials and signed desktop/mobile release identities.
- Execute multi-instance PostgreSQL load tests and failover drills.
