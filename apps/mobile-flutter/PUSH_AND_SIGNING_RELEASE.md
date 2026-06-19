# Mobile Push and Signing Release Gate

Required before production:

- Generate native projects with `flutter create . --platforms=android,ios`.
- Android: configure keystore, applicationId, Play internal testing track.
- iOS: configure bundle id, provisioning profile, APNS key, and TestFlight lane.
- Push: configure Firebase Messaging and APNS device-token upload.
- Approvals: biometric/PIN confirmation must pass on real iOS and Android devices.
- Run `flutter pub get && flutter analyze && flutter test`.
