import 'dart:convert';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omnidesk_mobile/device_identity.dart';

void main() {
  setUp(() => FlutterSecureStorage.setMockInitialValues(<String, String>{}));

  test('challenge signatures verify with the enrolled public key', () async {
    final store = DeviceIdentityStore(const FlutterSecureStorage());
    final identity = await store.loadOrCreate();
    const message = 'omnidesk-device-challenge:v2:chal_123:abc123';
    final signature = await store.signChallenge(message);
    expect(signature, startsWith('base64:'));

    final publicKey = SimplePublicKey(
      base64Decode(identity.publicKey.substring('base64:'.length)),
      type: KeyPairType.ed25519,
    );
    final valid = await Ed25519().verify(
      utf8.encode(message),
      signature: Signature(
        base64Decode(signature.substring('base64:'.length)),
        publicKey: publicKey,
      ),
    );
    expect(valid, isTrue);
  });

  test('only gateway challenge messages can be signed', () async {
    final store = DeviceIdentityStore(const FlutterSecureStorage());
    await store.loadOrCreate();
    expect(
      () => store.signChallenge('omnidesk-device-request:v1:POST:/x:h:1:n'),
      throwsArgumentError,
    );
  });

  test('pairing codes are 16 hex characters and unique', () {
    final a = DeviceIdentityStore.newPairingCode();
    final b = DeviceIdentityStore.newPairingCode();
    expect(a, matches(RegExp(r'^[0-9a-f]{16}$')));
    expect(a, isNot(b));
  });
}
