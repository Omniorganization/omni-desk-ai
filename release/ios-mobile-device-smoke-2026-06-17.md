# iOS Mobile Device Smoke Evidence - 2026-06-17

## Scope

- App: Omni Mobile
- Bundle ID: `com.omnidesk.mobile`
- Version: `1.11.0`
- Build: `11`
- Device: iPhone `00008030-000C04583C87802E`
- iOS: `26.5 23F77`
- Xcode: `26.5`, build `17F42`
- Flutter: `3.44.2`
- CocoaPods: `1.16.2`

## Build And Signing Evidence

- `flutter test`: passed, 5 tests.
- `pod install --project-directory=ios`: passed, 5 Podfile dependencies and 14 total pods.
- `flutter build ios --debug`: passed with automatic signing using team `VZH8W6APX7`.
- `flutter build ios --profile`: passed with automatic signing using team `VZH8W6APX7`.
- Signed app path: `apps/mobile-flutter/build/ios/iphoneos/Runner.app`
- Signed app size: `26M`
- Code signing authority: `Apple Development: q44ijzgakn1@outlook.com (HNR65WVL8Q)`
- Code signing team identifier: `VZH8W6APX7`
- Entitlements observed:
  - `application-identifier`: `VZH8W6APX7.com.omnidesk.mobile`
  - `com.apple.developer.team-identifier`: `VZH8W6APX7`
  - `get-task-allow`: `true`

## Artifact Hashes

- `Runner`: `832203ac61898c683b25f11fa0eb8794cd58a1d5af7512955a4a3e770c0f38d3`
- `Info.plist`: `56a9f423f6842884b54964670f9658bb51d43caabd18943016abfded7095bb80`
- `embedded.mobileprovision`: `1919e1e316d7c141cea8438f7c45de37ae217f4727863a2855cf96ea39b92715`

## Device Install And Launch Evidence

- `flutter devices`: detected iPhone `00008030-000C04583C87802E`.
- `xcrun devicectl list devices`: detected iPhone as `available (paired)`.
- `xcrun devicectl device install app --device 00008030-000C04583C87802E build/ios/iphoneos/Runner.app --timeout 120`: passed.
- Installed app query:
  - Name: `Omni Mobile`
  - Bundle Identifier: `com.omnidesk.mobile`
  - Version: `1.11.0`
  - Bundle Version: `11`
- Profile app launch with `xcrun devicectl device process launch --console`: launched and stayed running for more than 20 seconds until the console session was manually interrupted.
- Final foreground launch with `xcrun devicectl device process launch --device 00008030-000C04583C87802E --terminate-existing com.omnidesk.mobile --timeout 60`: passed.

## Issues Found And Fixed

- Initial debug launch via `devicectl` crashed because debug builds require Flutter tooling or Xcode. Resolution: use profile mode for direct `devicectl` smoke launches.
- Initial native launch crashed when `FirebaseApp.configure()` ran without `GoogleService-Info.plist`. Resolution: guard Firebase native initialization in `ios/Runner/AppDelegate.swift` and allow enterprise-staging builds without Firebase config.
- Flutter Swift Package Manager integration failed in this non-ASCII workspace path. Resolution: disabled Flutter SwiftPM locally and kept CocoaPods integration.
- The original iOS `Runner.xcodeproj/project.pbxproj` was a two-line placeholder that CocoaPods could not parse. Resolution: regenerated the standard Flutter iOS Runner project and restored OmniDesk-specific app configuration.

## Remaining Gaps

- APNS push smoke is not passed. Device console reported missing `aps-environment` entitlement.
- `GoogleService-Info.plist` is still absent, so Firebase/APNS token registration is expected to be unavailable in this build.
- Real Gateway data smoke was not completed from the phone UI. The installed app defaults to `http://127.0.0.1:18789`, which refers to the phone itself, not the Mac. A reachable Gateway URL, operator token, and UI/device automation path are required for full real-data approval/session/notification validation.
- This evidence is development/profile-device smoke evidence, not customer-distribution GA evidence.

## Rollback

- Remove the installed app from the iPhone with:
  - `xcrun devicectl device uninstall app --device 00008030-000C04583C87802E com.omnidesk.mobile`
- Revert the mobile iOS scaffold/signing changes from the Git working tree if the generated Runner project should not be retained.
