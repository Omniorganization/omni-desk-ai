import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { OmniAdminApi } from '../lib/api';
import { resolveGatewayBaseUrl } from '../lib/gateway';
import { normalizeGatewayActor, verifyGatewayIdentity } from '../lib/session-login';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test('registerAdminDevice uses the server-side proxy and per-install web_admin identity', async () => {
  let requestUrl = '';
  let requestInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    requestUrl = input.toString();
    requestInit = init;
    return new Response(JSON.stringify({ ok: true, device: { device_type: 'web_admin' } }), { status: 200 });
  };

  const identity = {
    deviceId: 'web_1234567890abcdef1234567890abcdef1234',
    publicKeyPem: '-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----',
  };
  const api = new OmniAdminApi({ csrfToken: 'csrf-token', role: 'owner' });
  const result = await api.registerAdminDevice(identity);

  assert.equal(requestUrl, '/api/omni/devices/register');
  assert.equal(result.device.device_type, 'web_admin');
  assert.equal((requestInit?.headers as Record<string, string>)['x-csrf-token'], 'csrf-token');
  assert.equal((requestInit?.headers as Record<string, string>)['idempotency-key'], `web-admin-device-registration-${identity.deviceId}`);
  assert.equal((requestInit?.headers as Record<string, string>).authorization, undefined);

  const body = JSON.parse(requestInit?.body as string);
  assert.equal(body.device_id, identity.deviceId);
  assert.equal(body.device_type, 'web_admin');
  assert.equal(body.public_key, identity.publicKeyPem);
  assert.deepEqual(body.capabilities, ['governance', 'channels', 'audit', 'approval', 'role:owner']);
});

test('decide posts the owner approval decision and surfaces gateway errors', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const signed: Array<{ method: string; path: string; body: string }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ url: input.toString(), init });
    if (calls.length === 1) {
      return new Response(JSON.stringify({ ok: true, approval: { status: 'approved' } }), { status: 200 });
    }
    return new Response('forbidden', { status: 403 });
  };

  const api = new OmniAdminApi({
    csrfToken: 'csrf-token',
    deviceId: 'web_signed_device',
    deviceSigner: async (method, path, body) => {
      signed.push({ method, path, body });
      return {
        'x-omnidesk-device-id': 'web_signed_device',
        'x-omnidesk-timestamp': '123',
        'x-omnidesk-nonce': 'nonce-1234567890abcdef',
        'x-omnidesk-device-signature': 'base64:sig',
      };
    },
  });
  await api.decide('appr-1', 'approved');

  assert.equal(calls[0].url, '/api/omni/approvals/appr-1/decide');
  assert.match((calls[0].init?.headers as Record<string, string>)['idempotency-key'], /^web-admin-appr-1-approved-/);
  assert.equal((calls[0].init?.headers as Record<string, string>)['x-omnidesk-device-id'], 'web_signed_device');
  assert.deepEqual(signed.map(({ method, path }) => ({ method, path })), [
    { method: 'POST', path: '/app/approvals/appr-1/decide' },
  ]);
  assert.deepEqual(JSON.parse(calls[0].init?.body as string), {
    decision: 'approved',
    reason: 'Web Admin decision',
    source_device_id: 'web_signed_device',
  });

  await assert.rejects(() => api.bootstrap(), /403: request failed/);
});

test('askConversation posts through the server-side chat proxy with csrf only', async () => {
  let requestUrl = '';
  let requestInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    requestUrl = input.toString();
    requestInit = init;
    return new Response(JSON.stringify({ ok: true, assistant_message: { content: 'answer' } }), { status: 200 });
  };

  const api = new OmniAdminApi({ csrfToken: 'csrf-token', role: 'operator', deviceId: 'web_signed_device' });
  await api.askConversation('conv-1', 'hello', 'fast');

  assert.equal(requestUrl, '/api/omni/conversations/conv-1/ask');
  assert.equal((requestInit?.headers as Record<string, string>)['x-csrf-token'], 'csrf-token');
  assert.match((requestInit?.headers as Record<string, string>)['idempotency-key'], /^web-admin-ask-conv-1-5-/);
  assert.equal((requestInit?.headers as Record<string, string>).authorization, undefined);
  assert.deepEqual(JSON.parse(requestInit?.body as string), {
    content: 'hello',
    model_profile: 'fast',
    stream: false,
    source_device_id: 'web_signed_device',
  });
});

test('runtime status is fetched through the server-side proxy', async () => {
  let requestUrl = '';
  globalThis.fetch = async (input, init) => {
    requestUrl = input.toString();
    assert.equal((init?.headers as Record<string, string>)['x-csrf-token'], 'csrf-token');
    return new Response(JSON.stringify({ ok: true, runtime: { resource_guard: { backend: 'postgres' } } }), { status: 200 });
  };

  const api = new OmniAdminApi({ csrfToken: 'csrf-token' });
  const result = await api.runtime();

  assert.equal(requestUrl, '/api/omni/runtime');
  assert.equal(result.runtime.resource_guard.backend, 'postgres');
});

test('resolveGatewayBaseUrl rejects unlisted browser-supplied gateway URLs in production', () => {
  const env = {
    NODE_ENV: 'production',
    OMNI_GATEWAY_URL: 'https://gateway.company.example/base',
    OMNI_GATEWAY_URL_ALLOWLIST: 'https://gateway-backup.company.example',
    OMNI_ALLOW_CLIENT_GATEWAY_URLS: 'true',
  };

  assert.equal(resolveGatewayBaseUrl('https://169.254.169.254/latest/meta-data', env), 'https://gateway.company.example/base');
  assert.equal(resolveGatewayBaseUrl('https://gateway-backup.company.example/', env), 'https://gateway-backup.company.example');
  assert.throws(() => resolveGatewayBaseUrl('file:///etc/passwd', env), /http or https/);
});

test('verifyGatewayIdentity derives session actor and role from gateway response only', async () => {
  let requestUrl = '';
  let requestInit: RequestInit | undefined;
  const identity = await verifyGatewayIdentity('https://gateway.example/base/', 'secret-token', async (input, init) => {
    requestUrl = input.toString();
    requestInit = init;
    return new Response(JSON.stringify({ ok: true, actor: 'token:OMNIDESK_OPERATOR_TOKEN', role: 'operator' }), { status: 200 });
  });

  assert.equal(requestUrl, 'https://gateway.example/base/admin/session/identity');
  assert.equal((requestInit?.headers as Record<string, string>).authorization, 'Bearer secret-token');
  assert.equal((requestInit?.headers as Record<string, string>)['x-omnidesk-actor'], undefined);
  assert.deepEqual(identity, { actor: 'token:OMNIDESK_OPERATOR_TOKEN', role: 'operator' });
  assert.equal(normalizeGatewayActor(' system<script> '), 'systemscript');

  await assert.rejects(
    () => verifyGatewayIdentity('https://gateway.example', 'bad-token', async () => new Response('forbidden', { status: 403 })),
    /gateway identity verification failed: 403/,
  );
});
