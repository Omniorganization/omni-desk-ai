import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'omni_api.dart';

class OmniPushService {
  final OmniApiClient client;
  final String deviceId;
  OmniPushService(this.client, this.deviceId);
  Future<String?> register() async {
    await Firebase.initializeApp();
    final messaging = FirebaseMessaging.instance;
    await messaging.requestPermission(alert: true, badge: true, sound: true);
    final token = await messaging.getToken();
    if (token != null && token.isNotEmpty) {
      await client.registerPushToken(deviceId, token, platform: 'mobile');
    }
    FirebaseMessaging.instance.onTokenRefresh.listen((newToken) {
      client.registerPushToken(deviceId, newToken, platform: 'mobile');
    });
    return token;
  }
}
