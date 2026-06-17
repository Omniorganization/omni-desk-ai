import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:omnidesk_mobile/omni_api.dart';

void main() {
  test('registerMobile sends bearer auth and mobile capabilities', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test/',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((http.Request request) async {
        expect(request.url.toString(),
            'https://gateway.example.test/app/devices/register');
        expect(request.headers['authorization'], 'Bearer operator-token');
        expect(request.headers['x-omnidesk-actor'], 'mobile-operator');
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['device_id'], 'mobile-1');
        expect(body['device_type'], 'mobile');
        expect(body['push_token'], 'push-token');
        expect(
            body['capabilities'], <String>['chat', 'approval', 'notification']);
        return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
      }),
    );

    await client.registerMobile(deviceId: 'mobile-1', pushToken: 'push-token');
    client.close();
  });

  test('sendMessage uses the shared conversation message endpoint', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((http.Request request) async {
        expect(request.url.toString(),
            'https://gateway.example.test/app/conversations/conv-1/messages');
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['content'], 'Run desktop task');
        expect(body['requires_desktop_runtime'], isTrue);
        expect(body['risk'], 'high');
        return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
      }),
    );

    await client.sendMessage('conv-1', 'Run desktop task',
        requiresDesktopRuntime: true, risk: 'high');
    client.close();
  });

  test('non-2xx responses throw with status and body', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'bad-token',
      actor: 'mobile-operator',
      httpClient: MockClient((http.Request request) async {
        return http.Response('bad token', 401);
      }),
    );

    expect(() => client.bootstrap(), throwsA(predicate((Object error) {
      return error.toString().contains('401: bad token');
    })));
    client.close();
  });

  test('registerPushToken posts to device push endpoint', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((http.Request request) async {
        expect(request.url.toString(),
            'https://gateway.example.test/app/devices/mobile-1/push-token');
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['push_token'], 'push-token');
        return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
      }),
    );
    await client.registerPushToken('mobile-1', 'push-token');
    client.close();
  });
}
