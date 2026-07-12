import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:omnidesk_mobile/omni_api.dart';

class TypedClientContractCase {
  const TypedClientContractCase({
    required this.method,
    required this.clientPath,
    required this.invoke,
  });

  final String method;
  final String clientPath;
  final Future<dynamic> Function(OmniApiClient client) invoke;
}

final mobileTypedClientContractCases = <TypedClientContractCase>[
  TypedClientContractCase(
    method: 'GET',
    clientPath: '/app/bootstrap',
    invoke: (client) => client.bootstrap(),
  ),
  TypedClientContractCase(
    method: 'GET',
    clientPath: '/app/projects',
    invoke: (client) => client.projects(),
  ),
  TypedClientContractCase(
    method: 'POST',
    clientPath: '/app/projects',
    invoke: (client) => client.createProject(
      'Typed mobile project',
      sourceDeviceId: 'mobile-1',
    ),
  ),
  TypedClientContractCase(
    method: 'PATCH',
    clientPath: '/app/projects/proj_1234567890abcdef',
    invoke: (client) => client.updateProject(
      'proj_1234567890abcdef',
      name: 'Renamed mobile project',
    ),
  ),
  TypedClientContractCase(
    method: 'DELETE',
    clientPath: '/app/projects/proj_1234567890abcdef',
    invoke: (client) => client.deleteProject('proj_1234567890abcdef'),
  ),
  TypedClientContractCase(
    method: 'POST',
    clientPath: '/app/devices/register',
    invoke: (client) => client.registerMobile(
      deviceId: 'mobile-1',
      pushToken: 'push-token',
    ),
  ),
  TypedClientContractCase(
    method: 'POST',
    clientPath: '/app/conversations',
    invoke: (client) => client.createConversation('Typed mobile conversation'),
  ),
  TypedClientContractCase(
    method: 'GET',
    clientPath: '/app/conversations/conv-1/messages',
    invoke: (client) => client.listMessages('conv-1'),
  ),
  TypedClientContractCase(
    method: 'POST',
    clientPath: '/app/conversations/conv-1/messages',
    invoke: (client) => client.sendMessage(
      'conv-1',
      'Run desktop task',
      requiresDesktopRuntime: true,
      risk: 'high',
    ),
  ),
  TypedClientContractCase(
    method: 'POST',
    clientPath: '/app/conversations/conv-1/ask',
    invoke: (client) => client.askConversation(
      'conv-1',
      'Ask AI',
      sourceDeviceId: 'mobile-1',
    ),
  ),
  TypedClientContractCase(
    method: 'POST',
    clientPath: '/app/approvals/appr-1/decide',
    invoke: (client) => client.decideApproval(
      'appr-1',
      'approved',
      sourceDeviceId: 'mobile-1',
    ),
  ),
  TypedClientContractCase(
    method: 'POST',
    clientPath: '/app/devices/mobile-1/push-token',
    invoke: (client) => client.registerPushToken('mobile-1', 'push-token'),
  ),
  TypedClientContractCase(
    method: 'GET',
    clientPath: '/app/notifications',
    invoke: (client) => client.notifications(),
  ),
];

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('mobile typed client cases emit expected authenticated requests', () async {
    for (final contractCase in mobileTypedClientContractCases) {
      final calls = <http.Request>[];
      final client = OmniApiClient(
        baseUrl: 'https://gateway.example.test',
        token: 'operator-token',
        actor: 'mobile-operator',
        httpClient: MockClient((request) async {
          calls.add(request);
          return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
        }),
      );

      await contractCase.invoke(client);
      expect(calls, hasLength(1));
      expect(calls.single.method, contractCase.method);
      expect(calls.single.url.path, contractCase.clientPath);
      expect(calls.single.headers['authorization'], 'Bearer operator-token');
      expect(calls.single.headers['x-omnidesk-actor'], 'mobile-operator');
      client.close();
    }
  });

  test('registerMobile advertises only implemented mobile capabilities', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test/',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((request) async {
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['device_type'], 'mobile');
        expect(body['capabilities'], <String>[
          'chat',
          'chat-streaming',
          'approval',
          'notification',
        ]);
        return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
      }),
    );

    await client.registerMobile(deviceId: 'mobile-1', pushToken: 'push-token');
    client.close();
  });

  test('streamChat parses monotonic SSE events and sends resume headers', () async {
    final requests = <http.Request>[];
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((request) async {
        requests.add(request);
        return http.Response(
          'id: 3\nevent: chat.delta\ndata: {"text":"hello"}\n\n'
          ': heartbeat\n\n'
          'id: 4\nevent: chat.completed\ndata: {"native":true}\n\n',
          200,
          headers: <String, String>{'content-type': 'text/event-stream'},
        );
      }),
    );

    final events = await client.streamChat(
      conversationId: 'conv-1',
      content: 'stream this',
      sourceDeviceId: 'mobile-1',
      idempotencyKey: 'mobile-stream-1',
      lastEventId: 2,
    ).toList();

    expect(events.map((event) => event.id), <int>[3, 4]);
    expect(events.first.event, 'chat.delta');
    expect(events.first.data['text'], 'hello');
    expect(events.last.event, 'chat.completed');
    expect(requests.single.url.path, '/api/chat/stream');
    expect(requests.single.headers['accept'], 'text/event-stream');
    expect(requests.single.headers['last-event-id'], '2');
    expect(requests.single.headers['idempotency-key'], 'mobile-stream-1');
    client.close();
  });

  test('streamChat fails closed on chat.failed events', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((request) async => http.Response(
        'id: 1\nevent: chat.failed\ndata: {"code":"provider_unavailable"}\n\n',
        200,
        headers: <String, String>{'content-type': 'text/event-stream'},
      )),
    );

    await expectLater(
      client.streamChat(content: 'hello').toList(),
      throwsA(contains('provider_unavailable')),
    );
    client.close();
  });

  test('base URL rejects non-loopback cleartext transport', () {
    expect(
      () => OmniApiClient(
        baseUrl: 'http://gateway.example.test',
        token: 'token',
        actor: 'actor',
      ),
      throwsArgumentError,
    );
  });
}
