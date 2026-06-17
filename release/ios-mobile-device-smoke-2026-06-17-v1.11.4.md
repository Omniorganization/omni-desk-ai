# iOS Mobile Device Smoke Evidence - 2026-06-17 - v1.11.4

## Scope

- App: Omni Mobile
- Bundle ID: `com.omnidesk.mobile`
- Version: `1.11.4`
- Build: `14`
- Device: iPhone `00008030-000C04583C87802E`
- CoreDevice identifier: `548F168F-D744-5CF6-B6E3-296F91546B93`
- Xcode: `26.5`, build `17F42`
- Flutter: `3.44.2`
- CocoaPods: `1.16.2`

## Fixes In This Version

- Bumped mobile version metadata to `1.11.4+14`, including Android `versionName` `1.11.4` and `versionCode` `14`.
- Reworked the iOS Flutter app into an OmniDesk AI mobile cockpit matching the requested interaction model:
  - Home dashboard with summary metrics, recent sessions, audit status, and bottom navigation.
  - Sales-analysis conversation detail with command bubble, completed-analysis state, bar chart, pie chart, summary, and report actions.
  - Tool surface for sales analysis, approval audit, and notification-chain checks.
  - Settings surface that preserves Gateway enrollment, secure storage, push registration downgrade, device identity, task dispatch, and biometric/PIN approval decision flow.
- Added widget smoke coverage for dashboard rendering, opening the analysis detail, and report action state changes.
- Kept Gateway URL validation, Firebase-config guard, push downgrade behavior, and Personal Team APNS non-binding policy intact.

## Tests And Build Evidence

- `dart format apps/mobile-flutter/lib apps/mobile-flutter/test`: passed.
- `flutter pub get`: passed.
- `flutter test`: passed, 9 tests.
- `dart analyze`: passed, no issues found.
- `pod install --project-directory=ios`: passed, 5 Podfile dependencies and 14 total pods.
- `flutter build ios --profile`: passed with automatic signing using team `VZH8W6APX7`.
- `git diff --check`: passed.
- `flutter analyze --no-pub`: blocked by Flutter analysis server crash with `FormatException: Unexpected end of input`; `dart analyze` passed against the same source tree.

## Signing Evidence

- Signed app path: `apps/mobile-flutter/build/ios/iphoneos/Runner.app`
- Code signing authority: `Apple Development: q44ijzgakn1@outlook.com (HNR65WVL8Q)`
- Code signing team identifier: `VZH8W6APX7`
- CDHash: `f2f1929d11881dce02199b717604b1fcc63f5231`
- Signed time: `Jun 17, 2026 at 16:39:06`
- Entitlements observed:
  - `application-identifier`: `VZH8W6APX7.com.omnidesk.mobile`
  - `com.apple.developer.team-identifier`: `VZH8W6APX7`
  - `get-task-allow`: `true`
- APNS entitlement status: not present in the signed Personal Team profile.

## Artifact Hashes

- `Runner`: `2f2369c2b2c622eed8163ac912689dfe825b29569623f2d33249b8a48ec0bb64`
- `Info.plist`: `a340b2ef7fe33ab544f358e3f642cfe8e15f6ceac31ebc23d6b7aa3a5ee749c9`
- `embedded.mobileprovision`: `1919e1e316d7c141cea8438f7c45de37ae217f4727863a2855cf96ea39b92715`

## Device Install And Launch Evidence

- `xcrun devicectl list devices`: iPhone `548F168F-D744-5CF6-B6E3-296F91546B93` was `available (paired)`.
- `xcrun devicectl device install app --device 548F168F-D744-5CF6-B6E3-296F91546B93 build/ios/iphoneos/Runner.app --timeout 120`: passed.
- Installed app query:
  - Name: `Omni Mobile`
  - Bundle Identifier: `com.omnidesk.mobile`
  - Version: `1.11.4`
  - Bundle Version: `14`
- `xcrun devicectl device process launch --console`: launched and stayed running for 30 seconds until the console session was manually interrupted.
- Console observation:
  - Missing `aps-environment` entitlement was reported.
  - No app startup crash was observed during the console window.
- Final foreground launch without console passed.

## APNS Capability Verification

APNS push smoke is not passed in this build.

- The signed entitlements do not include `aps-environment`.
- Console output reports the missing `aps-environment` entitlement.
- The current signing context is a Personal Development Team profile; the prior verified rule is to keep APNS entitlements as a paid-team template unless a Push-enabled provisioning profile is verified.

Required external inputs for APNS smoke:

- Paid Apple Developer Team.
- Push-enabled bundle id for `com.omnidesk.mobile`.
- Regenerated provisioning profile containing `aps-environment`.
- Valid `GoogleService-Info.plist`.
- APNS/FCM production or staging key material.

## Remaining Gaps

- APNS push delivery smoke remains blocked by signing/capability inputs.
- Real Gateway data smoke from the phone UI still requires a LAN/VPN-reachable Gateway URL and real operator token.
- Clipboard behavior is intentionally not the primary widget-test assertion because the Flutter widget test environment does not provide the same platform clipboard channel as iOS.

## Rollback

- Remove the installed app from the iPhone with:
  - `xcrun devicectl device uninstall app --device 548F168F-D744-5CF6-B6E3-296F91546B93 com.omnidesk.mobile`
- Reinstall the previous signed `1.11.1 (12)` or `1.11.0 (11)` artifact if a retained build artifact is available.
- Otherwise revert the mobile UI/version changes from the Git working tree and rebuild/install the previous profile artifact.
