# Native Build Attempt 1.11

Date: 2026-06-15

## Source Builds Verified

- Backend full test suite: 429 passed, 1 skipped.
- Web Admin: `npm ci`, `npm run typecheck`, `npm run test`, and `npm run build` passed.
- Desktop frontend: `npm ci`, `npm run typecheck`, `npm run test`, and `npm run build` passed.

## Native Build Attempts

| Target | Command/tool | Result |
|---|---|---|
| iOS Xcode project discovery | XcodeBuildMCP `discover_projs` | Found `apps/mobile-flutter/ios/Runner.xcodeproj` and `Runner.xcworkspace` |
| iOS Xcode list/build preflight | `xcodebuild -list -project apps/mobile-flutter/ios/Runner.xcodeproj` | Blocked: active developer directory is CommandLineTools, not full Xcode |
| Flutter iOS release | `flutter build ipa --release` | Blocked: `flutter` not found |
| macOS/Tauri native release | `npm run tauri:build` | Blocked: `tauri` command not found |
| Rust locked check | `cargo check --locked` | Blocked: `cargo`/`rustc` not found |
| CocoaPods integration | `pod` | Blocked: `pod` not found |

## Required Environment To Produce Real Artifacts

- Full Xcode selected with `xcode-select`.
- Flutter and Dart SDK.
- CocoaPods.
- Rust stable, Cargo, and Tauri CLI.
- Apple signing identities/provisioning profiles and notarization profile.
- Android signing keystore for Android release artifacts.

This report is build-attempt evidence only. It is not customer-distribution GA evidence and must not be placed under `release/external-evidence` as a successful native build or signing artifact.
