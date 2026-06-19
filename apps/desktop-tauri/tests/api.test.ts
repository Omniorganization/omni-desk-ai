import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { OmniApiClient } from '../src/api';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test('desktop registration trims the gateway URL and sends operator identity', async () => {
  let requestUrl = '';
  let requestInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    requestUrl = input.toString();
    requestInit = init;
    return new Response(JSON.stringify({ ok: true, device: { device_id: 'desktop-1' } }), { status: 200 });
  };

  const client = new OmniApiClient({ baseUrl: 'https://gateway.example.test/', token: 'operator-token', actor: 'desktop-operator' });
  const result = await client.registerDesktop('desktop-1', 'macOS', ['local-runtime']);

  assert.equal(requestUrl, 'https://gateway.example.test/app/devices/register');
  assert.equal(result.device.device_id, 'desktop-1');
  assert.equal((requestInit?.headers as Record<string, string>).authorization, 'Bearer operator-token');
  assert.equal((requestInit?.headers as Record<string, string>)['x-omnidesk-actor'], 'desktop-operator');
  assert.deepEqual(JSON.parse(requestInit?.body as string), {
    device_id: 'desktop-1',
    device_type: 'desktop',
    name: 'Omni Desktop Runtime',
    platform: 'macOS',
    capabilities: ['local-runtime'],
  });
});

test('heartbeat, task status, and sync use the shared /app contract', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ url: input.toString(), init });
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };

  const client = new OmniApiClient({ baseUrl: 'https://gateway.example.test', token: 'operator-token', actor: 'desktop-operator' });
  await client.heartbeat('desktop-1', 'online', '1.08', ['local-runtime']);
  await client.updateTaskStatus('task-1', 'completed', 'done', 'desktop-1');
  await client.sync(42);

  assert.equal(calls[0].url, 'https://gateway.example.test/app/runtime/desktop/heartbeat');
  assert.equal(calls[1].url, 'https://gateway.example.test/app/tasks/task-1/status');
  assert.equal(calls[2].url, 'https://gateway.example.test/app/sync?since_seq=42');
  assert.equal(JSON.parse(calls[1].init?.body as string).status, 'completed');
});

test('askConversation uses the shared audited chat endpoint', async () => {
  let requestUrl = '';
  let requestInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    requestUrl = input.toString();
    requestInit = init;
    return new Response(JSON.stringify({ ok: true, assistant_message: { content: 'answer' } }), { status: 200 });
  };

  const client = new OmniApiClient({ baseUrl: 'https://gateway.example.test/', token: 'operator-token', actor: 'desktop-operator' });
  await client.askConversation('conv-1', 'hello desktop', 'fast', 'desktop-1');

  assert.equal(requestUrl, 'https://gateway.example.test/app/conversations/conv-1/ask');
  assert.match((requestInit?.headers as Record<string, string>)['idempotency-key'], /^desktop-ask-conv-1-13-/);
  assert.deepEqual(JSON.parse(requestInit?.body as string), {
    content: 'hello desktop',
    model_profile: 'fast',
    stream: false,
    source_device_id: 'desktop-1',
  });
});

test('gateway errors include the response body', async () => {
  globalThis.fetch = async () => new Response('bad token', { status: 401 });

  const client = new OmniApiClient({ baseUrl: 'https://gateway.example.test', token: 'bad-token', actor: 'desktop-operator' });

  await assert.rejects(() => client.bootstrap(), /401 bad token/);
});
