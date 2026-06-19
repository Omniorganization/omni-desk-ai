# Mobile Flutter Rules

- Preserve device keypair generation, request signing, push token registration, and enrollment flows.
- Do not weaken iOS/Android signing, provisioning, APNS, FCM, or secure storage boundaries.
- Do not mark emulator-only checks as real signed device or push delivery evidence.
- Release changes must keep Flutter, Android, and iOS version metadata aligned.
