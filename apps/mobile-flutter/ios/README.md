# iOS release scaffold

The iOS Runner project is generated from the Flutter scaffold and keeps OmniDesk-specific bundle metadata.

Required release signing inputs:

- Apple team id.
- Development or distribution certificate/private key.
- Provisioning profile for `com.omnidesk.mobile`.
- APNS key and `GoogleService-Info.plist` when push delivery is part of the release evidence.

Push capability is not enabled by default for local Personal Team builds. Personal development teams do not support the Push Notifications capability, so binding `Runner/Runner.entitlements` by default breaks local signing. For a paid Apple Developer Team push build, enable Push Notifications for the bundle id, regenerate the provisioning profile, then set the Runner target `CODE_SIGN_ENTITLEMENTS` to `Runner/Runner.entitlements`.
