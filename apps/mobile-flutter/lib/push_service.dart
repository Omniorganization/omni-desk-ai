import 'dart:async';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'omni_api.dart';

class OmniPushService {
  final OmniApiClient client;
  final String deviceId;
  StreamSubscription<String>? _registrations;
  OmniPushService(this.client, this.deviceId);

  /// Registers with FCM and reports the Firebase Installation ID (FID) to the
  /// gateway, re-reporting it whenever FCM re-registers this install.
  Future<String?> register() async {
    try {
      await Firebase.initializeApp();
      final messaging = FirebaseMessaging.instance;
      await messaging.requestPermission(alert: true, badge: true, sound: true);
      final first = messaging.onRegistered.first.then<String?>((fid) => fid);
      _registrations ??= messaging.onRegistered.listen((fid) {
        client.registerPushToken(deviceId, fid, platform: 'mobile');
      });
      await messaging.register();
      return await first.timeout(
        const Duration(seconds: 20),
        onTimeout: () => null,
      );
    } catch (_) {
      return null;
    }
  }

  Future<void> dispose() async => _registrations?.cancel();
}
