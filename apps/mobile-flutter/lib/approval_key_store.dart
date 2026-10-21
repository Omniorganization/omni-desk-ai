import 'dart:convert';
import 'dart:math';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'secure_store.dart';

/// Private half of the approval key: readable only after biometric or device
/// passcode authentication, enforced by the OS (iOS Keychain access control,
/// Android Keystore user authentication), so app-level bypasses on a
/// compromised device cannot sign approvals.
const approvalKeyStorage = FlutterSecureStorage(
  iOptions: IOSOptions(
    accessibility: KeychainAccessibility.passcode,
    accessControlFlags: <AccessControlFlag>[AccessControlFlag.userPresence],
  ),
  aOptions: AndroidOptions.biometric(
    enforceBiometrics: true,
    requireBiometricsPerOperation: true,
    resetOnError: false,
    storageNamespace: 'omni_approval_key',
    biometricPromptTitle: 'Confirm Omni approval decision',
  ),
);

class ApprovalKeyStore {
  ApprovalKeyStore({
    FlutterSecureStorage? protectedStorage,
    FlutterSecureStorage? publicStorage,
  }) : _protected = protectedStorage ?? approvalKeyStorage,
       _public = publicStorage ?? omniSecureStorage;

  final FlutterSecureStorage _protected;
  final FlutterSecureStorage _public;
  static const _seedKey = 'omni.approval_key_seed.v1';
  static const _publicKeyKey = 'omni.approval_public_key.v1';
  static const _registeredKey = 'omni.approval_key_registered.v1';

  /// The approval public key (`base64:<32 bytes>`), creating the key pair on
  /// first use. Writing the seed may itself prompt for authentication.
  Future<String> ensurePublicKey() async {
    final existing = await _public.read(key: _publicKeyKey);
    if (existing != null && existing.isNotEmpty) return existing;
    final algorithm = Ed25519();
    final keyPair = await algorithm.newKeyPair();
    final seed = await keyPair.extractPrivateKeyBytes();
    final publicKey = await keyPair.extractPublicKey();
    final value = 'base64:${base64Encode(publicKey.bytes)}';
    await _protected.write(key: _seedKey, value: base64Encode(seed));
    await _public.write(key: _publicKeyKey, value: value);
    return value;
  }

  /// Whether the key was registered for [deviceId] (a re-enrolled device id
  /// registers again).
  Future<bool> isRegisteredFor(String deviceId) async =>
      await _public.read(key: _registeredKey) == deviceId;

  Future<void> markRegisteredFor(String deviceId) =>
      _public.write(key: _registeredKey, value: deviceId);

  /// Signs an approval decision. Reading the seed triggers the OS biometric
  /// or passcode prompt.
  Future<Map<String, String>> signDecision(
    String approvalId,
    String decision,
  ) async {
    final seed = await _protected.read(key: _seedKey);
    if (seed == null || seed.isEmpty) {
      throw StateError('approval key is not available');
    }
    final timestamp = DateTime.now().millisecondsSinceEpoch.toString();
    final random = Random.secure();
    final nonce = List<int>.generate(
      24,
      (_) => random.nextInt(256),
    ).map((b) => b.toRadixString(16).padLeft(2, '0')).join();
    final message =
        'omnidesk-approval-decision:v1:$approvalId:$decision:$timestamp:$nonce';
    final algorithm = Ed25519();
    final keyPair = await algorithm.newKeyPairFromSeed(base64Decode(seed));
    final signature = await algorithm.sign(
      utf8.encode(message),
      keyPair: keyPair,
    );
    return <String, String>{
      'x-omnidesk-approval-timestamp': timestamp,
      'x-omnidesk-approval-nonce': nonce,
      'x-omnidesk-approval-signature':
          'base64:${base64Encode(signature.bytes)}',
    };
  }
}
