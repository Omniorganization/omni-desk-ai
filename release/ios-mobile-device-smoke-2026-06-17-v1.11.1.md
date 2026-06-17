# iOS Mobile Device Smoke Evidence - 2026-06-17 - v1.11.1

## Scope

- App: Omni Mobile
- Bundle ID: `com.omnidesk.mobile`
- Version: `1.11.1`
- Build: `12`
- Device: iPhone `00008030-000C04583C87802E`
- iOS: `26.5 23F77`
- Xcode: `26.5`, build `17F42`
- Flutter: `3.44.2`
- CocoaPods: `1.16.2`

## Fixes In This Version

- Bumped mobile version metadata from `1.11.0+11` to `1.11.1+12`, including Android `versionName` and `versionCode`.
- Replaced the phone-local default Gateway URL with build-time configuration via `OMNI_MOBILE_GATEWAY_URL`.
- Added Gateway URL validation that rejects empty, non-http(s), and phone-local loopback URLs such as `127.0.0.1` and `localhost`.
- Added explicit push-token downgrade behavior when Firebase config is missing.
- Preserved the native guard that skips `FirebaseApp.configure()` when `GoogleService-Info.plist` is absent.
- Added `Runner/Runner.entitlements` as a paid-team APNS template, but did not bind it by default because Personal Team signing cannot support Push Notifications.

## Tests And Build Evidence

- `dart format lib test`: passed.
- `flutter pub get`: passed.
- `flutter test`: passed, 8 tests.
- `pod install --project-directory=ios`: passed, 5 Podfile dependencies and 14 total pods.
- `dart analyze`: passed, no issues found.
- `flutter analyze`: blocked by Flutter analysis server crash with `FormatException: Unexpected end of input`; `dart analyze` passed against the same source tree.
- `flutter build ios --profile`: passed with automatic signing using team `VZH8W6APX7`.

## Signing Evidence

- Signed app path: `apps/mobile-flutter/build/ios/iphoneos/Runner.app`
- Code signing authority: `Apple Development: q44ijzgakn1@outlook.com (HNR65WVL8Q)`
- Code signing team identifier: `VZH8W6APX7`
- Entitlements observed:
  - `application-identifier`: `VZH8W6APX7.com.omnidesk.mobile`
  - `com.apple.developer.team-identifier`: `VZH8W6APX7`
  - `get-task-allow`: `true`
- APNS entitlement status: not present in the signed Personal Team profile.

## Artifact Hashes

- `Runner`: `d5c180bfca8378fcb8f24018080c50d3b2dd01dee885bccce4d3f110a5c8b860`
- `Info.plist`: `d0ea74fc8f4b9f9b02bd66e8434eebea3ddc92ebb86d88aa2c4bf7cc479375f7`
- `embedded.mobileprovision`: `1919e1e316d7c141cea8438f7c45de37ae217f4727863a2855cf96ea39b92715`

## Device Install And Launch Evidence

- `xcrun devicectl device install app --device 00008030-000C04583C87802E build/ios/iphoneos/Runner.app --timeout 120`: passed.
- Installed app query:
  - Name: `Omni Mobile`
  - Bundle Identifier: `com.omnidesk.mobile`
  - Version: `1.11.1`
  - Bundle Version: `12`
- `xcrun devicectl device process launch --console`: launched and stayed running for more than 20 seconds until the console session was manually interrupted.
- Final foreground launch without console passed.

## APNS Capability Verification

Attempting to bind `Runner/Runner.entitlements` by default failed during signed profile build:

- Personal development teams do not support the Push Notifications capability.
- The provisioning profile did not include the Push Notifications capability.
- The provisioning profile did not include the `aps-environment` entitlement.

Result: APNS push smoke remains blocked until a paid Apple Developer Team, Push-enabled bundle id, regenerated provisioning profile, `GoogleService-Info.plist`, and APNS key are supplied. This is an external signing/capability blocker, not a passed smoke result.

## Remaining Gaps

- APNS push smoke is not passed.
- Firebase/FCM token registration is intentionally downgraded when `GoogleService-Info.plist` is absent.
- Real Gateway data smoke from the phone UI still requires a LAN/VPN-reachable Gateway URL and real operator token. The app now rejects phone-local loopback defaults instead of silently targeting the iPhone itself.

## Rollback

- Remove the installed app from the iPhone with:
  - `xcrun devicectl device uninstall app --device 00008030-000C04583C87802E com.omnidesk.mobile`
- Revert the mobile iOS scaffold/signing changes from the Git working tree if this generated Runner project should not be retained.
