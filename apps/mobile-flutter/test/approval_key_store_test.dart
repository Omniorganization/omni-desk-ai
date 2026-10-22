import 'dart:convert';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omnidesk_mobile/approval_key_store.dart';

void main() {
  setUp(() => FlutterSecureStorage.setMockInitialValues(<String, String>{}));

  test('decision signatures verify with the registered approval key', () async {
    final store = ApprovalKeyStore();
    final publicKey = await store.ensurePublicKey();
    expect(await store.ensurePublicKey(), publicKey, reason: 'stable key');

    final headers = await store.signDecision('appr_1', 'approved');
    final message =
        'omnidesk-approval-decision:v1:appr_1:approved:'
        '${headers['x-omnidesk-approval-timestamp']}:'
        '${headers['x-omnidesk-approval-nonce']}';
    final signature = headers['x-omnidesk-approval-signature']!;
    final valid = await Ed25519().verify(
      utf8.encode(message),
      signature: Signature(
        base64Decode(signature.substring('base64:'.length)),
        publicKey: SimplePublicKey(
          base64Decode(publicKey.substring('base64:'.length)),
          type: KeyPairType.ed25519,
        ),
      ),
    );
    expect(valid, isTrue);
    expect(headers['x-omnidesk-approval-nonce'], matches(RegExp(r'^[0-9a-f]{48}$')));
  });

  test('registration is tracked per device id', () async {
    final store = ApprovalKeyStore();
    expect(await store.isRegisteredFor('mob_1'), isFalse);
    await store.markRegisteredFor('mob_1');
    expect(await store.isRegisteredFor('mob_1'), isTrue);
    expect(await store.isRegisteredFor('mob_2'), isFalse);
  });

  test('signing without a key fails instead of sending unsigned decisions', () async {
    expect(
      () => ApprovalKeyStore().signDecision('appr_1', 'approved'),
      throwsStateError,
    );
  });
}
