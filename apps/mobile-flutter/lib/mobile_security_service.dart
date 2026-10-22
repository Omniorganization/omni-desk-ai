import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:local_auth/local_auth.dart';

import 'secure_store.dart';

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
  }) : storage = storage ?? omniSecureStorage,
       _localAuthentication = localAuthentication ?? LocalAuthentication(),
       _messaging = messaging;

  final FlutterSecureStorage storage;
  final LocalAuthentication _localAuthentication;
  final FirebaseMessaging? _messaging;
  Future<void>? _keychainMigration;

  /// Runs the one-time keychain accessibility migration before first access.
  Future<void> ensureKeychainMigrated() =>
      _keychainMigration ??= migrateLegacyKeychainItems(current: storage);

  Future<MobileSessionSnapshot> restoreSession({
    required String fallbackGateway,
    required String fallbackActor,
  }) async {
    await ensureKeychainMigrated();
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
    await ensureKeychainMigrated();
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

  /// Registers this install with FCM and returns its Firebase Installation ID
  /// (FID), the direct-send push target in firebase_messaging 17+. Returns
  /// null if registration does not complete within [timeout].
  Future<String?> resolvePushTarget({
    Duration timeout = const Duration(seconds: 20),
  }) async {
    final messaging = _messaging ?? FirebaseMessaging.instance;
    await messaging.requestPermission(alert: true, badge: true, sound: true);
    // Subscribe before register(): onRegistered also re-emits an existing FID.
    final registered = messaging.onRegistered.first.then<String?>((fid) => fid);
    await messaging.register();
    return registered.timeout(timeout, onTimeout: () => null);
  }

  /// Emits a new FID whenever FCM re-registers this install.
  Stream<String> pushTargetChanges() =>
      (_messaging ?? FirebaseMessaging.instance).onRegistered;
}
