# OmniDesk v1.11.7 iOS Real-Device Evidence Semantic Closure

This directory is the import point for real iOS evidence produced from a physical iPhone, a real Apple signing identity, and a real provisioning profile.

Do not commit private keys, raw device UDIDs, Apple certificates, provisioning profile contents, APNS tokens, Firebase tokens, bearer tokens, or screenshots that expose secrets.

Required iOS evidence files for v1.11.7:

- `native-build/flutter-ios-release.json`
- `signed-artifacts/ios-signed-ipa.json`
- `push/apns-live-delivery.json`

Use `scripts/import_ios_real_device_evidence.py` to validate and normalize raw evidence before copying it into this directory.

The evidence must be produced by a real local machine or trusted macOS runner after successful installation and smoke testing on a physical iPhone. Simulator-only output is not sufficient for customer-distribution GA.

Minimum smoke scope:

1. Build signed iOS release artifact.
2. Install to physical iPhone.
3. Launch successfully.
4. Connect to Omni Gateway.
5. Enroll/register mobile device identity.
6. Send mobile chat task.
7. Receive pending approval.
8. Approve/reject with biometric or PIN confirmation.
9. Register APNS/FCM push token and verify a real delivery receipt.


In v1.11.7 every evidence document must declare the expected release version, use `platform: ios`, and reference at least one artifact file under the raw evidence directory with a matching SHA256. The native build evidence must have `exit_code: 0`.

APNS live delivery evidence must reference a provider receipt, device notification log, or Firebase delivery receipt. It must not use the `.ipa` itself as delivery evidence. Supported APNS artifact kinds are:

- `apns_provider_receipt`
- `device_notification_log`
- `firebase_delivery_receipt`
