# Omni Mobile App

Flutter shell for mobile approval, chat, and notification.

The app uses the shared `/app/*` Gateway API and does not execute tools locally. It is meant to approve or reject high-risk desktop/runtime actions, send tasks, and display notifications.

## Push notifications

- **Registration.** On connect, once the device is challenge-verified, the app
  registers its FCM installation ID (FID) with the gateway, and again whenever
  FCM rotates it. A device that is not verified (an operator token cannot
  enroll one) is told so in the Push status chip; the connection carries on
  without push.
- **Sending.** The gateway queues a push for every verified mobile device of the
  organization when an approval is requested, and sends it automatically
  through FCM HTTP v1, with retries and dead-token removal
  (`docs/APPSYNC_POSTGRES_DATA_LAYER.md`, "Push outbox and dispatch").
- **In the app** (`lib/push_service.dart`): a push that arrives while the app is
  open shows a snackbar and refreshes the pending approvals. Tapping a
  notification, or launching the app from one, refreshes and highlights the
  approval it is about. In the background the system shows the notification
  itself.
- **Not supported: iOS.** Delivery to iOS needs APNs: an `aps-environment`
  entitlement, a provisioning profile, and an APNs key uploaded to Firebase,
  all of which need an Apple Developer account. The project has none, so iOS
  builds register an FID but receive no pushes. Signed iOS (and desktop)
  releases are out of scope for the same reason.
- **Not verified yet.** No push has been delivered to a real Android device.
  That needs a Firebase project and two secrets set by the owner:
  `FIREBASE_SERVICE_ACCOUNT_JSON` on the gateway (with
  `OMNIDESK_PUSH_PROVIDER=firebase`), and `OMNI_ANDROID_GOOGLE_SERVICES_JSON`
  for the Android release build.
