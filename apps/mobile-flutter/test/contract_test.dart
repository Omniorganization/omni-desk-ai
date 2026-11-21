import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:omnidesk_mobile/generated/contract.dart';
import 'package:omnidesk_mobile/omni_api.dart';

import 'contract_example.dart';

// The app checks every gateway answer against the contract it was built for
// (apps/shared/omni-app-api.contract.json). An older build in the field
// talking to a newer gateway fails with a clear message instead of half-working.

final int _major = int.parse(contractVersion.split('.').first);

OmniApiClient _client(
  Object? body, {
  String? version = contractVersion,
  int status = 200,
  List<http.BaseRequest>? sent,
}) => OmniApiClient(
  baseUrl: 'https://gateway.example.test',
  token: 'operator-token',
  actor: 'mobile-operator',
  httpClient: MockClient((http.Request request) async {
    sent?.add(request);
    return http.Response(
      jsonEncode(body),
      status,
      headers: <String, String>{
        if (version != null) contractHeader: version,
      },
    );
  }),
);

void main() {
  test('request paths map to their contract route', () {
    expect(
      contractRoute('post', '/app/approvals/appr_1/decide'),
      'POST /app/approvals/{approval_id}/decide',
    );
    expect(contractRoute('GET', '/app/notifications?audience=mobile'), 'GET /app/notifications');
    expect(contractRoute('GET', '/app/unknown'), isNull);
  });

  test('the requirement check names what is missing or mistyped', () {
    final rules = responseRequirements['GET /app/bootstrap'];
    expect(contractProblems(rules, exampleBody('GET /app/bootstrap')), isEmpty);
    expect(
      contractProblems(rules, <String, dynamic>{
        'pending_approvals': <dynamic>[
          <String, dynamic>{'approval_id': 7, 'action': 'a', 'risk': 'high'},
        ],
        'notifications': <dynamic>[],
        'runtime_status': <dynamic>[],
        'devices': <dynamic>[],
      }),
      <String>['pending_approvals[0].approval_id: expected string, got integer'],
    );
  });

  test('the app announces its contract and accepts a newer minor', () async {
    final sent = <http.BaseRequest>[];
    final snapshot = await _client(
      <String, dynamic>{...exampleBody('GET /app/bootstrap'), 'added_later': true},
      version: '$_major.99',
      sent: sent,
    ).bootstrap();
    expect(sent.single.headers[contractHeader], contractVersion);
    expect(snapshot['added_later'], isTrue);
  });

  test('a gateway of another major is refused, even when it answers 426', () async {
    expect(
      _client(exampleBody('GET /app/bootstrap'), version: '${_major + 1}.0').bootstrap(),
      throwsA(isA<ContractMismatchException>()),
    );
    expect(
      _client(<String, dynamic>{'code': 'contract_version_unsupported'}, version: '${_major + 1}.0', status: 426).bootstrap(),
      throwsA(isA<ContractMismatchException>()),
    );
  });

  test('a gateway from before negotiation is accepted while it meets the contract', () async {
    expect(await _client(exampleBody('GET /app/bootstrap'), version: null).bootstrap(), isA<Map<String, dynamic>>());
  });

  test('an answer missing what the app reads is refused', () async {
    expect(
      _client(<String, dynamic>{'conversation': <String, dynamic>{'title': 't'}}).createConversation('t'),
      throwsA(
        isA<ContractMismatchException>().having(
          (error) => error.message,
          'message',
          contains('conversation.conversation_id: missing'),
        ),
      ),
    );
  });
}
