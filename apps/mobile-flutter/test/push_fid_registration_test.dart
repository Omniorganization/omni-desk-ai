import 'dart:async';

import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omnidesk_mobile/mobile_security_service.dart';

class _FakeMessaging implements FirebaseMessaging {
  _FakeMessaging({this.fid});

  final String? fid;
  final _registered = StreamController<String>.broadcast();
  int registerCalls = 0;

  @override
  Stream<String> get onRegistered => _registered.stream;

  @override
  Future<void> register({String? vapidKey, String? serviceWorkerScriptPath}) async {
    registerCalls += 1;
    final value = fid;
    if (value != null) scheduleMicrotask(() => _registered.add(value));
  }

  void rotate(String newFid) => _registered.add(newFid);

  @override
  Future<NotificationSettings> requestPermission({
    bool alert = true,
    bool announcement = false,
    bool badge = true,
    bool carPlay = false,
    bool criticalAlert = false,
    bool provisional = false,
    bool sound = true,
    bool providesAppNotificationSettings = false,
  }) async =>
      const NotificationSettings(
        alert: AppleNotificationSetting.enabled,
        announcement: AppleNotificationSetting.notSupported,
        authorizationStatus: AuthorizationStatus.authorized,
        badge: AppleNotificationSetting.enabled,
        carPlay: AppleNotificationSetting.notSupported,
        lockScreen: AppleNotificationSetting.enabled,
        notificationCenter: AppleNotificationSetting.enabled,
        showPreviews: AppleShowPreviewSetting.always,
        timeSensitive: AppleNotificationSetting.notSupported,
        criticalAlert: AppleNotificationSetting.notSupported,
        sound: AppleNotificationSetting.enabled,
        providesAppNotificationSettings: AppleNotificationSetting.notSupported,
      );

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

void main() {
  setUp(() => FlutterSecureStorage.setMockInitialValues(<String, String>{}));

  test('push target is the FID emitted after register()', () async {
    final messaging = _FakeMessaging(fid: 'cX1yZ2abcdefghijklmnop');
    final service = MobileSecurityService(messaging: messaging);
    expect(await service.resolvePushTarget(), 'cX1yZ2abcdefghijklmnop');
    expect(messaging.registerCalls, 1);
  });

  test('registration that never completes times out instead of hanging', () async {
    final service = MobileSecurityService(messaging: _FakeMessaging());
    expect(
      await service.resolvePushTarget(timeout: const Duration(milliseconds: 50)),
      isNull,
    );
  });

  test('FID rotations are surfaced for re-registration', () async {
    final messaging = _FakeMessaging(fid: 'cX1yZ2abcdefghijklmnop');
    final service = MobileSecurityService(messaging: messaging);
    final changes = <String>[];
    final subscription = service.pushTargetChanges().listen(changes.add);
    messaging.rotate('dY2zA3abcdefghijklmnop');
    await Future<void>.delayed(Duration.zero);
    expect(changes, <String>['dY2zA3abcdefghijklmnop']);
    await subscription.cancel();
  });
}
