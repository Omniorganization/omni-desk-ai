import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
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
    method: 'GET',
    contractPath: '/app/projects',
    clientPath: '/app/projects',
    invoke: _invokeProjects,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'POST',
    contractPath: '/app/projects',
    clientPath: '/app/projects',
    invoke: _invokeCreateProject,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'PATCH',
    contractPath: '/app/projects/{project_id}',
    clientPath: '/app/projects/proj_1234567890abcdef',
    invoke: _invokeUpdateProject,
  ),
  TypedClientContractCase(
    surface: 'mobile',
    method: 'DELETE',
    contractPath: '/app/projects/{project_id}',
    clientPath: '/app/projects/proj_1234567890abcdef',
    invoke: _invokeDeleteProject,
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
Future<dynamic> _invokeProjects(OmniApiClient client) => client.projects();
Future<dynamic> _invokeCreateProject(OmniApiClient client) =>
    client.createProject('Typed mobile project', sourceDeviceId: 'mobile-1');
Future<dynamic> _invokeUpdateProject(OmniApiClient client) => client.updateProject(
      'proj_1234567890abcdef',
      name: 'Renamed mobile project',
    );
Future<dynamic> _invokeDeleteProject(OmniApiClient client) =>
    client.deleteProject('proj_1234567890abcdef');
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

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('mobile typed client cases emit expected authenticated requests', () async {
    for (final contractCase in mobileTypedClientContractCases) {
      expect(contractCase.surface, 'mobile');
      if (contractCase.signedInProduction) {
        expect(
          contractCase.contractPath,
          anyOf(contains('/approvals/'), contains('/push-token')),
        );
      }

      final calls = <http.Request>[];
      final client = OmniApiClient(
        baseUrl: 'https://gateway.example.test',
        token: 'operator-token',
        actor: 'mobile-operator',
        httpClient: MockClient((http.Request request) async {
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

  test('registerMobile advertises implemented mobile capabilities', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test/',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((http.Request request) async {
        expect(request.url.toString(),
            'https://gateway.example.test/app/devices/register');
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['device_id'], 'mobile-1');
        expect(body['device_type'], 'mobile');
        expect(body['push_token'], 'push-token');
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

  test('createProject preserves an explicit idempotency key', () async {
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'operator-token',
      actor: 'mobile-operator',
      httpClient: MockClient((http.Request request) async {
        expect(request.headers['idempotency-key'],
            'mobile-project-create-op-123');
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['name'], 'Mobile Launch');
        expect(body['source_device_id'], 'mobile-1');
        return http.Response(jsonEncode(<String, dynamic>{'ok': true}), 200);
      }),
    );

    await client.createProject(
      'Mobile Launch',
      sourceDeviceId: 'mobile-1',
      idempotencyKey: 'mobile-project-create-op-123',
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
