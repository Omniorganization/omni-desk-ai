import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:local_auth/local_auth.dart';

class MobileSessionSnapshot {
  const MobileSessionSnapshot({
    required this.gateway,
    required this.token,
    required this.actor,
  });

  final String gateway;
  final String token;
  final String actor;
}

class MobileSecurityService {
  MobileSecurityService({
    FlutterSecureStorage? storage,
    LocalAuthentication? localAuthentication,
    FirebaseMessaging? messaging,
  }) : storage = storage ?? const FlutterSecureStorage(),
       _localAuthentication = localAuthentication ?? LocalAuthentication(),
       _messaging = messaging;

  final FlutterSecureStorage storage;
  final LocalAuthentication _localAuthentication;
  final FirebaseMessaging? _messaging;

  Future<MobileSessionSnapshot> restoreSession({
    required String fallbackGateway,
    required String fallbackActor,
  }) async {
    return MobileSessionSnapshot(
      gateway: await storage.read(key: 'omni.gateway') ?? fallbackGateway,
      token: await storage.read(key: 'omni.token') ?? '',
      actor: await storage.read(key: 'omni.actor') ?? fallbackActor,
    );
  }

  Future<void> saveSession({
    required String gateway,
    required String token,
    required String actor,
  }) async {
    await Future.wait(<Future<void>>[
      storage.write(key: 'omni.gateway', value: gateway),
      storage.write(key: 'omni.token', value: token),
      storage.write(key: 'omni.actor', value: actor),
    ]);
  }

  Future<bool> confirmSensitiveAction() async {
    final canCheck =
        await _localAuthentication.canCheckBiometrics ||
        await _localAuthentication.isDeviceSupported();
    if (!canCheck) return false;
    return _localAuthentication.authenticate(
      localizedReason: 'Confirm Omni approval decision',
      biometricOnly: false,
      persistAcrossBackgrounding: true,
    );
  }

  Future<String?> resolvePushToken() async {
    final messaging = _messaging ?? FirebaseMessaging.instance;
    await messaging.requestPermission(alert: true, badge: true, sound: true);
    return messaging.getToken();
  }
}
