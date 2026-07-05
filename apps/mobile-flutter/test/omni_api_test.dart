import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:omnidesk_mobile/device_identity.dart';
import 'package:omnidesk_mobile/omni_api.dart';

class TypedClientContractCase {
  const TypedClientContractCase({
    required this.surface,
    required this.method,
    required this.contractPath,
    required this.clientPath,
    required this.invoke,
    this.signedInProduction = false,
  });

  final String surface;
  final String method;
  final String contractPath;
  final String clientPath;
  final bool signedInProduction;
  final Future<dynamic> Function(OmniApiClient client) invoke;
}

const mobileTypedClientContractCases = <TypedClientContractCase>[
  TypedClientContractCase(
    surface: 'mobile',
    method: 'GET',
    contractPath: '/app/bootstrap',
    clientPath: '/app/bootstrap',
    invoke: _invokeBootstrap,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'POST',
    contractPath: '/app/devices/register',
    clientPath: '/app/devices/register',
    invoke: _invokeRegisterMobile,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'POST',
    contractPath: '/app/conversations',
    clientPath: '/app/conversations',
    invoke: _invokeCreateConversation,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'GET',
    contractPath: '/app/conversations/{conversation_id}/messages',
    clientPath: '/app/conversations/conv-1/messages',
    invoke: _invokeListMessages,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'POST',
    contractPath: '/app/conversations/{conversation_id}/messages',
    clientPath: '/app/conversations/conv-1/messages',
    invoke: _invokeSendMessage,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'POST',
    contractPath: '/app/conversations/{conversation_id}/ask',
    clientPath: '/app/conversations/conv-1/ask',
    invoke: _invokeAskConversation,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'POST',
    contractPath: '/app/approvals/{approval_id}/decide',
    clientPath: '/app/approvals/appr-1/decide',
    signedInProduction: true,
    invoke: _invokeDecideApproval,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'POST',
    contractPath: '/app/devices/{device_id}/push-token',
    clientPath: '/app/devices/mobile-1/push-token',
    signedInProduction: true,
    invoke: _invokeRegisterPushToken,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'GET',
    contractPath: '/app/notifications',
    clientPath: '/app/notifications',
    invoke: _invokeNotifications,
  ),
];

Future<dynamic> _invokeBootstrap(OmniApiClient client) => client.bootstrap();
Future<dynamic> _invokeRegisterMobile(OmniApiClient client) =>
    client.registerMobile(deviceId: 'mobile-1', pushToken: 'push-token');
Future<dynamic> _invokeCreateConversation(OmniApiClient client) =>
    client.createConversation('Typed mobile conversation');
Future<dynamic> _invokeListMessages(OmniApiClient client) =>
    client.listMessages('conv-1');
Future<dynamic> _invokeSendMessage(OmniApiClient client) => client.sendMessage(
  'conv-1',
  'Run desktop task',
  requiresDesktopRuntime: true,
  risk: 'high',
);
Future<dynamic> _invokeAskConversation(OmniApiClient client) =>
    client.askConversation('conv-1', 'Ask AI', sourceDeviceId: 'mobile-1');
Future<dynamic> _invokeDecideApproval(OmniApiClient client) =>
    client.decideApproval('appr-1', 'approved', sourceDeviceId: 'mobile-1');
Future<dynamic> _invokeRegisterPushToken(OmniApiClient client) =>
    client.registerPushToken('mobile-1', 'push-token');
Future<dynamic> _invokeNotifications(OmniApiClient client) =>
    client.notifications();

Future<DeviceIdentityStore> _newDeviceIdentityStore() async {
  FlutterSecureStorage.setMockInitialValues(<String, String>{});
  final store = DeviceIdentityStore(const FlutterSecureStorage());
  await store.loadOrCreate();
  return store;
}

Future<Map<String, Map<String, dynamic>>> _loadSharedContractIndex() async {
  final contractFile = File('../shared/omni-app-api.contract.json');
  final decoded =
      jsonDecode(await contractFile.readAsString()) as Map<String, dynamic>;
  final endpoints = decoded['endpoints'] as List<dynamic>;
  return <String, Map<String, dynamic>>{
    for (final endpoint in endpoints)
      '${(endpoint as Map<String, dynamic>)['method']} ${endpoint['path']}':
          endpoint,
  };
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'mobile typed client contract cases match shared contract and emitted requests',
    () async {
      final contract = await _loadSharedContractIndex();

      for (final contractCase in mobileTypedClientContractCases) {
        final sharedEntry =
            contract['${contractCase.method} ${contractCase.contractPath}'];
        expect(
          sharedEntry,
          isNotNull,
          reason:
              '${contractCase.method} ${contractCase.contractPath} must exist in shared contract',
        );
        expect(sharedEntry!['role'], isNot(isEmpty));
        final signedSurfaces =
            (sharedEntry['signed_device_required_in_production']
                        as List<dynamic>? ??
                    <dynamic>[])
                .map((value) => value.toString())
                .toSet();
        if (signedSurfaces.contains(contractCase.surface)) {
          expect(
            contractCase.signedInProduction,
            isTrue,
            reason:
                '${contractCase.contractPath} must be tested as signed in production',
          );
        }
        final deviceIdentityStore = contractCase.signedInProduction
            ? await _newDeviceIdentityStore()
            : null;

        final calls = <http.Request>[];
        final client = OmniApiClient(
          baseUrl: 'https://gateway.example.test',
          token: 'operator-token',
          actor: 'mobile-operator',
          deviceIdentityStore: deviceIdentityStore,
          httpClient: MockClient((http.Request request) async {
            calls.add(request);
            return http.Response(
              jsonEncode(<String, dynamic>{'ok': true}),
              200,
            );
          }),
        );

        await contractCase.invoke(client);
        expect(
          calls,
          hasLength(1),
          reason: '${contractCase.contractPath} should issue one request',
        );
        expect(calls.single.method, contractCase.method);
        expect(calls.single.url.path, contractCase.clientPath);
        expect(calls.single.headers['authorization'], 'Bearer operator-token');
        expect(calls.single.headers['x-omnidesk-actor'], 'mobile-operator');
        if (contractCase.signedInProduction) {
          expect(
            calls.single.headers['x-omnidesk-device-id'],
            startsWith('mob_'),
          );
          expect(calls.single.headers['x-omnidesk-timestamp'], isNotEmpty);
          expect(calls.single.headers['x-omnidesk-nonce'], isNotEmpty);
          expect(
            calls.single.headers['x-omnidesk-device-signature'],
            startsWith('base64:'),
          );
        }
        client.close();
      }
    },
  );

  test('registerMobile sends bearer auth and mobile capabilities', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test/',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((http.Request request) async {
        expect(
          request.url.toString(),
          'https://gateway.example.test/app/devices/register',
        );
        expect(request.headers['authorization'], 'Bearer operator-token');
        expect(request.headers['x-omnidesk-actor'], 'mobile-operator');
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['device_id'], 'mobile-1');
        expect(body['device_type'], 'mobile');
        expect(body['push_token'], 'push-token');
        expect(body['capabilities'], <String>[
          'chat',
          'approval',
          'notification',
        ]);
        return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
      }),
    );

    await client.registerMobile(deviceId: 'mobile-1', pushToken: 'push-token');
    client.close();
  });

  test(
    'sendMessage encodes path segments and uses the shared conversation endpoint',
    () async {
      final client = OmniApiClient(
        baseUrl: 'https://gateway.example.test',
        token: 'operator-token',
        actor: 'mobile-operator',
        httpClient: MockClient((http.Request request) async {
          expect(
            request.url.toString(),
            'https://gateway.example.test/app/conversations/conv-1%2Fwith%20space/messages',
          );
          final body = jsonDecode(request.body) as Map<String, dynamic>;
          expect(body['content'], 'Run desktop task');
          expect(body['requires_desktop_runtime'], isTrue);
          expect(body['risk'], 'high');
          return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
        }),
      );

      await client.sendMessage(
        'conv-1/with space',
        'Run desktop task',
        requiresDesktopRuntime: true,
        risk: 'high',
      );
      client.close();
    },
  );

  test('askConversation uses the shared audited chat endpoint', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((http.Request request) async {
        expect(
          request.url.toString(),
          'https://gateway.example.test/app/conversations/conv-1/ask',
        );
        expect(
          request.headers['idempotency-key'],
          startsWith('mobile-ask-conv-1-'),
        );
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['content'], 'Ask AI');
        expect(body['model_profile'], 'fast');
        expect(body['stream'], isFalse);
        expect(body['source_device_id'], 'mobile-1');
        return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
      }),
    );

    await client.askConversation(
      'conv-1',
      'Ask AI',
      sourceDeviceId: 'mobile-1',
    );
    client.close();
  });

  test(
    'non-2xx responses throw with status and safe error code only',
    () async {
      final client = OmniApiClient(
        baseUrl: 'https://gateway.example.test',
        token: 'bad-token',
        actor: 'mobile-operator',
        httpClient: MockClient((http.Request request) async {
          return http.Response('bad token', 401);
        }),
      );

      expect(
        () => client.bootstrap(),
        throwsA(
          predicate((Object error) {
            return error.toString().contains(
                  'Omni API request failed (401, request_failed)',
                ) &&
                !error.toString().contains('bad token');
          }),
        ),
      );
      client.close();
    },
  );

  test('baseUrl must use https unless loopback', () async {
    expect(
      () => OmniApiClient(
        baseUrl: 'http://gateway.example.test',
        token: 'token',
        actor: 'actor',
        httpClient: MockClient((http.Request request) async {
          return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
        }),
      ),
      throwsArgumentError,
    );
    final loopback = OmniApiClient(
      baseUrl: 'http://127.0.0.1:18789',
      token: 'token',
      actor: 'actor',
      httpClient: MockClient((http.Request request) async {
        return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
      }),
    );
    await loopback.bootstrap();
    loopback.close();
  });

  test('registerPushToken posts to device push endpoint', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((http.Request request) async {
        expect(
          request.url.toString(),
          'https://gateway.example.test/app/devices/mobile-1/push-token',
        );
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['push_token'], 'push-token');
        return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
      }),
    );
    await client.registerPushToken('mobile-1', 'push-token');
    client.close();
  });
}
