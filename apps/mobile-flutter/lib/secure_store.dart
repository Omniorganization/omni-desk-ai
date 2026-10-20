import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Secrets stay on this device: on iOS they are not restored from iCloud or
/// iTunes backups onto another device (which would clone the device identity
/// the gateway trusts). first_unlock keeps background push refresh working.
const omniSecureStorage = FlutterSecureStorage(
  iOptions: IOSOptions(
    accessibility: KeychainAccessibility.first_unlock_this_device,
  ),
);

/// Plugin default used by earlier releases.
const legacySecureStorage = FlutterSecureStorage();

const omniSecureStorageKeys = <String>[
  'omni.gateway',
  'omni.token',
  'omni.actor',
  'omni.device_id.v2',
  'omni.device_public_key.v2',
  'omni.device_private_key.v2',
];

/// Moves items written with the old iOS accessibility to [current].
///
/// The plugin filters iOS reads by accessibility, so old items are invisible
/// to [current], and a plain write would collide with them. Each value is
/// staged under a temporary key first, so an interrupted migration resumes on
/// the next launch instead of losing the credential. On Android both handles
/// read the same item and this is a no-op.
Future<void> migrateLegacyKeychainItems({
  FlutterSecureStorage legacy = legacySecureStorage,
  FlutterSecureStorage current = omniSecureStorage,
  Iterable<String> keys = omniSecureStorageKeys,
}) async {
  for (final key in keys) {
    final staged = '$key.migrating';
    final stagedValue = await current.read(key: staged);
    if (await current.read(key: key) != null) {
      if (stagedValue != null) await current.delete(key: staged);
      continue;
    }
    final value = stagedValue ?? await legacy.read(key: key);
    if (value == null) continue;
    if (stagedValue == null) await current.write(key: staged, value: value);
    await legacy.delete(key: key);
    await current.write(key: key, value: value);
    await current.delete(key: staged);
  }
}
