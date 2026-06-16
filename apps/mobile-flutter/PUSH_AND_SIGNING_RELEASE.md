# Mobile Push and Signing Release Gate

Required before production:

- Generate native projects with `flutter create . --platforms=android,ios`.
- Android: configure keystore, applicationId, Play internal testing track.
- iOS: configure bundle id, provisioning profile, APNS key, and TestFlight lane.
  GitHub Release Build requires `OMNI_IOS_CERTIFICATE_P12_BASE64`,
  `OMNI_IOS_CERTIFICATE_PASSWORD`, `OMNI_IOS_PROVISIONING_PROFILE_BASE64`,
  `OMNI_IOS_KEYCHAIN_PASSWORD`, `OMNI_IOS_APPLE_TEAM_ID`, and optionally
  `OMNI_IOS_BUNDLE_ID` plus `OMNI_IOS_EXPORT_METHOD`.
- Push: configure Firebase Messaging and APNS device-token upload.
- Approvals: biometric/PIN confirmation must pass on real iOS and Android devices.
- Run `flutter pub get && flutter analyze && flutter test`.
