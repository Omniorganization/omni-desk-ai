# OmniDesk AI 1.09 Production GA Self-Healing Evidence Source Package

This package closes the 1.07 GA candidate blockers that prevented native release evidence from being trustworthy.

## 1.09 GA optimization direction

- Web Admin Docker images must build from a committed public asset directory.
- Desktop Tauri Rust checks must fail closed with `cargo check --locked` and cannot reference undeclared crates.
- Mobile Flutter push registration must match the typed API surface and native release builds must include analyzer, tests, Android appbundle, and iOS IPA steps.
- iOS plugin registration is structurally compatible in source packages, while release-mode readiness requires Flutter-generated plugin registration before distribution.
- The shared tri-app contract now declares device request signature headers for enrolled desktop/mobile devices.

## Remaining external evidence boundary

- Real macOS notarization and Windows code signing.
- Real Android/iOS signed build artifacts.
- APNS/FCM live push validation.
- Registry attestations with final OCI digests.
- Multi-instance PostgreSQL soak, rollback, and backup/restore drill evidence.
- Device signature enforcement rollout once production clients have completed challenge verification and token migration.
