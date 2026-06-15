import 'dart:convert';
import 'dart:math';

import 'package:cryptography/cryptography.dart';
import 'package:crypto/crypto.dart' as crypto;
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class OmniDeviceIdentity {
  const OmniDeviceIdentity({required this.deviceId, required this.publicKey});

  final String deviceId;
  final String publicKey;
}

class DeviceIdentityStore {
  DeviceIdentityStore(this.storage);

  final FlutterSecureStorage storage;
  static const _deviceIdKey = 'omni.device_id.v2';
  static const _publicKeyKey = 'omni.device_public_key.v2';
  static const _privateKeyKey = 'omni.device_private_key.v2';

  Future<OmniDeviceIdentity> loadOrCreate() async {
    final existingId = await storage.read(key: _deviceIdKey);
    final existingPublicKey = await storage.read(key: _publicKeyKey);
    final existingPrivateKey = await storage.read(key: _privateKeyKey);
    if (existingId != null && existingId.isNotEmpty && existingPublicKey != null && existingPublicKey.isNotEmpty && existingPrivateKey != null && existingPrivateKey.isNotEmpty) {
      return OmniDeviceIdentity(deviceId: existingId, publicKey: existingPublicKey);
    }

    final algorithm = Ed25519();
    final keyPair = await algorithm.newKeyPair();
    final publicKey = await keyPair.extractPublicKey();
    final privateBytes = await keyPair.extractPrivateKeyBytes();
    final deviceId = 'mob_${_randomHex(18)}';
    final publicKeyValue = 'base64:${base64Encode(publicKey.bytes)}';
    await storage.write(key: _deviceIdKey, value: deviceId);
    await storage.write(key: _publicKeyKey, value: publicKeyValue);
    await storage.write(key: _privateKeyKey, value: base64Encode(privateBytes));
    return OmniDeviceIdentity(deviceId: deviceId, publicKey: publicKeyValue);
  }

  Future<Map<String, String>> signRequest({required String method, required String path, String body = ''}) async {
    final deviceId = await storage.read(key: _deviceIdKey);
    final privateKeyEncoded = await storage.read(key: _privateKeyKey);
    if (deviceId == null || deviceId.isEmpty || privateKeyEncoded == null || privateKeyEncoded.isEmpty) {
      throw StateError('mobile device private key is not initialized');
    }
    final timestamp = DateTime.now().millisecondsSinceEpoch.toString();
    final nonce = _randomHex(24);
    final bodyHash = crypto.sha256.convert(utf8.encode(body)).toString();
    final message = 'omnidesk-device-request:v1:${method.toUpperCase()}:$path:$bodyHash:$timestamp:$nonce';
    final algorithm = Ed25519();
    final keyPair = await algorithm.newKeyPairFromSeed(base64Decode(privateKeyEncoded));
    final signature = await algorithm.sign(utf8.encode(message), keyPair: keyPair);
    return <String, String>{
      'x-omnidesk-device-id': deviceId,
      'x-omnidesk-timestamp': timestamp,
      'x-omnidesk-nonce': nonce,
      'x-omnidesk-device-signature': 'base64:${base64Encode(signature.bytes)}',
    };
  }

  static String _randomHex(int bytes) {
    final random = Random.secure();
    return List<int>.generate(bytes, (_) => random.nextInt(256)).map((b) => b.toRadixString(16).padLeft(2, '0')).join();
  }
}
