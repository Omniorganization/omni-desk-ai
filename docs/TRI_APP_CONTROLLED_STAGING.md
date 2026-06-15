# OmniDesk 0.7.39 Tri-App Controlled Staging

This release moves the 0.7.37 tri-app foundation toward controlled staging.

## Closed P0 items

- AppSync can now run on JSON or PostgreSQL persistence via `app_sync.backend`.
- Desktop runtime tasks have a claim/lease contract: `/app/runtime/desktop/claim`.
- AppSync message creation and approval decisions accept idempotency keys.
- `/app/ws` is no longer anonymous; it requires a viewer-or-higher token via header or short-lived query token.
- Web Admin keeps session tokens in memory and adds role-aware approval controls plus security headers.
- Desktop no longer stores the operator token in `localStorage`; it uses OS secure storage through Tauri commands.
- Mobile adds `flutter_secure_storage`, biometric/PIN confirmation hooks, FCM/APNS dependency hooks, and Android/iOS release scaffolds.

## Remaining production gates

- Replace controlled-staging mobile native scaffolds with generated `flutter create . --platforms=android,ios` outputs before App Store / Play Store release.
- Configure real signing secrets for macOS, Windows, Android, and iOS.
- Run the full tri-app quality workflow on CI with Flutter, Rust, Node, and PostgreSQL available.
- Execute multi-instance staging: 3 gateway pods, 2 desktop runtimes, 1 PostgreSQL cluster, and mobile push test devices.
