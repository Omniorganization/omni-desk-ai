# iOS Simulator repair evidence — 2026-10-07

Base: `41cca0af821d45a98216dfc20a8e32903e2c4f23`. Version: `1.12.7+127`.

## Change and local validation

- The Sync button catches failures and asks for successful device enrollment before signing a request. Shared project refresh still serves connect and mutation callers.
- Session restoration reports unavailable secure storage, restores no partial credentials, and avoids updates after disposal.
- Hero, approval, and notification titles use the dark theme below MaterialApp.
- Debug/Profile/Release bind Keychain access groups through Runner.entitlements; no APNS entitlement or signing identity is invented.
- Firebase Core/Messaging use published versions 4.15.0/16.7.0. Core removes the two NSNull cast warnings; Messaging includes upstream APNs registration fixes. The lockfile is compatible with the repository's Flutter 3.38.8 / Dart 3.10.7 pin.
- A native integration test now runs in the existing iOS Simulator CI job, in addition to its unit tests, native build, and source readiness gate.

Local evidence: analysis passed; 12 unit/widget tests passed; all 3 added widget regressions fail on the original code; native Keychain write/read/delete, session restore, dark title, and offline Sync passed; MobAI 0.9.1 passed 3 local UI flows with 12 assertions. `summary.json` identifies the environment, results, and SHA-256 of the attached logs. Native app build and launch succeeded on iOS 26.5 Simulator with Xcode 26.6. The initial native test's title check ran after scrolling it off screen; moving that check before scrolling fixed the test, and its successful retry is attached.

## Remaining upstream warnings and validation limits

The first complete native build reports 9 warnings: two local_auth_darwin actor conformance warnings, one empty dummy object, and six Firebase Messaging warnings (legacy notification presentation, scene BOOL/void mismatch, and four deprecated FCM delegate/token APIs). A later incremental build emits none because dependencies are cached; it does not prove these warnings are resolved. Released plugins do not currently remove all of them. An unreleased Git Messaging candidate failed against its published platform interface and was discarded. No dependency cache patch, warning suppression, native SDK downgrade, or security bypass is included.

Upstream references: [Messaging APNs fix 18650](https://github.com/firebase/flutterfire/pull/18650), [scene registration fix 18620](https://github.com/firebase/flutterfire/pull/18620), [unreleased BOOL fix 18733](https://github.com/firebase/flutterfire/pull/18733), [Firebase iOS SDK 12.18 deprecations](https://firebase.google.com/support/release-notes/ios#version_12180_-_august_19_2026).

This is local Simulator evidence. Physical-device signing/provisioning, existing signed-device Keychain access, biometrics, live Gateway requests, APNS/FCM delivery, TestFlight, App Store, and customer Real GA remain unverified. Repository release gates retain their existing fail-closed behavior. GitHub checks on the exact PR head are authoritative for cloud validation.

## Risk and rollback

- Keychain access groups must be expanded by the real signing configuration on physical devices; this patch supplies no team, profile, or push capability.
- Dependency updates affect both mobile platforms. Android and iOS CI jobs must pass before merging; Simulator success alone does not validate Android packaging or physical iPhone behavior.
- Manual Sync requires enrollment again after a fresh process, while existing connect and post-mutation refresh retain their shared request path.
- Revert the repair commit to roll back; restore its parent pubspec/lockfile, Dart source, entitlements, Xcode settings, and CI change together. Run analysis, unit/widget tests, Android packaging, and the iOS native integration/build before redistribution. Preserve existing secure-storage items; this repair does not delete user sessions or device keys.
