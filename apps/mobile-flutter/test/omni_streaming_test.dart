import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:omnidesk_mobile/omni_api.dart';
import 'package:omnidesk_mobile/omni_streaming.dart';

class ControlledClient extends http.BaseClient {
  final StreamController<List<int>> controller =
      StreamController<List<int>>();
  http.BaseRequest? request;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    this.request = request;
    return http.StreamedResponse(
      controller.stream,
      200,
      headers: <String, String>{'content-type': 'text/event-stream'},
    );
  }

  @override
  void close() {
    unawaited(controller.close());
    super.close();
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('stream controller falls back only before the first event', () async {
    var calls = 0;
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'token',
      actor: 'actor',
      httpClient: MockClient((request) async {
        calls += 1;
        if (request.url.path == '/api/chat/stream') {
          return http.Response('upstream unavailable', 503);
        }
        expect(request.url.path, '/app/conversations/conv-1/ask');
        return http.Response(
          jsonEncode(<String, dynamic>{
            'assistant_message': <String, dynamic>{'content': 'fallback'},
          }),
          200,
        );
      }),
    );

    final result = await client.streamChatWithFallback(
      conversationId: 'conv-1',
      content: 'hello',
      idempotencyKey: 'idem-1',
      onEvent: (_) => fail('no stream event should be observed'),
    );

    expect(result.streamed, isFalse);
    expect(result.completed, isTrue);
    expect(result.cancelled, isFalse);
    expect(result.fallbackResult?['assistant_message']['content'], 'fallback');
    expect(calls, 2);
    client.close();
  });

  test('stream controller reconnects with the last observed event id', () async {
    final requests = <http.Request>[];
    var call = 0;
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'token',
      actor: 'actor',
      httpClient: MockClient((request) async {
        requests.add(request);
        call += 1;
        if (call == 1) {
          return http.Response(
            'id: 1\nevent: chat.started\ndata: {"conversation_id":"conv-1"}\n\n'
            'id: 2\nevent: chat.delta\ndata: {"text":"hel"}\n\n'
            'id: 3\nevent: chat.failed\ndata: {"code":"transport_lost"}\n\n',
            200,
            headers: <String, String>{'content-type': 'text/event-stream'},
          );
        }
        return http.Response(
          'id: 3\nevent: chat.delta\ndata: {"text":"ignored"}\n\n'
          'id: 4\nevent: chat.delta\ndata: {"text":"lo"}\n\n'
          'id: 5\nevent: chat.completed\ndata: {"native":true}\n\n',
          200,
          headers: <String, String>{'content-type': 'text/event-stream'},
        );
      }),
    );

    final observed = <ChatStreamEvent>[];
    final result = await client.streamChatWithFallback(
      conversationId: 'conv-1',
      content: 'hello',
      idempotencyKey: 'idem-reconnect',
      maxReconnects: 1,
      onEvent: observed.add,
    );

    expect(result.streamed, isTrue);
    expect(result.completed, isTrue);
    expect(result.lastEventId, 5);
    expect(observed.map((event) => event.id), <int>[1, 2, 3, 4, 5]);
    expect(observed[2].event, 'chat.failed');
    expect(requests, hasLength(2));
    expect(requests.last.headers['last-event-id'], '3');
    client.close();
  });

  test('MobileChatStreamSession cancellation closes the active subscription', () async {
    final transport = ControlledClient();
    final client = OmniApiClient(
      baseUrl: 'https://gateway.example.test',
      token: 'token',
      actor: 'actor',
      httpClient: transport,
    );
    final session = client.startChatStreamSession(
      conversationId: 'conv-1',
      content: 'long response',
      idempotencyKey: 'idem-cancel',
    );
    final events = <ChatStreamEvent>[];
    final subscription = session.events.listen(events.add);

    transport.controller.add(
      utf8.encode(
        'id: 1\nevent: chat.started\ndata: {"conversation_id":"conv-1"}\n\n',
      ),
    );
    await Future<void>.delayed(Duration.zero);
    await session.cancel();
    final result = await session.result;

    expect(result.cancelled, isTrue);
    expect(result.completed, isFalse);
    expect(result.streamed, isTrue);
    expect(result.lastEventId, 1);
    expect(events.single.event, 'chat.started');
    await subscription.cancel();
    client.close();
  });
}
