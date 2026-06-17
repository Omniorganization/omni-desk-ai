import 'package:flutter_test/flutter_test.dart';
import 'package:omnidesk_mobile/mobile_config.dart';

void main() {
  test('rejects empty gateway url', () {
    expect(OmniMobileConfig.validateGatewayUrl(''), contains('required'));
  });

  test('rejects phone-local loopback gateway urls', () {
    expect(OmniMobileConfig.validateGatewayUrl('http://127.0.0.1:18789'),
        contains('reachable from this phone'));
    expect(OmniMobileConfig.validateGatewayUrl('http://localhost:18789'),
        contains('reachable from this phone'));
  });

  test('accepts absolute lan or https gateway urls', () {
    expect(OmniMobileConfig.validateGatewayUrl('http://192.168.1.25:18789'),
        isNull);
    expect(OmniMobileConfig.validateGatewayUrl('https://gateway.example.test'),
        isNull);
  });
}
