import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omnidesk_mobile/secure_store.dart';

/// Models the iOS keychain: one item per key, tagged with its accessibility.
/// Reads only match items with the handle's accessibility, adds collide on the
/// key regardless of accessibility, and deletes remove every variant.
class _Keychain {
  final items = <String, MapEntry<String, String>>{};
}

class _FakeStorage implements FlutterSecureStorage {
  _FakeStorage(this.keychain, this.accessibility);

  final _Keychain keychain;
  final String accessibility;

  @override
  Future<String?> read({
    required String key,
    AppleOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    AppleOptions? mOptions,
    WindowsOptions? wOptions,
  }) async {
    final item = keychain.items[key];
    return item != null && item.key == accessibility ? item.value : null;
  }

  @override
  Future<void> write({
    required String key,
    required String? value,
    AppleOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    AppleOptions? mOptions,
    WindowsOptions? wOptions,
  }) async {
    final existing = keychain.items[key];
    if (existing != null && existing.key != accessibility) {
      throw StateError('errSecDuplicateItem');
    }
    keychain.items[key] = MapEntry(accessibility, value!);
  }

  @override
  Future<void> delete({
    required String key,
    AppleOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    AppleOptions? mOptions,
    WindowsOptions? wOptions,
  }) async {
    keychain.items.remove(key);
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

void main() {
  test('legacy keychain items move to this-device accessibility', () async {
    final keychain = _Keychain();
    final legacy = _FakeStorage(keychain, 'unlocked');
    final current = _FakeStorage(keychain, 'first_unlock_this_device');
    await legacy.write(key: 'omni.token', value: 'tok');
    await legacy.write(key: 'omni.device_private_key.v2', value: 'seed');

    expect(await current.read(key: 'omni.token'), isNull);
    await migrateLegacyKeychainItems(legacy: legacy, current: current);

    expect(await current.read(key: 'omni.token'), 'tok');
    expect(await current.read(key: 'omni.device_private_key.v2'), 'seed');
    expect(await legacy.read(key: 'omni.token'), isNull);
    expect(keychain.items.keys.where((k) => k.endsWith('.migrating')), isEmpty);

    // Idempotent on later launches.
    await migrateLegacyKeychainItems(legacy: legacy, current: current);
    expect(await current.read(key: 'omni.token'), 'tok');
  });

  test('an interrupted migration resumes from the staged copy', () async {
    final keychain = _Keychain();
    final legacy = _FakeStorage(keychain, 'unlocked');
    final current = _FakeStorage(keychain, 'first_unlock_this_device');
    // Crash after staging and deleting the old item, before the final write.
    await current.write(key: 'omni.token.migrating', value: 'tok');

    await migrateLegacyKeychainItems(legacy: legacy, current: current);

    expect(await current.read(key: 'omni.token'), 'tok');
    expect(await current.read(key: 'omni.token.migrating'), isNull);
  });

  test('device-only storage is the default for secrets', () {
    expect(
      omniSecureStorage.iOptions.accessibility,
      KeychainAccessibility.first_unlock_this_device,
    );
  });
}
