import assert from 'node:assert/strict';
import test from 'node:test';

import { OmniApiClient, parseSseBlock } from '../src/api';

test('parseSseBlock accepts monotonic JSON events and ignores heartbeat', () => {
  assert.equal(parseSseBlock(': heartbeat'), null);
  assert.deepEqual(parseSseBlock('id: 3\nevent: chat.delta\ndata: {"text":"hello"}'), { id: 3, event: 'chat.delta', data: { text: 'hello' } });
  assert.deepEqual(parseSseBlock('id: 4\nevent: chat.completed\ndata: {"native":true}'), { id: 4, event: 'chat.completed', data: { native: true } });
  assert.equal(parseSseBlock('id: -1\nevent: chat.delta\ndata: {}'), null);
});

test('Desktop stream sends signed resume and idempotency headers', async () => {
  const originalFetch = globalThis.fetch;
  let observed: Request | null = null;
  globalThis.fetch = async (input, init) => {
    observed = new Request(input, init);
    return new Response('id: 3\nevent: chat.delta\ndata: {"text":"hello"}\n\nid: 4\nevent: chat.completed\ndata: {"native":true}\n\n', {
      status: 200,
      headers: { 'content-type': 'text/event-stream' },
    });
  };
  try {
    const events: string[] = [];
    const client = new OmniApiClient({
      baseUrl: 'https://gateway.example.test', token: 'token', actor: 'desktop',
      deviceSigner: async () => ({ 'x-omnidesk-device-signature': 'signature' }),
    });
    const result = await client.streamChat({
      conversationId: 'conv-1', content: 'hello', idempotencyKey: 'idem-1', lastEventId: 2,
      onEvent: event => events.push(event.event),
    });
    assert.deepEqual(events, ['chat.delta', 'chat.completed']);
    assert.equal(result.lastEventId, 4);
    assert.equal(result.completed, true);
    assert.ok(observed);
    const headers = (observed as Request).headers;
    assert.equal(headers.get('last-event-id'), '2');
    assert.equal(headers.get('idempotency-key'), 'idem-1');
    assert.equal(headers.get('x-omnidesk-device-signature'), 'signature');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Desktop stream propagates AbortSignal cancellation', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_input, init) => new Promise((_resolve, reject) => {
    const signal = init?.signal;
    const rejectAbort = () => reject(new DOMException('Aborted', 'AbortError'));
    if (signal?.aborted) {
      rejectAbort();
    } else {
      signal?.addEventListener('abort', rejectAbort, { once: true });
    }
  });
  try {
    const client = new OmniApiClient({ baseUrl: 'https://gateway.example.test', token: 'token', actor: 'desktop' });
    const controller = new AbortController();
    const pending = client.streamChat({ conversationId: 'conv-1', content: 'long response', signal: controller.signal, onEvent: () => undefined });
    controller.abort('operator_stop');
    await assert.rejects(pending, error => error instanceof DOMException && error.name === 'AbortError');
  } finally {
    globalThis.fetch = originalFetch;
  }
});
