import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { OmniAdminApi } from '../lib/api';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test('streamChat sends resume headers and emits only newer SSE events', async () => {
  let requestUrl = '';
  let requestInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    requestUrl = input.toString();
    requestInit = init;
    return new Response(
      'id: 1\nevent: chat.started\ndata: {"conversation_id":"conv-1"}\n\n'
      + ': heartbeat\n\n'
      + 'id: 2\nevent: chat.delta\ndata: {"text":"hello"}\n\n'
      + 'id: 3\nevent: chat.completed\ndata: {"native":true}\n\n',
      {
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
      },
    );
  };

  const events: Array<{ id: number; event: string }> = [];
  const api = new OmniAdminApi({
    csrfToken: 'csrf-token',
    role: 'operator',
    deviceId: 'web-device',
  });
  const result = await api.streamChat({
    conversationId: 'conv-1',
    content: 'hello',
    modelProfile: 'fast',
    idempotencyKey: 'web-stream-test',
    lastEventId: 1,
    onEvent: (event) => events.push({ id: event.id, event: event.event }),
  });

  assert.equal(requestUrl, '/api/omni/chat/stream');
  assert.equal(requestInit?.method, 'POST');
  const headers = requestInit?.headers as Record<string, string>;
  assert.equal(headers.accept, 'text/event-stream');
  assert.equal(headers['x-csrf-token'], 'csrf-token');
  assert.equal(headers['idempotency-key'], 'web-stream-test');
  assert.equal(headers['last-event-id'], '1');
  assert.deepEqual(events, [
    { id: 2, event: 'chat.delta' },
    { id: 3, event: 'chat.completed' },
  ]);
  assert.deepEqual(result, { lastEventId: 3, completed: true });
});

test('streamChatWithFallback degrades only before the first visible event', async () => {
  const calls: string[] = [];
  globalThis.fetch = async (input) => {
    const url = input.toString();
    calls.push(url);
    if (url === '/api/omni/chat/stream') {
      return new Response('gateway unavailable', { status: 503 });
    }
    return new Response(
      JSON.stringify({
        ok: true,
        assistant_message: { content: 'fallback answer' },
      }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    );
  };

  const api = new OmniAdminApi({ csrfToken: 'csrf-token', role: 'operator' });
  const result = await api.streamChatWithFallback({
    conversationId: 'conv-1',
    content: 'hello',
    idempotencyKey: 'web-stream-fallback',
    onEvent: () => assert.fail('no stream event should be observed'),
  });

  assert.equal(result.streamed, false);
  assert.equal(result.result.assistant_message.content, 'fallback answer');
  assert.deepEqual(calls, [
    '/api/omni/chat/stream',
    '/api/omni/conversations/conv-1/ask',
  ]);
});

test('streamChat fails closed after a visible chat.failed event', async () => {
  globalThis.fetch = async () => new Response(
    'id: 1\nevent: chat.started\ndata: {"conversation_id":"conv-1"}\n\n'
    + 'id: 2\nevent: chat.failed\ndata: {"code":"provider_unavailable"}\n\n',
    { status: 200, headers: { 'content-type': 'text/event-stream' } },
  );

  const observed: string[] = [];
  const api = new OmniAdminApi({ csrfToken: 'csrf-token', role: 'operator' });
  await assert.rejects(
    () => api.streamChatWithFallback({
      conversationId: 'conv-1',
      content: 'hello',
      onEvent: (event) => observed.push(event.event),
    }),
    /provider_unavailable/,
  );
  assert.deepEqual(observed, ['chat.started', 'chat.failed']);
});
