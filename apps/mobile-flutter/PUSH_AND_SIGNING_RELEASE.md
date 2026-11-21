# Mobile Push and Signing Release Gate

Required before production:

- Generate native projects with `flutter create . --platforms=android,ios`.
- Android: configure keystore, applicationId, Play internal testing track.
- iOS (needs an Apple Developer account; out of scope for now): bundle id, provisioning profile, APNs key, and TestFlight lane.
- Push (Android): a Firebase project, `FIREBASE_SERVICE_ACCOUNT_JSON` and `OMNIDESK_PUSH_PROVIDER=firebase` on the gateway, and `OMNI_ANDROID_GOOGLE_SERVICES_JSON` for the build. See README.md, "Push notifications".
- Push (iOS) and iOS signing need an Apple Developer account (APNs key, `aps-environment` entitlement, provisioning profile). The project has none, so both are out of scope.
- Approvals: biometric/PIN confirmation must pass on real iOS and Android devices.
- Run `flutter pub get && flutter analyze && flutter test`.
