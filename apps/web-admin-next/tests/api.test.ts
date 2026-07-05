import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, test } from 'node:test';
import { OmniAdminApi } from '../lib/api';
import { resolveGatewayBaseUrl } from '../lib/gateway';
import { normalizeGatewayActor, verifyGatewayIdentity } from '../lib/session-login';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

type ContractMethod = 'GET' | 'POST';
type SharedContractEntry = { role: string; signed: readonly string[] };
type BaseTypedContractCase = {
  readonly surface: 'web_admin';
  readonly method: ContractMethod;
  readonly contractPath: string;
  readonly clientPath: string;
  readonly invoke: (api: OmniAdminApi) => Promise<unknown>;
};

type UnsignedTypedContractCase = BaseTypedContractCase & {
  readonly signedInProduction?: false;
  readonly signedPath?: never;
};

type SignedTypedContractCase = BaseTypedContractCase & {
  readonly signedInProduction: true;
  readonly signedPath: string;
};

type TypedContractCase = UnsignedTypedContractCase | SignedTypedContractCase;

const WEB_ADMIN_TYPED_CLIENT_CONTRACT_CASES: readonly TypedContractCase[] = [
  { surface: 'web_admin', method: 'GET', contractPath: '/app/bootstrap', clientPath: '/api/omni/bootstrap', invoke: (api) => api.bootstrap() },
  { surface: 'web_admin', method: 'POST', contractPath: '/app/devices/register', clientPath: '/api/omni/devices/register', invoke: (api) => api.registerAdminDevice({ deviceId: 'web_1234567890abcdef1234567890abcdef1234', publicKeyPem: '-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----' }) },
  { surface: 'web_admin', method: 'GET', contractPath: '/app/conversations', clientPath: '/api/omni/conversations', invoke: (api) => api.conversations() },
  { surface: 'web_admin', method: 'POST', contractPath: '/app/conversations', clientPath: '/api/omni/conversations', invoke: (api) => api.createConversation('Typed contract conversation') },
  { surface: 'web_admin', method: 'GET', contractPath: '/app/conversations/{conversation_id}/messages', clientPath: '/api/omni/conversations/conv-1/messages', invoke: (api) => api.listMessages('conv-1') },
  { surface: 'web_admin', method: 'POST', contractPath: '/app/conversations/{conversation_id}/ask', clientPath: '/api/omni/conversations/conv-1/ask', invoke: (api) => api.askConversation('conv-1', 'hello', 'fast') },
  { surface: 'web_admin', method: 'GET', contractPath: '/app/approvals', clientPath: '/api/omni/approvals', invoke: (api) => api.approvals() },
  { surface: 'web_admin', method: 'POST', contractPath: '/app/approvals/{approval_id}/decide', clientPath: '/api/omni/approvals/appr-1/decide', signedPath: '/app/approvals/appr-1/decide', signedInProduction: true, invoke: (api) => api.decide('appr-1', 'approved') },
  { surface: 'web_admin', method: 'GET', contractPath: '/app/notifications', clientPath: '/api/omni/notifications', invoke: (api) => api.notifications() },
  { surface: 'web_admin', method: 'POST', contractPath: '/app/devices/enrollment/start', clientPath: '/api/omni/devices/enrollment/start', invoke: (api) => api.startDeviceEnrollment('web_admin', 'pair-123') },
  { surface: 'web_admin', method: 'POST', contractPath: '/app/devices/enrollment/{enrollment_id}/complete', clientPath: '/api/omni/devices/enrollment/enroll-1/complete', invoke: (api) => api.completeDeviceEnrollment('enroll-1', 'pair-123', { deviceId: 'web_1234567890abcdef1234567890abcdef1234', publicKeyPem: '-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----' }) },
  { surface: 'web_admin', method: 'POST', contractPath: '/app/devices/enrollment/{enrollment_id}/challenge', clientPath: '/api/omni/devices/enrollment/enroll-1/challenge', invoke: (api) => api.issueDeviceChallenge('enroll-1', 'web_1234567890abcdef1234567890abcdef1234') },
  { surface: 'web_admin', method: 'POST', contractPath: '/app/devices/enrollment/{enrollment_id}/verify', clientPath: '/api/omni/devices/enrollment/enroll-1/verify', invoke: (api) => api.verifyDeviceChallenge('enroll-1', 'challenge-1', 'web_1234567890abcdef1234567890abcdef1234', 'base64:sig') },
];

function loadSharedContractIndex(): Map<string, SharedContractEntry> {
  const contractPath = join(process.cwd(), '..', 'shared', 'omni-app-api.contract.json');
  const contract = JSON.parse(readFileSync(contractPath, 'utf8')) as { endpoints: Array<Record<string, unknown>> };
  return new Map(contract.endpoints.map((endpoint) => [
    `${String(endpoint.method)} ${String(endpoint.path)}`,
    {
      role: String(endpoint.role),
      signed: Array.isArray(endpoint.signed_device_required_in_production)
        ? endpoint.signed_device_required_in_production.map(String)
        : [],
    },
  ]));
}

test('web admin typed client contract cases match shared contract and emitted requests', async () => {
  const contract = loadSharedContractIndex();

  for (const contractCase of WEB_ADMIN_TYPED_CLIENT_CONTRACT_CASES) {
    const sharedEntry = contract.get(`${contractCase.method} ${contractCase.contractPath}`);
    assert.ok(sharedEntry, `${contractCase.method} ${contractCase.contractPath} must exist in shared contract`);
    assert.ok(sharedEntry.role.length > 0, `${contractCase.contractPath} must declare a role`);
    if (sharedEntry.signed.includes(contractCase.surface)) {
      assert.equal(contractCase.signedInProduction, true, `${contractCase.contractPath} must be tested as signed in production`);
      assert.ok(contractCase.signedPath, `${contractCase.contractPath} must expose the canonical signed gateway path`);
    }

    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const signedCalls: Array<{ method: string; path: string; body: string }> = [];
    globalThis.fetch = async (input, init) => {
      calls.push({ url: input.toString(), init });
      return new Response(JSON.stringify({ ok: true, device: { device_type: 'web_admin' }, approval: { status: 'approved' }, runtime: {}, assistant_message: { content: 'ok' } }), { status: 200 });
    };

    const api = new OmniAdminApi({
      csrfToken: 'csrf-token',
      role: 'owner',
      deviceId: 'web_signed_device',
      deviceSigner: async (method, path, body) => {
        signedCalls.push({ method, path, body });
        return {
          'x-omnidesk-device-id': 'web_signed_device',
          'x-omnidesk-timestamp': '123',
          'x-omnidesk-nonce': 'nonce-1234567890abcdef',
          'x-omnidesk-device-signature': 'base64:sig',
        };
      },
    });

    await contractCase.invoke(api);
    assert.equal(calls.length, 1, `${contractCase.contractPath} should issue one request`);
    assert.equal(calls[0].url.split('?', 1)[0], contractCase.clientPath);
    assert.equal(String(calls[0].init?.method ?? 'GET').toUpperCase(), contractCase.method);
    assert.equal((calls[0].init?.headers as Record<string, string>)['x-csrf-token'], 'csrf-token');
    if (contractCase.signedInProduction) {
      assert.deepEqual(signedCalls.map(({ method, path }) => ({ method, path })), [
        { method: contractCase.method, path: contractCase.signedPath },
      ]);
    }
  }
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
