# Patch Manifest 1.08

Version: `1.09+production-ga-self-healing-evidence`

## Changed

1. Promoted the product identity from `1.07+production-ga-release-integrity` to `1.09+production-ga-self-healing-evidence` across Python, Docker, workflows, Helm, release evidence, shared API, Web Admin, Desktop Tauri, and Mobile Flutter.
2. Fixed the Web Admin Docker runtime copy contract by committing `apps/web-admin-next/public/.gitkeep`.
3. Removed the Desktop Tauri undeclared `dirs::home_dir()` usage and switched workspace home resolution to standard-library environment handling.
4. Fixed Mobile Flutter push token registration to call `registerPushToken(..., platform: 'mobile')`.
5. Replaced the invalid iOS plugin registrant placeholder with an AppDelegate-compatible source placeholder and made release-mode readiness fail until Flutter regenerates it.
6. Added strict tri-app release targets for npm clean installs, `cargo check --locked`, Dart analysis, Android appbundle builds, and iOS IPA builds.
7. Added 1.08 regression tests and GA release gate checks for the runtime evidence blockers.

## Verification

- Version consistency gate.
- Tri-app source readiness gate.
- GA release gate.
- 1.09 self-healing evidence regression tests.
- Release hygiene gate.

Native signed binaries, APNS/FCM live delivery, notarization, Windows signing, and store distribution remain external evidence items that must be produced by the relevant release CI or signing system.
